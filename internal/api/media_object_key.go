package api

import (
	"context"
	"errors"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"path"
	"path/filepath"
	"strings"

	"github.com/athenavi/chiron/internal/storage"
)

// ── 媒体对象键的「服务端唯一权威」（H29）──
//
// 旧行为：上传完成（POST /v1/media/complete）只比较客户端 `file_url` 的
// scheme/host（S3）或 `/media/` 前缀（本地），然后**原样把这个 URL 写进库**；
// 签名下载再从库里的 `file_url` 推导读取路径，下载端点又直接 302 到该 URL。
// 于是「同 host」被当成了「同一对象所有权」——同桶里其它对象、甚至别的租户
// 的对象，都能被代理读出来。
//
// 新行为：对象键**只由上传会话（tenant_id + asset_id + 文件名）推导**，
// 客户端 URL 仅用于**比对**（必须与推导值逐字相同），任何不一致一律拒绝；
// 读取侧同样从资产行推导并校验租户前缀。
//
// 本文件只放纯函数（无 DB / 无 HTTP handler），便于单测覆盖每一条拒绝分支。

var (
	// errMediaKeyMalformed 表示对象键/文件名本身不合法（缺租户、非 UUID、路径穿越）。
	errMediaKeyMalformed = errors.New("media object key malformed")
	// errMediaKeyNotSession 表示客户端给的 file_url 与上传会话推导出的对象键不一致。
	errMediaKeyNotSession = errors.New("media object key does not match upload session")
	// errMediaKeyNotTenant 表示对象键不在当前租户（或资产归属）前缀之下。
	errMediaKeyNotTenant = errors.New("media object key outside tenant scope")
)

// mediaObjectKeyPrefix 返回租户媒体对象键前缀：`media/<tenantID>/`。
func mediaObjectKeyPrefix(tenantID int16) string {
	return "media/" + tenantStr(tenantID) + "/"
}

// cleanObjectKey 归一化并校验一个对象键。
//
// 校验比"看起来干净"更严：`path.Clean` 往返不相等即拒绝（吃掉 `..`、`//`、
// `./`），并显式拒绝反斜杠（Windows 上会被当作分隔符）。
func cleanObjectKey(key string) (string, error) {
	key = strings.TrimPrefix(strings.TrimSpace(key), "/")
	if key == "" {
		return "", errMediaKeyMalformed
	}
	if strings.Contains(key, "\\") || strings.Contains(key, "\x00") {
		return "", errMediaKeyMalformed
	}
	if path.Clean(key) != key || strings.Contains(key, "..") {
		return "", errMediaKeyMalformed
	}
	return key, nil
}

// deriveSessionObjectKey 由**上传会话**推导对象键，这是唯一的键来源。
//
// 客户端不参与键的拼装：assetID 必须是 UUID（否则无法作为目录段），文件名必须
// 通过 sanitizeUploadName（拒绝分隔符/穿越）。两者任一不合法都 fail-close。
func deriveSessionObjectKey(tenantID int16, assetID, rawName string) (string, error) {
	if tenantID == 0 || !uuidRe.MatchString(assetID) {
		return "", errMediaKeyMalformed
	}
	name := sanitizeUploadName(rawName)
	if name == "" {
		return "", errMediaKeyMalformed
	}
	return mediaObjectKey(tenantID, assetID, name), nil
}

// localObjectKeyFromURL 把本地后端的 `/media/...` URL 反解为对象键。
func localObjectKeyFromURL(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" || strings.HasPrefix(raw, "//") || strings.Contains(raw, "://") {
		return "", errMediaKeyMalformed
	}
	if i := strings.IndexAny(raw, "?#"); i >= 0 {
		raw = raw[:i]
	}
	if !strings.HasPrefix(raw, "/media/") {
		return "", errMediaKeyMalformed
	}
	return cleanObjectKey(strings.TrimPrefix(raw, "/"))
}

// objectKeyFromFileURL 把任意后端的公开 URL 反解为对象键（严格校验归属后端）。
func objectKeyFromFileURL(fileURL string, store storage.FileStore) (string, error) {
	inner := store
	if atomic, ok := store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	switch s := inner.(type) {
	case *storage.S3Store:
		return s.ObjectKeyFromURL(fileURL)
	case *storage.LocalStore:
		return localObjectKeyFromURL(fileURL)
	case nil:
		return "", errMediaKeyMalformed
	default:
		return "", errMediaKeyMalformed
	}
}

