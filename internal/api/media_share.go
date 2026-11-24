package api

import (
	"log/slog"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/storage"
)

// ── Download ──

// Download 返回资产的下载响应。
//
// 旧实现把库里存的 `file_url` 直接 302 出去：一旦库里出现任意 URL（历史写入
// 信任过客户端、迁移事故、注入），这里就成了开放式代理。现在改为**从资产行
// 重新推导对象键并校验归属**，再按下载策略（attachment/nosniff/private）出流。
func (h *MediaHandler) Download(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	var filePath, fileURL, mimeType, name string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(file_path,''), COALESCE(file_url,''), COALESCE(mime_type,''), COALESCE(name,'')
		 FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
		id, claims.TenantID, claims.UserID).Scan(&filePath, &fileURL, &mimeType, &name); err != nil {
		NotFound(w, "media asset not found")
		return
	}
	relKey, keyErr := mediaOwnedObjectKey(claims.TenantID, id, claims.UserID, filePath, fileURL, h.store)
	if keyErr != nil {
		slog.Warn("media download rejected: object key outside tenant scope",
			"asset_id", id, "tenant_id", claims.TenantID, "file_path", filePath, "error", keyErr)
		NotFound(w, "asset has no stored file")
		return
	}

	policy := mediaDownloadHeaders(mimeType, name)
	if s3 := h.s3Store(); s3 != nil {
		url, err := s3.PresignedGetURLWithResponse(r.Context(), "media/"+relKey, mediaSignTTL,
			policy.ContentType, policy.Disposition, policy.CacheControl)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "generate download url failed")
			return
		}
		http.Redirect(w, r, url, http.StatusFound)
		return
	}
	h.serveMediaFile(w, r, relKey, policy)
}

// ── Share ──

// Share returns a time-limited presigned download URL (S3 backend only).
func (h *MediaHandler) Share(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.TenantID == 0 {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	var body struct {
		ExpiresInSeconds int `json:"expires_in_seconds"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.ExpiresInSeconds <= 0 {
		body.ExpiresInSeconds = 900
	}
	if body.ExpiresInSeconds > 7*24*3600 {
		BadRequest(w, "expires_in_seconds must be <= 604800")
		return
	}

	var filePath, fileURL, fileName, mimeType string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(file_path,''), COALESCE(file_url,''), COALESCE(name,''), COALESCE(mime_type,'')
		 FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
		id, claims.TenantID, claims.UserID).Scan(&filePath, &fileURL, &fileName, &mimeType); err != nil {
		NotFound(w, "media asset not found")
		return
	}
	// 分享链接必须指向**这条资产自己的**对象：旧实现用
	// `media/u_<user>/<id[:8]>_<name>` 现拼键（名字还来自请求行），等于可以拿一张
	// 分享票去签同目录下任意文件名。现在只接受资产行推导出的、通过归属校验的键。
	relKey, keyErr := mediaOwnedObjectKey(claims.TenantID, id, claims.UserID, filePath, fileURL, h.store)
	if keyErr != nil {
		slog.Warn("media share rejected: object key outside tenant scope",
			"asset_id", id, "tenant_id", claims.TenantID, "file_path", filePath, "error", keyErr)
		NotFound(w, "asset has no stored file")
		return
	}

	// 类型断言取 S3 后端（与 PresignUpload 相同模式）
	inner := h.store
	if atomic, ok := h.store.(*storage.AtomicStore); ok {
		inner = atomic.LoadRaw()
	}
	s3store, ok := inner.(*storage.S3Store)
	if !ok {
		BadRequest(w, "share links require S3 storage backend")
		return
	}

	ttl := time.Duration(body.ExpiresInSeconds) * time.Second
	policy := mediaDownloadHeaders(mimeType, fileName)
	url, err := s3store.PresignedGetURLWithResponse(r.Context(), "media/"+relKey, ttl,
		policy.ContentType, policy.Disposition, policy.CacheControl)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "generate share link failed")
		return
	}
	OK(w, map[string]interface{}{
		"url":        url,
		"expires_at": time.Now().Add(ttl).Format(time.RFC3339),
		"expires_in": body.ExpiresInSeconds,
	})
}
