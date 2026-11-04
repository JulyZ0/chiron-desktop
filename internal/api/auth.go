package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
	"golang.org/x/crypto/bcrypt"
)

const tokenCookieName = "chiron_token"

// P0-4: Email 鑴辨晱鍑芥暟锛屼繚鐣欏墠3瀛楃+@+鍩熷悕棣栧瓧绗︼紝鍏朵綑鐢?**
func maskEmail(email string) string {
	at := strings.Index(email, "@")
	if at < 1 {
		return "***"
	}
	local := email[:at]
	domain := email[at+1:]
	dot := strings.Index(domain, ".")
	if dot < 1 {
		return "***@" + domain
	}
	if len(local) <= 3 {
		return local[:1] + "***@" + domain[:1] + "***" + domain[dot:]
	}
	return local[:3] + "***@" + domain[:1] + "***" + domain[dot:]
}

// maskPhone 保留前 3 位与后 2 位，中间打码（如 138****12）。
// 与 maskEmail 同为日志脱敏：审计流水里出现完整手机号，等于把用户 PII 长期留痕。
func maskPhone(phone string) string {
	if len(phone) < 7 {
		return "***"
	}
	return phone[:3] + "****" + phone[len(phone)-2:]
}

// DefaultTenantID is the default tenant ID for single-tenant mode.
const DefaultTenantID = db.DefaultTenantID

type AuthHandler struct {
	auth    *auth.Authenticator
	cfg     *config.Config
	// emailBloom 可选：注入后注册流程先用布隆过滤器快速否定"该邮箱未注册"，
	// 从而跳过昂贵的 DB 判重。nil 表示未启用（始终回落 DB 查证）。
	emailBloom *userEmailBloom
}

func NewAuthHandler(cfg *config.Config) *AuthHandler {
	return &AuthHandler{
		auth: auth.NewAuthenticator(cfg.JWTSecret, cfg.JWTExpiration),
		cfg:  cfg,
	}
}

// SetEmailBloomHandler injects the registration email bloom filter.
// 传 nil 表示关闭该优化（注册流程始终回落 DB 判重）。
func (h *AuthHandler) SetEmailBloomHandler(b *userEmailBloom) {
	h.emailBloom = b
}

type LoginRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

type UserResponse struct {
	ID    string `json:"id"`
	Email string `json:"email"`
	Name  string `json:"name"`
	Role  string `json:"role"`
}

// SetTokenCookie sets the JWT as an HTTP-only secure cookie.
func SetTokenCookie(w http.ResponseWriter, token string, maxAge int, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     tokenCookieName,
		Value:    token,
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   maxAge,
	})
}

func ClearTokenCookie(w http.ResponseWriter, secure bool) {
	http.SetCookie(w, &http.Cookie{
		Name:     tokenCookieName,
		Value:    "",
		Path:     "/",
		HttpOnly: true,
		Secure:   secure,
		SameSite: http.SameSiteLaxMode,
		MaxAge:   -1,
	})
}

// logLoginFailure 统一的登录失败审计日志+验证码记录
func (h *AuthHandler) logLoginFailure(ctx context.Context, r *http.Request, email string, tenantID int16, remoteAddr string) {
	db.AuditLog(ctx, "", tenantID, "login_failed", "/v1/auth/login", "email="+maskEmail(email), remoteAddr, nil)
}

func (h *AuthHandler) Login(w http.ResponseWriter, r *http.Request) {
	var req LoginRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if req.Email == "" || req.Password == "" {
		BadRequest(w, "email and password are required")
		return
	}
	if len(req.Email) > 255 {
		BadRequest(w, "email too long")
		return
	}
	if len(req.Password) > 128 {
		BadRequest(w, "password too long")
		return
	}

	// No dev bypass — always validate against DB
	ctx := r.Context()

	// 设置租户上下文以绕过 RLS
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "login failed")
		return
	}
	defer tx.Rollback(ctx)

	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(DefaultTenantID)); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "login failed")
		return
	}

	var user UserResponse
	var passwordHash string
	var tenantID int16
	// F2：不再用 `tenant_id = DefaultTenantID` 过滤 —— 经邀请码注册的用户属于别的
	// 租户，硬过滤会让它们永远登不进来。改为按 email 取用户，并在同 email 跨租户
	// 重名时优先默认租户（存量账号行为不变）。
	err = tx.QueryRow(ctx,
		`SELECT id, email, name, role, tenant_id, password_hash
		   FROM users
		  WHERE email = $1
		  ORDER BY (tenant_id = $2) DESC, created_at ASC
		  LIMIT 1`,
		req.Email, DefaultTenantID,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role, &tenantID, &passwordHash)
	if err != nil {
		slog.Warn("login failed", "email", req.Email, "error", err)
		h.logLoginFailure(r.Context(), r, req.Email, DefaultTenantID, r.RemoteAddr)
		Unauthorized(w, "invalid email or password")
		return
	}

	if err := bcrypt.CompareHashAndPassword([]byte(passwordHash), []byte(req.Password)); err != nil {
		h.logLoginFailure(r.Context(), r, req.Email, tenantID, r.RemoteAddr)
		Unauthorized(w, "invalid email or password")
		return
	}

	// multi-tenant isolation
	// P1-5: tenant_id 为空直接拒绝登录
	if tenantID == 0 {
		slog.Warn("login rejected: user has null tenant_id", "user_id", user.ID)
		Unauthorized(w, "user has no tenant binding; contact admin")
		return
	}
	// C1-4：权限优先取 tenant_members（角色真相），查不到回退 auth.RolePermissions。
	token, err := h.auth.GenerateToken(user.ID, user.Email, user.Role, tenantID,
		resolveUserPermsDefault(r.Context(), user.ID, user.Role))
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	db.AuditLog(r.Context(), user.ID, tenantID, "login_success", "/v1/auth/login", "email="+maskEmail(req.Email), r.RemoteAddr, nil)
	OK(w, map[string]interface{}{
		"token": token,
		"user":  user,
	})
}