// resolveSessionObjectKey 校验客户端提交的 file_url 与上传会话推导出的键一致。
//
// 这是 CompleteUpload 的唯一入口：返回值（服务端推导的键）才允许入库，
// 客户端 URL 从不直接写库。
func resolveSessionObjectKey(tenantID int16, assetID, rawName, fileURL string, store storage.FileStore) (string, error) {
	expected, err := deriveSessionObjectKey(tenantID, assetID, rawName)
	if err != nil {
		return "", err
	}
	provided, err := objectKeyFromFileURL(fileURL, store)
	if err != nil {
		return "", err
	}
	if provided != expected {
		return "", errMediaKeyNotSession
	}
	if err := requireTenantObjectKey(tenantID, expected); err != nil {
		return "", err
	}
	return expected, nil
}

// requireTenantObjectKey 要求对象键位于 `media/<tenantID>/` 之下。
func requireTenantObjectKey(tenantID int16, key string) error {
	if tenantID == 0 {
		return errMediaKeyNotTenant
	}
	clean, err := cleanObjectKey(key)
	if err != nil {
		return err
	}
	if !strings.HasPrefix(clean, mediaObjectKeyPrefix(tenantID)) {
		return errMediaKeyNotTenant
	}
	return nil
}

// mediaOwnedObjectKey 从资产行推导「相对 media/ 根」的读取路径，并校验归属。
//
// 归属规则（fail-close，任一不满足即拒绝）：
//   - 新布局：`<tenant>/<assetID>/<name>` —— 三段必须与本次查询的租户和资产一致；
//   - 旧布局：`u_<userID>/...`（无租户前缀的存量对象）—— 首段必须属于该行 owner。
//
// 其余形态（含绝对路径、`..`、指向别的租户/资产的键）一律拒绝。这样即便库里的
// `file_path` / `file_url` 被写脏（历史数据、注入、迁移事故），也不会变成任意文件读取。
func mediaOwnedObjectKey(tenantID int16, assetID, userID, filePath, fileURL string, store storage.FileStore) (string, error) {
	candidate := strings.TrimSpace(filePath)
	if candidate == "" {
		derived, err := objectKeyFromFileURL(fileURL, store)
		if err != nil {
			return "", err
		}
		candidate = derived
	}
	rel := strings.TrimPrefix(strings.TrimPrefix(candidate, "/"), "media/")
	rel, err := cleanObjectKey(rel)
	if err != nil {
		return "", err
	}
	tenantSeg := tenantStr(tenantID)
	if strings.HasPrefix(rel, tenantSeg+"/") {
		if !strings.HasPrefix(rel, tenantSeg+"/"+assetID+"/") {
			return "", errMediaKeyNotTenant
		}
		return rel, nil
	}
	if userID != "" && strings.HasPrefix(rel, "u_"+userID+"/") {
		return rel, nil
	}
	return "", errMediaKeyNotTenant
}

// sniffStoredObject 读取存储后端上对象的前 512 字节判断真实 MIME。
//
// 为什么不 HEAD 客户端给的 URL：那是一次由客户端指定目标的服务器端请求（SSRF），
// 而且对象可能根本不归本租户所有。直接读我们自己推导出来的对象键，既无 SSRF，
// 判据也绑定到真实落盘内容。
func sniffStoredObject(ctx context.Context, store storage.FileStore, objectKey string) (string, error) {
	if store == nil {
		return "", errMediaKeyMalformed
	}
	rc, err := store.OpenStream(ctx, objectKey)
	if err != nil {
		return "", err
	}
	defer func() { _ = rc.Close() }()
	head := make([]byte, 512)
	n, err := io.ReadFull(rc, head)
	if err != nil && !errors.Is(err, io.EOF) && !errors.Is(err, io.ErrUnexpectedEOF) {
		return "", err
	}
	return truncateMIME(http.DetectContentType(head[:n])), nil
}

// ── 下载响应策略：可执行/HTML/SVG 一律 attachment + nosniff ──

// mediaPrivateCacheControl 私有媒体必须禁止任何共享缓存留存（代理/CDN）。
const mediaPrivateCacheControl = "private, no-store"

// mediaDownloadPolicy 是「一次媒体下载应该带哪些响应头」的唯一判据。
type mediaDownloadPolicy struct {
	ContentType  string
	Disposition  string
	Attachment   bool
	CacheControl string
}

// baseMime 去掉 MIME 参数并小写化（`text/HTML; charset=utf-8` → `text/html`）。
func baseMime(mimeType string) string {
	m := strings.ToLower(strings.TrimSpace(mimeType))
	if i := strings.Index(m, ";"); i >= 0 {
		m = strings.TrimSpace(m[:i])
	}
	return m
}

