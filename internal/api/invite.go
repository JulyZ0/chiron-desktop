package api

import (
	"crypto/sha256"
	"encoding/hex"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"golang.org/x/crypto/bcrypt"
)

// invite.go —— 管理员生成一次性邀请链接，受邀人用链接设置自己的密码。
//
// 与「临时密码只展示一次」相比，邀请链接不容易经聊天工具外传：token 只存 sha256
// 摘要、24 小时过期、用后即焚，且要求 HTTPS 页面输入。邮件通道（SMTP）仍后置。

const inviteTokenTTL = 24 * time.Hour

// CreateUserInvite POST /v1/admin/users/{id}/invite
func (h *AdminHandler) CreateUserInvite(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenantID := h.adminTenant(r)
	target, err := loadManagedUser(r.Context(), tenantID, r.PathValue("id"))
	if err != nil {
		NotFound(w, "user not found")
		return
	}
	if userStatus(target.Status) != "active" {
		BadRequest(w, "user is not active")
		return
	}
	// 与重置密码同一套"谁能管谁"：不能给自己发、不能绕开 owner 保护。
	if err := ensureCanManage(r.Context(), claims, target, "reset_password"); err != nil {
		Forbidden(w, err.Error())
		return
	}

	raw, err := auth.GenerateMailToken()
	if err != nil {
		InternalError(w, "generate invite token failed")
		return
	}
	hash := sha256.Sum256([]byte(raw))
	tokenHash := hex.EncodeToString(hash[:])
	expiresAt := time.Now().Add(inviteTokenTTL)

	// 同一个用户只保留最新一条未使用的邀请；旧链接自然失效。
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM user_invite_tokens WHERE user_id = $1 AND used_at IS NULL`, target.ID,
	); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "clear old invites failed")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO user_invite_tokens (token_hash, user_id, tenant_id, expires_at, created_at)
		 VALUES ($1, $2, $3, $4, NOW())`,
		tokenHash, target.ID, tenantID, expiresAt,
	); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create invite failed")
		return
	}

	inviteURL := ""
	if base := publicBaseURL(h.cfg, r); base != "" {
		inviteURL = base + "/invite/" + raw
	}
	// 邮件投递是"有则更好"：未配置/发送失败不撤销邀请，链接仍由管理员复制。
	// email_note 只给稳定的原因码，不含服务商原始错误（可能带收件人/凭据片段）。
	emailSent := false
	emailNote := ""
	if inviteURL == "" {
		emailNote = "public_base_url_missing"
	} else if !auth.ValidateMailAddress(target.Email) {
		emailNote = "invalid_recipient"
	} else if cfg := readMailRuntimeConfig(r.Context(), h.ensureSettingsStore()); cfg.validate() != nil {
		emailNote = "mail_not_configured"
	} else if err := sendInviteEmail(r.Context(), mailSenderFactory(), cfg, target.Email, inviteURL, expiresAt); err != nil {
		slog.Warn("invite email send failed", "user_id", target.ID, "error", err)
		emailNote = "mail_send_failed"
	} else {
		emailSent = true
	}
	db.AuditLog(r.Context(), claims.UserID, tenantID, "invite_created",
		"/v1/admin/users/"+target.ID+"/invite", "email="+maskEmail(target.Email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"invite_url":  inviteURL,
		"token":       raw,
		"expires_at":  expiresAt.UTC().Format(time.RFC3339),
		"email_sent":  emailSent,
		"email_note":  emailNote,
	})
}

// AcceptInvite POST /v1/auth/invite/accept —— 公开端点（限流，无鉴权）。
func AcceptInvite(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Token    string `json:"token"`
		Password string `json:"password"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	token := strings.TrimSpace(body.Token)
	if token == "" || body.Password == "" {
		BadRequest(w, "token and password are required")
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(body.Password); !ok {
		BadRequest(w, msg)
		return
	}
	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "user store unavailable")
		return
	}

	hash := sha256.Sum256([]byte(token))
	tokenHash := hex.EncodeToString(hash[:])
	var userID string
	var tenantID int16
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT user_id, tenant_id FROM user_invite_tokens
		  WHERE token_hash = $1 AND used_at IS NULL AND expires_at > NOW()`,
		tokenHash,
	).Scan(&userID, &tenantID); err != nil {
		BadRequest(w, "invite is invalid or expired")
		return
	}

	passwordHash, err := bcrypt.GenerateFromPassword([]byte(body.Password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "set password failed")
		return
	}

	// 先原子消费 token：并发的两次提交只有一次能成功。
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE user_invite_tokens SET used_at = NOW()
		  WHERE token_hash = $1 AND used_at IS NULL AND expires_at > NOW()`, tokenHash)
	if err != nil || tag.RowsAffected() != 1 {
		BadRequest(w, "invite is invalid or expired")
		return
	}

	tag, err = db.GlobalDBManager.Exec(r.Context(),
		`UPDATE users
		    SET password_hash = $1, password_set = true, must_change_password = false,
		        password_changed_at = NOW(), updated_at = NOW()
		  WHERE id = $2 AND tenant_id = $3 AND COALESCE(status, 'active') = 'active'`,
		string(passwordHash), userID, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "set password failed")
		return
	}
	if tag.RowsAffected() != 1 {
		BadRequest(w, "user is not active")
		return
	}

	if err := revokeUserSessions(r.Context(), userID); err != nil {
		slog.Warn("invite accepted but session revoke failed", "user_id", userID, "error", err)
	}
	db.AuditLog(r.Context(), userID, tenantID, "invite_accepted",
		"/v1/auth/invite/accept", "", r.RemoteAddr, nil)
	OK(w, map[string]interface{}{"user_id": userID})
}