type RegisterRequest struct {
	Email    string `json:"email"`
	Password string `json:"password"`
	Name     string `json:"name"`
}

func (h *AuthHandler) Register(w http.ResponseWriter, r *http.Request) {
	if h.cfg.DisableRegistration {
		Forbidden(w, "registration is disabled on this instance")
		return
	}

	var req RegisterRequest
	if err := DecodeJSON(w, r, &req); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}

	if req.Email == "" || req.Password == "" || req.Name == "" {
		BadRequest(w, "email, password, and name are required")
		return
	}
	if ok, msg := auth.ValidatePasswordComplexity(req.Password); !ok {
		BadRequest(w, msg)
		return
	}
	if len(req.Email) > 255 || len(req.Name) > 128 {
		BadRequest(w, "email or name too long")
		return
	}

	ctx := r.Context()

	// 设置租户上下文（SET LOCAL）：必须在事务中才持续生效
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "registration failed")
		return
	}
	defer tx.Rollback(ctx)

	// 本地单机形态：所有账号都落在默认租户，不再有"邀请码换租户"的路径。
	tenantID := DefaultTenantID

	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(tenantID)); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "registration failed")
		return
	}

	// Check for existing user
	//
	// 布隆过滤器前置快速否定：**仅当**它断言"该租户下这个邮箱一定不存在"时才跳过
	// 这次 DB 判重 —— users 上 email 无索引时这是一次全表扫描，也是注册热路径上
	// 最贵的一步。只要判定为"可能存在"（含预热未完成、跨实例广播滞后的保守态），
	// 就照常查库：唯一索引始终是真相源，过滤器出错最多让我们多查一次库。
	if h.emailBloom.MightExist(tenantStr(tenantID), req.Email) {
		var exists int
		if err := tx.QueryRow(ctx,
			`SELECT COUNT(*) FROM users WHERE email = $1 AND tenant_id = $2`,
			req.Email, tenantID,
		).Scan(&exists); err != nil {
			slog.Error("check existing user", "error", err)
			InternalError(w, "registration failed")
			return
		}
		if exists > 0 {
			// 用户枚举缓解：不再回 "email already registered" —— 那等于给出"该邮箱已注册"
			// 的确定性答案，可被批量用来探测账号。改成与其它注册失败同形的模糊提示，
			// 真实原因只进结构化日志。注册接口本身另有验证码 + 限流兜底。
			slog.Info("registration rejected: email already exists", "tenant", tenantID)
			BadRequest(w, "registration could not be completed")
			return
		}
	}

	// 首个注册用户成为系统管理员（owner）：以 users 表是否为空判定；
	// 咨询锁保证并发注册时只有一个请求能成为 owner，其余按普通用户落库。
	if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtext('chiron_first_user'))`); err != nil {
		slog.Error("acquire first-user lock", "error", err)
		InternalError(w, "registration failed")
		return
	}
	var userTotal int
	if err := tx.QueryRow(ctx, `SELECT COUNT(*) FROM users`).Scan(&userTotal); err != nil {
		slog.Error("count users for first-user check", "error", err)
		InternalError(w, "registration failed")
		return
	}
	role := "user"
	if userTotal == 0 {
		role = "owner"
		slog.Info("first registered user becomes owner", "email", req.Email)
	}

	hash, err := bcrypt.GenerateFromPassword([]byte(req.Password), auth.BcryptCost)
	if err != nil {
		InternalError(w, "registration failed")
		return
	}

	// 使用 PostgreSQL 的 gen_random_uuid() 生成 UUID
	var userID string
	err = tx.QueryRow(ctx,
		`INSERT INTO users (id, tenant_id, email, name, password_hash, role, created_at, updated_at)
		 VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, NOW(), NOW())
		 RETURNING id`,
		tenantID, req.Email, req.Name, string(hash), role,
	).Scan(&userID)
	if err != nil {
		// 唯一索引兜底：布隆过滤器同步滞后（预热未完成 / 广播延迟 / 实例刚重启）时，
		// 重复邮箱会走到这里被拦下。返回与"邮箱已存在"同形的模糊提示以避免用户枚举。
		// users 的 INSERT 除 (tenant_id, email) 外没有会冲突的唯一列（id 用
		// gen_random_uuid、其余唯一列为空），所以 23505 即等价于邮箱冲突。
		if isUniqueViolation(err) {
			slog.Info("registration rejected: email already exists (unique index)", "tenant", tenantID)
			BadRequest(w, "registration could not be completed")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "registration failed")
		return
	}

	// 鎻愪氦浜嬪姟
	if err := tx.Commit(ctx); err != nil {
		slog.Error("commit tx", "error", err)
		InternalError(w, "registration failed")
		return
	}

	// 登记到布隆过滤器并广播给其他副本。放在 commit 之后是有意为之：只有真正落库的
	// 邮箱才值得登记；若在 commit 前登记而事务回滚，会留下一个永久的假阳性 —— 无害
	// 但降低过滤器的过滤率。广播失败同样无害（其他副本只是回落 DB 多查一次）。
	if h.emailBloom != nil {
		h.emailBloom.Add(tenantStr(tenantID), req.Email)
		broadcastUserEmailBloomAdd(tenantStr(tenantID), req.Email)
	}

	token, err := h.auth.GenerateToken(userID, req.Email, role, tenantID,
		resolveUserPermsDefault(r.Context(), userID, role))
	if err != nil {
		InternalError(w, "authentication failed")
		return
	}

	// 新用户引导：为这个账号建好三步的 pending 行（P0-3）。
	//
	// 放在 commit **之后**：引导进度写不进去不能让一个已经合法的账号注册失败。
	// 存量用户（没有这一步之前注册的）因此不会在该表里留下行 ⇒ 前端判定
	// eligible=false，不弹引导，符合 roadmap「不打断已有用户」。
	StartOnboarding(ctx, userID)

	SetTokenCookie(w, token, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	Created(w, map[string]interface{}{
		"token": token,
		"user":  UserResponse{ID: userID, Email: req.Email, Name: req.Name, Role: role},
	})
}

func (h *AuthHandler) Logout(w http.ResponseWriter, r *http.Request) {
	// 鈹€鈹€ JWT 榛戝悕鍗曪細灏嗚 token 鍔犲叆 Redis 榛戝悕鍗曪紝TTL 绛変簬鍓╀綑鏈夋晥鏈?鈹€鈹€
	if claims := auth.GetClaims(r.Context()); claims != nil && claims.ID != "" && db.Redis != nil {
		remaining := time.Until(claims.ExpiresAt.Time)
		if remaining > 0 {
			db.Redis.Set(r.Context(), db.RedisKey("jwt:blacklist:")+claims.ID, "1", remaining)
			// P0-1: 璺ㄥ疄渚嬪悓姝ラ粦鍚嶅崟
			broadcastBlacklistSync(claims.ID)
		}
	}
	// 同步本地缓存，确保本实例后续请求立即拒绝该 token
	if claims := auth.GetClaims(r.Context()); claims != nil {
		markJWTBlacklisted(claims.ID)
	}
	ClearTokenCookie(w, h.cfg.CookieSecure)
	OK(w, map[string]string{"message": "logged out"})
}

func (h *AuthHandler) Profile(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var settings map[string]interface{}
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(settings::jsonb, '{}'::jsonb) FROM users WHERE id = $1`, claims.UserID).Scan(&settings); err != nil {
		settings = map[string]interface{}{}
	}
	OK(w, map[string]interface{}{
		"user_id":  claims.UserID,
		"email":    claims.Email,
		"role":     claims.Role,
		"perms":    claims.Perms,
		"settings": settings,
	})
}