// isActiveContentMIME 判定会在浏览器里「作为文档解析并执行脚本」的 MIME。
//
// 这些类型即使用户直接打开 URL 也不能内联渲染：SVG/XHTML/XML 家族可以内嵌
// `<script>`，`text/html` 更不用说 —— 在对象存储域或网关同源下就是存储型 XSS。
func isActiveContentMIME(mimeType string) bool {
	switch baseMime(mimeType) {
	case "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml",
		"application/xml", "application/javascript", "text/javascript",
		"application/x-javascript", "application/ecmascript", "text/ecmascript",
		"application/x-shockwave-flash", "text/css":
		return true
	}
	return false
}

// isInlineMediaType 只允许这些类型内联渲染（图片/音视频/PDF/纯文本）。
func isInlineMediaType(mimeType string) bool {
	m := baseMime(mimeType)
	switch {
	case strings.HasPrefix(m, "image/"), strings.HasPrefix(m, "audio/"),
		strings.HasPrefix(m, "video/"):
		return m != "image/svg+xml"
	case m == "application/pdf", m == "text/plain", m == "text/markdown":
		return true
	}
	return false
}

// mediaDownloadHeaders 依据真实 MIME 与文件名推导下载响应策略。
//
// 规则：
//   - 可执行/HTML/SVG（按 MIME 或扩展名）→ `attachment` + `application/octet-stream`；
//   - 白名单内联类型（图片/音视频/PDF/纯文本）→ `inline`；
//   - 其余未知类型 → `attachment`（宁可让浏览器另存，也不猜它能安全渲染）。
func mediaDownloadHeaders(mimeType, fileName string) mediaDownloadPolicy {
	display := sanitizeDispositionFilename(fileName)
	ct := baseMime(mimeType)
	if ct == "" {
		ct = baseMime(mime.TypeByExtension(strings.ToLower(filepath.Ext(fileName))))
	}
	active := isActiveContentMIME(ct) || isExecutableMIME(ct, fileName)
	if active {
		ct = "application/octet-stream"
	}
	inline := !active && isInlineMediaType(ct)
	kind := "attachment"
	if inline {
		kind = "inline"
	}
	if ct == "" {
		ct = "application/octet-stream"
		kind = "attachment"
	}
	return mediaDownloadPolicy{
		ContentType:  ct,
		Disposition:  contentDispositionValue(kind, display),
		Attachment:   kind == "attachment",
		CacheControl: mediaPrivateCacheControl,
	}
}

// sanitizeDispositionFilename 净化 Content-Disposition 里的文件名。
//
// 只保留 basename 并剔除引号/控制字符/CR-LF：否则 `a".html` 或换行可以闭合
// 引号或注入额外响应头（header injection）。
func sanitizeDispositionFilename(name string) string {
	name = strings.TrimSpace(name)
	name = filepath.Base(strings.ReplaceAll(name, "\\", "/"))
	var b strings.Builder
	for _, r := range name {
		switch {
		case r == '"' || r == '\\' || r == '\r' || r == '\n' || r < 0x20 || r == 0x7f:
			continue
		case r == '/':
			continue
		default:
			b.WriteRune(r)
		}
	}
	out := strings.TrimSpace(b.String())
	if out == "." || out == ".." {
		out = ""
	}
	if len(out) > 120 {
		out = out[:120]
	}
	return out
}

// contentDispositionValue 组装 Content-Disposition，非 ASCII 走 RFC 5987 的 filename*。
func contentDispositionValue(kind, filename string) string {
	if filename == "" {
		filename = "download"
	}
	ascii := true
	for _, r := range filename {
		if r > 0x7f {
			ascii = false
			break
		}
	}
	if ascii {
		return fmt.Sprintf(`%s; filename="%s"`, kind, filename)
	}
	return fmt.Sprintf(`%s; filename="download"; filename*=UTF-8''%s`, kind, url.PathEscape(filename))
}

// applyMediaHeaders 把策略写进响应头。nosniff 无条件设置：
// 即使浏览器对 Content-Type 有别的猜测路径，也不允许它把内容升级成可执行文档。
func applyMediaHeaders(h http.Header, p mediaDownloadPolicy) {
	h.Set("Content-Type", p.ContentType)
	h.Set("Content-Disposition", p.Disposition)
	h.Set("Cache-Control", p.CacheControl)
	h.Set("X-Content-Type-Options", "nosniff")
}
