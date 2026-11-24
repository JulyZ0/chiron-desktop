package api

// auth_team.go —— 团队模式账号闭环：管理员建号/重置/禁用、用户自助改密、
// 首位 owner 引导。
//
// 为什么单独成文件：auth.go 的主线是"登录/注册/会话"，AdminHandler 的主线是
// 平台运维。团队账号的规则（谁能管谁、哪些动作要吊销会话）集中在这里，改动时
// 只需要看一处，不必在三个 handler 之间来回比对口径。

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"golang.org/x/crypto/bcrypt"
)

func currentPasswordError(storedHash, oldPassword, newPassword string, passwordSet bool) string {
	if !passwordSet {
		return ""
	}
	if oldPassword == "" {
		return "current password is required"
	}
	if err := bcrypt.CompareHashAndPassword([]byte(storedHash), []byte(oldPassword)); err != nil {
		return "current password is incorrect"
	}
	if oldPassword == newPassword {
		return "new password must be different from the current password"
	}
	return ""
}

// ChangePassword POST /v1/auth/password —— 已登录用户自助改密。
//
// 首登（`must_change_password=true`）也走这里：AuthMiddleware 只放行本端点、
// 登出、profile 与会话刷新，改密成功后才解除限制。
func (h *AuthHandler) ChangePassword(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		OldPassword string `json:"old_password"`
		NewPassword string `json:"new_password"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.NewPassword == "" {
		BadRequest(w, "new_password is required")
		return
	}
	if len(body.NewPassword) > 128 {
		BadRequest(w, "password too long")
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(body.NewPassword); !ok {
		BadRequest(w, msg)
		return
	}

	tenantID := claims.TenantID
	if tenantID == 0 {
		tenantID = DefaultTenantID
	}

	var storedHash, status, email, name, username, nickname string
	var role string
	var passwordSet bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(password_hash, ''), COALESCE(status, 'active'),
		        COALESCE(email, ''), COALESCE(name, ''), COALESCE(username, ''),
		        COALESCE(nickname, ''), COALESCE(role, 'user'),
		        COALESCE(password_set, true)
		   FROM users WHERE id = $1 AND tenant_id = $2`,
		claims.UserID, tenantID,
	).Scan(&storedHash, &status, &email, &name, &username, &nickname, &role, &passwordSet); err != nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if userStatus(status) != userStatusActive {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if msg := currentPasswordError(storedHash, body.OldPassword, body.NewPassword, passwordSet); msg != "" {
		BadRequest(w, msg)
		return
	}
	// password_set=false 是本地 owner 的"首次设置密码"：它此前的 hash 是随机值，
	// 没人知道，要求旧密码等于把账号锁死。

	hash, err := bcrypt.GenerateFromPassword([]byte(body.NewPassword), auth.BcryptCost)
	if err != nil {
		InternalError(w, "password change failed")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE users
		    SET password_hash = $1,
		        password_set = true,
		        must_change_password = false,
		        password_changed_at = NOW(),
		        updated_at = NOW()
		  WHERE id = $2 AND tenant_id = $3`,
		string(hash), claims.UserID, tenantID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "password change failed")
		return
	}

	// 改密是标准止血动作：让此前签发的所有 token/API key 立即失效。
	if err := revokeUserSessions(r.Context(), claims.UserID); err != nil {
		slog.Warn("password changed but session revoke failed", "user_id", claims.UserID, "error", err)
	}

	// 当前会话重签一枚 must_change_password=false 的 token，避免用户刚改完
	// 就被踢回登录页；旧 token 已被上面的纪元作废。
	newToken, err := h.auth.GenerateTokenWithState(claims.UserID, email, role, tenantID,
		resolveUserPermsDefault(r.Context(), claims.UserID, role), false)
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}
	SetTokenCookie(w, newToken, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)

	action := "password_changed"
	if !passwordSet {
		action = "password_set"
	}
	db.AuditLog(r.Context(), claims.UserID, tenantID, action, "/v1/auth/password",
		"email="+maskEmail(email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": newToken,
		"user": UserResponse{
			ID: claims.UserID, Email: email, Username: username,
			Name: name, Nickname: nickname, Role: role,
			PasswordSet: true, MustChangePassword: false,
		},
	})
}

// ── 管理员账号管理 ─────────────────────────────────────────────────────

// usernameRe 约束登录用户名：字母数字与 . _ -，3-64 位。
// 不允许 @ 是为了让"邮箱或用户名"的判定不会互相歧义。
var usernameRe = regexp.MustCompile(`^[a-zA-Z0-9._-]{3,64}$`)

type managedUser struct {
	ID       string
	Email    string
	Name     string
	Role     string
	Status   string
	TenantID int16
}

func loadManagedUser(ctx context.Context, tenantID int16, id string) (*managedUser, error) {
	var u managedUser
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT id, COALESCE(email, ''), COALESCE(name, ''),
		        COALESCE(role, 'user'), COALESCE(status, 'active'), COALESCE(tenant_id, 0)
		   FROM users WHERE id = $1 AND tenant_id = $2`,
		id, tenantID,
	).Scan(&u.ID, &u.Email, &u.Name, &u.Role, &u.Status, &u.TenantID)
	if err != nil {
		return nil, err
	}
	return &u, nil
}