// UpdateProfile updates the authenticated user's profile (name/email).
func (h *AuthHandler) UpdateProfile(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	var body struct {
		Email    string                 `json:"email"`
		Name     string                 `json:"name"`
		Settings map[string]interface{} `json:"settings"` // 鑷畾涔夋崲鑲ょ瓑鐢ㄦ埛璁剧疆锛堝眬閮ㄥ悎骞讹級
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Email == "" && body.Name == "" && body.Settings == nil {
		BadRequest(w, "no fields to update")
		return
	}

	setClauses := ""
	args := []interface{}{}
	argIdx := 1
	if body.Email != "" {
		setClauses += fmt.Sprintf("email = $%d, ", argIdx)
		args = append(args, body.Email)
		argIdx++
	}
	if body.Name != "" {
		setClauses += fmt.Sprintf("name = $%d, ", argIdx)
		args = append(args, body.Name)
		argIdx++
	}
	if body.Settings != nil {
		settingsJSON, err := json.Marshal(body.Settings)
		if err != nil {
			InternalError(w, "invalid settings")
			return
		}
		// 局部合并：settings = settings || $n
		setClauses += fmt.Sprintf("settings = COALESCE(settings::jsonb, '{}'::jsonb) || $%d::jsonb, ", argIdx)
		args = append(args, string(settingsJSON))
		argIdx++
	}
	setClauses = strings.TrimSuffix(setClauses, ", ")
	args = append(args, claims.UserID)

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE users SET %s, updated_at = NOW() WHERE id = $%d", setClauses, argIdx),
		args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update profile failed")
		return
	}

	OK(w, map[string]string{"status": "updated"})
}

