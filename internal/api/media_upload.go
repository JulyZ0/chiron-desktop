package api

import (
	"bytes"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"path/filepath"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"github.com/athenavi/chiron/internal/storage"
)

// ── Upload (multipart file) ──

func (h *MediaHandler) Upload(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := claims.TenantID

	// 单请求流式上传：上限 50MB。更大的文件应走 /v1/uploads 分片上传
	// （前端 api/index.ts uploadFile 会按大小自动分流）。
	const maxInlineUpload = 50 << 20
	r.Body = http.MaxBytesReader(w, r.Body, maxInlineUpload)
	if err := r.ParseMultipartForm(4 << 20); err != nil {
		// 暴露真实原因（请求体超限 / boundary 缺失 / 格式损坏），便于定位
		slog.Warn("parse multipart form failed", "error", err, "content_type", r.Header.Get("Content-Type"), "content_length", r.ContentLength)
		BadRequest(w, "file too large or invalid form")
		return
	}

	file, header, err := r.FormFile("file")
	if err != nil {
		slog.Warn("get form file failed", "error", err, "form_keys", r.MultipartForm.File)
		BadRequest(w, "file is required")
		return
	}
	defer file.Close()

	// 流式写入：不再把整个文件读进内存。此前 io.ReadAll 在 200MB 上限下会让
	// 单个请求的内存峰值达到 200MB，并发上传极易触发 OOM。
	// magic bytes 检测只需前 512 字节，因此先预读头部，再与剩余流拼接后整体落盘。
	fileSize := header.Size
	head := make([]byte, 512)
	n, err := io.ReadFull(file, head)
	if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
		logAndRespond(w, err, http.StatusInternalServerError, "read file failed")
		return
	}
	head = head[:n]

	// P1-6: 用 magic bytes 检测真实 MIME，避免客户端伪造 Content-Type 上传可执行文件
	declaredMIME := truncateMIME(header.Header.Get("Content-Type"))
	detectedMIME := truncateMIME(http.DetectContentType(head))
	// 优先采用检测到的 MIME；声明与检测不一致时以检测为准（更安全）
	mimeType := detectedMIME
	if declaredMIME != "" && declaredMIME == detectedMIME {
		// 声明与检测一致，保留声明的（可能更精确，如 image/png vs image/jpeg）
		mimeType = declaredMIME
	}
	// 拒绝可执行/脚本类 MIME（即使客户端伪装为图片）
	// P1-6: 传入文件名以便扩展名兜底（PE/ELF magic bytes 只检测为 octet-stream）
	if isExecutableMIME(mimeType, header.Filename) {
		BadRequest(w, "file type not allowed: "+mimeType)
		return
	}
	assetType := detectType(mimeType)
	category := r.FormValue("category")
	parentID := r.FormValue("parent_id") // 当前目录；空 = 根目录

	name := r.FormValue("name")
	if name == "" {
		name = header.Filename
	}

	// 租户配额门禁：存储容量 + 文件数量。此前 CheckStorageQuota / CheckFileCountQuota
	// 从未被调用，配额完全不生效。放在落库**之前**，避免超限文件先入库再回滚。
	if allowed, reason := CheckUploadAllowed(r.Context(), tenantStr(tenantID), fileSize); !allowed {
		JSON(w, http.StatusRequestEntityTooLarge, APIResponse{Success: false, Error: reason})
		return
	}

	// 先插入数据库获取 UUID（含 parent_id，子文件夹上传不落到根目录）
	assetID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, mime_type, category, size, parent_id, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, '', $6, $7, $8, $9, NOW(), NOW())`,
		assetID, tenantID, claims.UserID, assetType, name, mimeType, nullableStr(category), fileSize, parentID,
	)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
		return
	}

	objectKey := mediaObjectKey(tenantID, assetID, sanitizeUploadName(name))
	// 预读的头部 + 剩余流拼接，保证落盘内容与原始文件完全一致
	stream := io.MultiReader(bytes.NewReader(head), file)
	if err := h.store.WriteStream(r.Context(), objectKey, stream, fileSize, 0o644); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "save file failed")
		return
	}
	fileURL := h.objectURL(objectKey)

	// 更新 file_url 和 file_path（签名URL服务需要）
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`UPDATE media_assets SET file_url = $1, file_path = $2 WHERE id = $3 AND tenant_id = $4`,
		fileURL, objectKey, assetID, tenantID)
	if err != nil {
		slog.Warn("update file_url/file_path", "error", err)
	}

	OK(w, map[string]string{
		"id": assetID, "name": name, "type": assetType,
		"file_url": fileURL, "size": fmt.Sprintf("%d", fileSize),
	})
}

// ── PresignUpload — returns a presigned URL for client-side direct upload ──

func (h *MediaHandler) PresignUpload(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Name     string `json:"name"`
		Type     string `json:"type"`
		Category string `json:"category"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Name == "" {
		BadRequest(w, "name is required")
		return
	}
	// 预签名与完成阶段必须用同一份净化结果拼对象键，否则两边推导出的键不同，
	// 合法上传会在 complete 阶段被判成「不属于本会话」。
	if sanitizeUploadName(body.Name) == "" {
		BadRequest(w, "invalid name")
		return
	}
	if body.Type == "" {
		body.Type = "file"
	}
	if body.Category == "" {
		body.Category = "upload"
	}

	// Type-assert to S3Store for presigned URL support (unwrap AtomicStore first)
	inner := h.store
	if atomic, ok := h.store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	s3store, ok := inner.(*storage.S3Store)
	if !ok {
		InternalError(w, "presigned upload requires S3 storage backend")
		return
	}

	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	// 对象键唯一性来源：完整 UUID（id.NextID() 是 snowflake→base62，高位为毫秒时间戳，
	// 任何前缀截断都会在同一毫秒内碰撞，故不再使用）。
	assetID, err := id.UUID()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate id failed")
		return
	}
	objectKey := mediaObjectKey(claims.TenantID, assetID, sanitizeUploadName(body.Name))
	fileURL := s3store.ObjectURL(objectKey)

	presignedURL, err := s3store.PresignedPutURL(r.Context(), objectKey, 15*time.Minute)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate presigned url failed")
		return
	}

	OK(w, map[string]string{
		"id":            assetID,
		"name":          body.Name,
		"type":          body.Type,
		"category":      body.Category,
		"file_url":      fileURL,
		"presigned_url": presignedURL,
		"expires_in":    "900",
	})
}