// ensureCanManage 是"谁能管谁"的单一判定点。
//
// 口径：
//   - 只有 owner 能对 owner 施加管理动作；
//   - 任何人都不能禁用/删除/重置自己（改自己的密码走自助改密）；
//   - 不能把最后一个 owner 禁用/删除/降级（否则系统失去管理入口）。
func ensureCanManage(ctx context.Context, actor *auth.Claims, target *managedUser, action string) error {
	if actor == nil {
		return errors.New("not authenticated")
	}
	if target == nil {
		return errors.New("user not found")
	}
	if userStatus(target.Status) == userStatusDeleted {
		return errors.New("user not found")
	}
	if target.Role == "owner" && actor.Role != "owner" {
		return errors.New("only the platform owner can manage an owner")
	}
	// 管理员之间不互相管理：否则任一 admin 都能把另一个 admin 禁用/重置，
	// 横向越权比"管普通成员"严重得多。
	if target.Role == "admin" && actor.Role != "owner" && target.ID != actor.UserID {
		return errors.New("only the platform owner can manage an admin")
	}
	switch action {
	case "disable", "delete", "reset_password":
		if target.ID == actor.UserID {
			return errors.New("cannot perform this action on your own account")
		}
	}
	if target.Role == "owner" && action != "force_logout" {
		var owners int
		if err := db.GlobalDBManager.QueryRow(ctx,
			`SELECT COUNT(*) FROM users WHERE role = 'owner' AND COALESCE(status, 'active') <> 'deleted'`,
		).Scan(&owners); err != nil {
			return fmt.Errorf("count owners: %w", err)
		}
		if owners <= 1 {
			return errors.New("cannot remove or demote the last owner")
		}
	}
	return nil
}

func (h *AdminHandler) adminTenant(r *http.Request) int16 {
	if claims := auth.GetClaims(r.Context()); claims != nil && claims.TenantID != 0 {
		return claims.TenantID
	}
	return DefaultTenantID
}

