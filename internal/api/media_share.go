package api

import (
	"fmt"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/storage"
)

// ── Download ──

// Download redirects to the asset's storage URL.
func (h *MediaHandler) Download(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}
	tenantID := claims.TenantID
	var fileURL string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(file_url,'') FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
		id, tenantID, claims.UserID).Scan(&fileURL); err != nil {
		NotFound(w, "media asset not found")
		return
	}
	if fileURL == "" {
		NotFound(w, "asset has no stored file")
		return
	}
	http.Redirect(w, r, fileURL, http.StatusFound)
}

// ── Share ──

// Share returns a time-limited presigned download URL (S3 backend only).
func (h *MediaHandler) Share(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
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

	var fileName, userID string
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(name,''), COALESCE(user_id,'') FROM media_assets WHERE id=$1 AND tenant_id=$2 AND user_id=$3`,
		id, claims.TenantID, claims.UserID).Scan(&fileName, &userID); err != nil {
		NotFound(w, "media asset not found")
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

	dir := "anonymous"
	if userID != "" {
		dir = "u_" + userID
	}
	// legacyShortAssetID：存量资产的旧布局键（file_path 为空时的回退）
	objectKey := fmt.Sprintf("media/%s/%s_%s", dir, legacyShortAssetID(id), fileName)
	ttl := time.Duration(body.ExpiresInSeconds) * time.Second
	url, err := s3store.PresignedGetURL(r.Context(), objectKey, ttl)
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