// ── CompleteUpload — called by client after presigned upload is done ──

func (h *MediaHandler) CompleteUpload(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := claims.TenantID

	var body struct {
		ID       string `json:"id"`
		Name     string `json:"name"`
		Type     string `json:"type"`
		FileURL  string `json:"file_url"`
		Category string `json:"category"`
		Size     int64  `json:"size"`
		MimeType string `json:"mime_type"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// 上传会话 id 就是对象键的目录段：服务端据此推导键，客户端不能自带路径。
	if !uuidRe.MatchString(body.ID) {
		BadRequest(w, "id (upload session) is required and must be a UUID")
		return
	}
	if body.Type == "" {
		body.Type = "file"
	}

	// ── 对象键：服务端从上传会话推导，客户端 file_url 只用于比对 ──
	//
	// 旧实现只比较 scheme/host（S3）或 `/media/` 前缀（本地），通过之后**原样入库**。
	// 「同 host」于是被当成「同一对象所有权」：同桶里任何可读对象都能被写进自己的
	// 资产行，再经下载端点代理读出（签名下载又会照着库里的 URL 去取）。
	objectKey, keyErr := resolveSessionObjectKey(tenantID, body.ID, body.Name, body.FileURL, h.store)
	if keyErr != nil {
		slog.Warn("complete upload rejected: object key does not match upload session",
			"tenant_id", tenantID, "asset_id", body.ID, "file_url", body.FileURL, "error", keyErr)
		BadRequest(w, "file_url does not match the upload session object key")
		return
	}

	// ── 内容类型强校验：直接读我们自己推导出的对象（不再 HEAD 客户端 URL）──
	//
	// 旧实现 `http.Head(body.FileURL)` 有两个问题：一是客户端可控目标（SSRF），
	// 二是它读的可能是别人的对象。这里读存储后端上真实落盘的内容，读不到即拒绝
	//（完成阶段对象必然已存在，否则入库只会留下悬空资产）。
	mimeType := truncateMIME(baseMime(body.MimeType))
	sniffed, sniffErr := sniffStoredObject(r.Context(), h.store, objectKey)
	if sniffErr != nil {
		slog.Warn("complete upload rejected: uploaded object not readable",
			"tenant_id", tenantID, "asset_id", body.ID, "object_key", objectKey, "error", sniffErr)
		BadRequest(w, "uploaded object not found; upload the file before completing")
		return
	}
	if isExecutableMIME(sniffed, body.Name) || isActiveContentMIME(sniffed) {
		BadRequest(w, "file type not allowed: "+sniffed)
		return
	}
	mimeType = sniffed

	assetID := body.ID
	// file_url 一律用服务端推导值：与对象键同源，客户端无法把任意 URL 塞进库里。
	fileURL := h.objectURL(objectKey)
	_, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO media_assets (id, tenant_id, user_id, type, name, file_url, file_path, mime_type, category, size, parent_id, created_at, updated_at)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, '', NOW(), NOW())`,
		assetID, tenantID, claims.UserID, body.Type, body.Name, fileURL, objectKey, mimeType, nullableStr(body.Category), body.Size)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create asset failed")
		return
	}

	OK(w, map[string]string{"id": assetID, "name": body.Name, "file_url": fileURL})
}