type RefreshRequest struct {
	Token string `json:"token"`
}

// Session returns the current session from the httpOnly cookie.
func (h *AuthHandler) Session(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(tokenCookieName)
	if err != nil || cookie.Value == "" {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	claims, err := h.auth.ValidateToken(cookie.Value)
	if err != nil || claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	ctx := r.Context()
	// 租户取 token 里的（Refresh 会按 users 表重取后重签，所以这里不会用到过期的归属）。
	// 此前这里硬编码 DefaultTenantID：`WHERE id=$1 AND tenant_id=$2` 于是恒不匹配
	// 非默认租户的用户 —— 他们每次刷新页面都 401，且 set_config 也把 RLS 上下文
	// 指到了别的租户。
	tenantID := claims.TenantID
	if tenantID == 0 {
		tenantID = DefaultTenantID
	}
	tx, err := db.GlobalDBManager.Begin(ctx)
	if err != nil {
		slog.Error("begin tx for tenant context", "error", err)
		InternalError(w, "session lookup failed")
		return
	}
	defer tx.Rollback(ctx)
	if _, err := tx.Exec(ctx, "SELECT set_config('app.current_tenant_id', $1, true)", tenantStr(tenantID)); err != nil {
		slog.Error("set tenant context", "error", err)
		InternalError(w, "session lookup failed")
		return
	}

	var user UserResponse
	if err := tx.QueryRow(ctx,
		`SELECT id, email, name, role FROM users WHERE id = $1 AND tenant_id = $2`,
		claims.UserID, tenantID,
	).Scan(&user.ID, &user.Email, &user.Name, &user.Role); err != nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	OK(w, map[string]interface{}{
		"token": cookie.Value,
		"user":  user,
	})
}

func (h *AuthHandler) Refresh(w http.ResponseWriter, r *http.Request) {
	cookie, err := r.Cookie(tokenCookieName)
	if err != nil {
		Unauthorized(w, "not authenticated")
		return
	}

	// 1) 校验 token 未被加入黑名单
	oldClaims, err := h.auth.ValidateToken(cookie.Value)
	if err != nil {
		Unauthorized(w, "session expired")
		return
	}
	if oldClaims.ID != "" && db.Redis != nil {
		if n, err := db.Redis.Exists(r.Context(), db.RedisKey("jwt:blacklist:")+oldClaims.ID).Result(); err == nil && n > 0 {
			Unauthorized(w, "token revoked")
			return
		}
	}

	// 2) 校验用户仍然存在，并**重取最新角色/租户**。
	//    此前直接 RefreshToken(旧 token) 会原样复制旧 claims 的 role/permissions，
	//    于是管理员被降权后，只要在旧 token 有效期内刷新，权限就被「续期」——
	//    降权不生效（P1）。这里改为从 users 表读当前 role 与 tenant_id 后重新签发。
	var dbEmail, dbRole string
	var dbTenant int16
	err = db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT COALESCE(email, ''), COALESCE(role, ''), COALESCE(tenant_id, 0)
		   FROM users WHERE id = $1`,
		oldClaims.UserID).Scan(&dbEmail, &dbRole, &dbTenant)
	if err != nil {
		Unauthorized(w, "user not found")
		return
	}
	if dbTenant == 0 {
		dbTenant = DefaultTenantID
	}

	// 权限按**当前角色**重新计算，而非沿用旧 token 的快照。
	newToken, err := h.auth.GenerateToken(
		oldClaims.UserID, dbEmail, dbRole, dbTenant,
		resolveUserPermsDefault(r.Context(), oldClaims.UserID, dbRole))
	if err != nil {
		Unauthorized(w, "session expired")
		return
	}

	SetTokenCookie(w, newToken, int(h.cfg.JWTExpiration.Seconds()), h.cfg.CookieSecure)
	OK(w, map[string]string{"message": "token refreshed"})
}

func generateID() string {
	return id.NextID()
}