// CreateUser POST /v1/admin/users —— 后台建号（团队模式的主要入口）。
func (h *AdminHandler) CreateUser(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	var body struct {
		Email           string `json:"email"`
		Username        string `json:"username"`
		Nickname        string `json:"nickname"`
		Name            string `json:"name"` // 兼容旧调用方：nickname 为空时用它
		Role            string `json:"role"`
		InitialPassword string `json:"initial_password"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	email := strings.ToLower(strings.TrimSpace(body.Email))
	username := strings.ToLower(strings.TrimSpace(body.Username))
	nickname := strings.TrimSpace(body.Nickname)
	if nickname == "" {
		nickname = strings.TrimSpace(body.Name)
	}
	role := strings.ToLower(strings.TrimSpace(body.Role))
	if role == "" {
		role = "user"
	}
	if email == "" {
		BadRequest(w, "email is required")
		return
	}
	if username != "" && !usernameRe.MatchString(username) {
		BadRequest(w, "username must be 3-64 chars of letters, digits, dot, underscore or hyphen")
		return
	}
	if nickname == "" {
		nickname = username
	}
	if nickname == "" {
		nickname = strings.SplitN(email, "@", 2)[0]
	}
	if len(email) > 255 || len(nickname) > 128 {
		BadRequest(w, "email or nickname too long")
		return
	}
	if role != "user" && role != "admin" && role != "owner" {
		BadRequest(w, "invalid role: must be user, admin, or owner")
		return
	}
	// 只有 owner 能造管理员；admin 只能建普通成员。
	if role != "user" && claims.Role != "owner" {
		Forbidden(w, "only the platform owner can create admin or owner accounts")
		return
	}

	password := body.InitialPassword
	if password == "" {
		generated, err := generateTemporaryPassword()
		if err != nil {
			InternalError(w, "generate initial password failed")
			return
		}
		password = generated
	} else if ok, msg := auth.ValidatePasswordComplexity(password); !ok {
		BadRequest(w, msg)
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "create user failed")
		return
	}

	tenantID := h.adminTenant(r)
	var userID string
	err = db.GlobalDBManager.QueryRow(r.Context(),
		`INSERT INTO users (id, tenant_id, email, username, nickname, name,
		                    password_hash, password_set, role, status,
		                    must_change_password, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, NULLIF($3, ''), $4, $4,
		         $5, true, $6, 'active', true, NOW(), NOW())
		 RETURNING id`,
		tenantID, email, username, nickname, string(hash), role,
	).Scan(&userID)
	if err != nil {
		if isUniqueViolation(err) {
			JSON(w, http.StatusConflict, APIResponse{Success: false, Error: "email or username already exists"})
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "create user failed")
		return
	}

	StartOnboarding(r.Context(), userID)
	db.AuditLog(r.Context(), claims.UserID, tenantID, "user_created", "/v1/admin/users",
		"target="+userID+" email="+maskEmail(email)+" role="+role, r.RemoteAddr, nil)
	Created(w, map[string]interface{}{
		"user": UserResponse{
			ID: userID, Email: email, Username: username, Name: nickname, Nickname: nickname,
			Role: role, PasswordSet: true, MustChangePassword: true,
		},
		// 只返回这一次；不落日志、不写库（库里只有 bcrypt 摘要）。
		"initial_password": password,
	})
}

// ResetUserPassword POST /v1/admin/users/{id}/password/reset
func (h *AdminHandler) ResetUserPassword(w http.ResponseWriter, r *http.Request) {
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
	if err := ensureCanManage(r.Context(), claims, target, "reset_password"); err != nil {
		Forbidden(w, err.Error())
		return
	}

	password, err := generateTemporaryPassword()
	if err != nil {
		InternalError(w, "generate temporary password failed")
		return
	}
	hash, err := bcrypt.GenerateFromPassword([]byte(password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "reset password failed")
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE users
		    SET password_hash = $1, password_set = true, must_change_password = true,
		        password_changed_at = NOW(), updated_at = NOW()
		  WHERE id = $2 AND tenant_id = $3`,
		string(hash), target.ID, tenantID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "reset password failed")
		return
	}
	if err := revokeUserSessions(r.Context(), target.ID); err != nil {
		slog.Warn("password reset but session revoke failed", "user_id", target.ID, "error", err)
	}
	db.AuditLog(r.Context(), claims.UserID, tenantID, "password_reset",
		"/v1/admin/users/"+target.ID+"/password/reset", "email="+maskEmail(target.Email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{"temporary_password": password})
}

// SetUserStatus POST /v1/admin/users/{id}/disable | /enable
func (h *AdminHandler) SetUserStatus(w http.ResponseWriter, r *http.Request, disable bool) {
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
	action := "enable"
	next := userStatusActive
	if disable {
		action = "disable"
		next = userStatusDisabled
	}
	if err := ensureCanManage(r.Context(), claims, target, action); err != nil {
		Forbidden(w, err.Error())
		return
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE users SET status = $1, updated_at = NOW() WHERE id = $2 AND tenant_id = $3`,
		next, target.ID, tenantID); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, action+" user failed")
		return
	}
	if disable {
		// 禁用必须立刻踢下线：只改状态的话，已签发的 token 还能继续用。
		if err := revokeUserSessions(r.Context(), target.ID); err != nil {
			slog.Warn("user disabled but session revoke failed", "user_id", target.ID, "error", err)
		}
	}
	db.AuditLog(r.Context(), claims.UserID, tenantID, "user_"+action,
		"/v1/admin/users/"+target.ID+"/"+action, "email="+maskEmail(target.Email), r.RemoteAddr, nil)
	OK(w, map[string]string{"status": next})
}

// ForceLogout POST /v1/admin/users/{id}/force-logout
func (h *AdminHandler) ForceLogout(w http.ResponseWriter, r *http.Request) {
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
	if err := ensureCanManage(r.Context(), claims, target, "force_logout"); err != nil {
		Forbidden(w, err.Error())
		return
	}
	if err := revokeUserSessions(r.Context(), target.ID); err != nil {
		InternalError(w, "force logout failed")
		return
	}
	db.AuditLog(r.Context(), claims.UserID, tenantID, "forced_logout",
		"/v1/admin/users/"+target.ID+"/force-logout", "email="+maskEmail(target.Email), r.RemoteAddr, nil)
	OK(w, map[string]string{"status": "revoked"})
}