// ── Upload helpers ──

func detectType(mime string) string {
	if strings.HasPrefix(mime, "image/") {
		return "image"
	}
	if strings.HasPrefix(mime, "video/") {
		return "video"
	}
	if strings.HasPrefix(mime, "audio/") {
		return "audio"
	}
	if strings.Contains(mime, "pdf") || strings.Contains(mime, "document") {
		return "document"
	}
	return "file"
}

// truncateMIME ensures MIME type fits in the VARCHAR(64) column.
func truncateMIME(mime string) string {
	if len(mime) > 64 {
		return mime[:64]
	}
	return mime
}

// isExecutableMIME 拦截可执行/脚本类 MIME，防止伪装为图片上传恶意文件。
// P1-6: 即使 magic bytes 检测出真实类型，仍需拒绝危险类型落地存储。
// fileName 用于扩展名兜底（net/http 的 DetectContentType 对 PE/ELF/Mach-O
// 通常返回 application/octet-stream，无法区分可执行文件与普通二进制）。
func isExecutableMIME(mime string, fileName string) bool {
	// 归一化小写并去掉参数
	m := strings.ToLower(strings.TrimSpace(mime))
	if i := strings.Index(m, ";"); i >= 0 {
		m = strings.TrimSpace(m[:i])
	}
	switch m {
	case "application/x-msdownload",
		"application/x-msdos-program",
		"application/x-msi",
		"application/x-sh",
		"application/x-shar",
		"application/x-csh",
		"application/x-bat",
		"application/x-batch",
		"application/vnd.microsoft.portable-executable",
		"application/x-elf",
		"application/x-executable",
		"application/x-mach-o-executable",
		"application/x-mach-binary":
		return true
	}
	// 扩展名兜底：octet-stream（PE/ELF/Mach-O magic bytes 通用回退）或 text/plain
	// （shell 脚本检测为 text/plain）+ 危险扩展名 → 拒绝
	if m == "application/octet-stream" || m == "" || m == "text/plain" || m == "text/html" {
		ext := strings.ToLower(filepath.Ext(fileName))
		switch ext {
		case ".exe", ".dll", ".msi", ".sh", ".bat", ".cmd", ".com", ".scr",
			".so", ".dylib", ".app", ".jar", ".class", ".py", ".rb", ".pl",
			".ps1", ".vbs", ".wsf", ".htm", ".html",
			// SVG / XHTML / XML 家族：浏览器**按文档解析并执行其中的脚本**，而
			// DetectContentType 会把它们判成 text/plain（正好走上面这个入口）⇒
			// 此前可上传 `<svg><script>…</script></svg>`，再经 /media/s/ 签名 URL
			// 以 image/svg+xml 返回 ⇒ **同源存储型 XSS**（S3 后端下 302 到对象存储域、
			// 无 CSP 兜底，直接执行）。
			".svg", ".svgz", ".xhtml", ".xht", ".xml", ".xsl", ".xslt",
			".mhtml", ".mht", ".swf":
			return true
		}
	}
	// text/plain 可能是任何脚本，但 magic bytes 只能识别文本；
	// 这里只拦截明确的可执行二进制类型，text/plain 由业务层判断扩展名
	return false
}
