package api

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
)

// ── 审计日志异步 worker pool ──
// 避免热路径每个请求启动一个 goroutine，在突发流量下防止 goroutine 堆积
type auditLogTask struct {
	ctx        context.Context
	userID     string
	tenantID   int16
	method     string
	path       string
	remoteAddr string
	status     int
}

var (
	auditLogCh   = make(chan auditLogTask, 4096) // 缓冲通道，防背压时丢弃
	auditLogOnce sync.Once
)

// StartAuditLogWorker 启动审计日志 worker（main.go 中调用一次）
func StartAuditLogWorker(ctx context.Context) {
	auditLogOnce.Do(func() {
		const workerCount = 4
		for i := 0; i < workerCount; i++ {
			go func(id int) {
				slog.Debug("audit log worker started", "id", id)
				for {
					select {
					case <-ctx.Done():
						return
					case task := <-auditLogCh:
						meta := map[string]interface{}{
							"status": task.status,
							"method": task.method,
							"path":   task.path,
						}
						db.AuditLog(task.ctx, task.userID, task.tenantID, task.method, task.path, "", task.remoteAddr, meta)
					}
				}
			}(i)
		}
	})
}

type responseWriter struct {
	http.ResponseWriter
	status  int
	bytes   int
	flusher http.Flusher
}

// ── JWT 黑名单本地 TTL 缓存（P1 优化：减少热路径每次请求的 Redis 往返）──
// 正缓存（已拉黑）15 分钟有效；负缓存（未拉黑）仅 30 秒，确保登出撤销
// 在 ≤30s 内全局生效（多副本仍以 Redis 为最终事实源）。
type jwtBlacklistEntry struct {
	blacklisted bool
	checkedAt   time.Time
}

var jwtBlacklistCache sync.Map // jti -> jwtBlacklistEntry

const (
	jwtBlacklistHitTTL  = 15 * time.Minute
	jwtBlacklistMissTTL = 30 * time.Second
)

// checkJWTBlacklisted 优先查本地缓存，miss 时回源 Redis 并回填。
func checkJWTBlacklisted(ctx context.Context, jti string) (bool, error) {
	if v, ok := jwtBlacklistCache.Load(jti); ok {
		e := v.(jwtBlacklistEntry)
		ttl := jwtBlacklistMissTTL
		if e.blacklisted {
			ttl = jwtBlacklistHitTTL
		}
		if time.Since(e.checkedAt) < ttl {
			return e.blacklisted, nil
		}
	}
	if db.Redis == nil {
		// fail-close（安全优先，用户策略）：Redis 不可用视为无法验证，
		// 返回错误让调用方拒绝请求，而非放行登出后的 token。
		return false, errors.New("jwt blacklist store unavailable (redis not configured)")
	}
	n, err := db.Redis.Exists(ctx, db.RedisKey("jwt:blacklist:")+jti).Result()
	if err != nil {
		return false, err
	}
	jwtBlacklistCache.Store(jti, jwtBlacklistEntry{blacklisted: n > 0, checkedAt: time.Now()})
	return n > 0, nil
}

// markJWTBlacklisted 登出时同步本地正缓存（配合 Redis 写入）。
func markJWTBlacklisted(jti string) {
	if jti == "" {
		return
	}
	jwtBlacklistCache.Store(jti, jwtBlacklistEntry{blacklisted: true, checkedAt: time.Now()})
}

// StartBlacklistCleaner 定期清理过期的 JWT 黑名单本地缓存条目，防止内存泄漏。
// 每 10 分钟扫描一次，删除超过 1 小时未更新的条目。
func StartBlacklistCleaner(ctx context.Context) {
	go func() {
		ticker := time.NewTicker(10 * time.Minute)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				expired := 0
				cutoff := time.Now().Add(-1 * time.Hour)
				jwtBlacklistCache.Range(func(key, value any) bool {
					e := value.(jwtBlacklistEntry)
					if e.checkedAt.Before(cutoff) {
						jwtBlacklistCache.Delete(key)
						expired++
					}
					return true
				})
				if expired > 0 {
					slog.Debug("cleaned expired JWT blacklist cache entries", "count", expired)
				}
			}
		}
	}()
}

// StartBlacklistPubSub 启动 Redis Pub/Sub 监听器，实现跨实例黑名单同步。
// 当任一实例将 token 加入黑名单时，通过 Redis channel 广播给所有其他实例。
func StartBlacklistPubSub(ctx context.Context) {
	if db.Redis == nil {
		slog.Warn("Redis not available, JWT blacklist cross-instance sync disabled")
		return
	}

	go func() {
		pubsub := db.Redis.Subscribe(ctx, db.RedisKey("jwt:blacklist:sync"))
		defer pubsub.Close()

		ch := pubsub.Channel()
		slog.Info("JWT blacklist Pub/Sub listener started")

		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("JWT blacklist Pub/Sub channel closed")
					return
				}
				// 消息格式: "jti"
				jti := msg.Payload
				if jti != "" {
					slog.Debug("received cross-instance blacklist notification", "jti", jti)
					markJWTBlacklisted(jti)
				}
			}
		}
	}()
}

// broadcastBlacklistSync 向其他实例广播 token 黑名单事件
func broadcastBlacklistSync(jti string) {
	if db.Redis == nil || jti == "" {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	if err := db.Redis.Publish(ctx, db.RedisKey("jwt:blacklist:sync"), jti).Err(); err != nil {
		slog.Warn("failed to broadcast JWT blacklist sync", "jti", jti, "error", err)
	}
}

func (rw *responseWriter) WriteHeader(status int) {
	rw.status = status
	rw.ResponseWriter.WriteHeader(status)
}

func (rw *responseWriter) Write(b []byte) (int, error) {
	n, err := rw.ResponseWriter.Write(b)
	rw.bytes += n
	return n, err
}

func (rw *responseWriter) Flush() {
	if rw.flusher != nil {
		rw.flusher.Flush()
	}
}

// Hijack implements http.Hijacker, required for WebSocket upgrades through middleware.
func (rw *responseWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	if hj, ok := rw.ResponseWriter.(http.Hijacker); ok {
		return hj.Hijack()
	}
	return nil, nil, fmt.Errorf("underlying ResponseWriter does not support hijacking")
}

func LoggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		var flusher http.Flusher
		if f, ok := w.(http.Flusher); ok {
			flusher = f
		}
		rw := &responseWriter{ResponseWriter: w, status: http.StatusOK, flusher: flusher}

		next.ServeHTTP(rw, r)

		slog.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rw.status,
			"bytes", rw.bytes,
			"duration", time.Since(start).String(),
			"ip", r.RemoteAddr,
			"trace_id", GetTraceID(r.Context()),
		)

		// Write audit log for non-GET requests (P0-性能: 异步写入，不阻塞响应返回)
		if r.Method != "GET" && r.Method != "OPTIONS" && rw.status < 500 {
			claims := auth.GetClaims(r.Context())
			if claims != nil {
				select {
				case auditLogCh <- auditLogTask{
					ctx:        r.Context(),
					userID:     claims.UserID,
					tenantID:   claims.TenantID,
					method:     r.Method,
					path:       r.URL.Path,
					remoteAddr: r.RemoteAddr,
					status:     rw.status,
				}:
				default:
					slog.Warn("audit log channel full, dropping entry",
						"path", r.URL.Path, "method", r.Method)
				}
			}
		}
	})
}

// TracingMiddleware creates a span for each HTTP request and attaches it to the context.
// Must be placed early in the middleware chain so downstream handlers can use monitor.GetSpan.
// Also injects a trace_id into context for structured log correlation.
func TracingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ctx, span := monitor.StartSpan(r.Context(), r.Method+" "+r.URL.Path, "server")
		span.SetTag("http.method", r.Method)
		span.SetTag("http.path", r.URL.Path)
		span.SetTag("http.remote_addr", r.RemoteAddr)
		// Inject trace_id into context for structured log correlation
		ctx = context.WithValue(ctx, traceIDKey, span.TraceID.String())
		defer span.End()

		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// traceIDKey is a context key type for trace_id to avoid collisions.
type traceIDKeyType struct{}

var traceIDKey = traceIDKeyType{}

// GetTraceID extracts the trace_id from context, if present.
func GetTraceID(ctx context.Context) string {
	if v, ok := ctx.Value(traceIDKey).(string); ok {
		return v
	}
	return ""
}

func RecoverMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defer func() {
			if rec := recover(); rec != nil {
				// http.ErrAbortHandler 是标准库中用于强制中止连接的 sentinel panic，
				// 不应被拦截，否则 http.Server 无法正确关闭连接。
				if rec == http.ErrAbortHandler {
					panic(rec)
				}
				slog.Error("panic recovered", "panic", rec, "path", r.URL.Path)
				InternalError(w, "internal server error")
			}
		}()
		next.ServeHTTP(w, r)
	})
}

// CORSMiddleware 处理 CORS。白名单取自进程内运行时共享源（cors_runtime.go）：
// 启动由 gateway_router 注入 cfg.CORSOrigins，后台保存 cors 分类后本实例与
// 跨副本订阅者热更新；"*" 在 AllowCredentials=true 下违反 CORS 规范且高危，显式拒绝。
func CORSMiddleware() func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			allowed := false
			if origin != "" && corsOriginAllowed(origin) {
				allowed = true
			}
			if allowed {
				w.Header().Set("Access-Control-Allow-Origin", origin)
				w.Header().Set("Vary", "Origin")
				w.Header().Set("Access-Control-Allow-Credentials", "true")
			}
			w.Header().Set("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type,Authorization,X-API-Key")
			w.Header().Set("Access-Control-Max-Age", "86400")

			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// SecurityHeadersMiddleware 注入通用安全响应头。
// CSP_CONNECT_SRC 通过 env 注入（默认留空则不强制 connect-src 白名单，
// 由部署方按生产域名配置，避免 localhost 写死导致生产环境被阻断）。
func SecurityHeadersMiddleware(next http.Handler) http.Handler {
	cspConnectSrc := strings.TrimSpace(os.Getenv("CSP_CONNECT_SRC"))
	if cspConnectSrc == "" {
		cspConnectSrc = "'self'"
	}
	csp := "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
		"img-src 'self' data: blob:; connect-src " + cspConnectSrc + "; " +
		"font-src 'self' https://fonts.gstatic.com; frame-ancestors 'none'"
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("X-XSS-Protection", "1; mode=block")
		w.Header().Set("Content-Security-Policy", csp)
		w.Header().Set("Referrer-Policy", "strict-origin-when-cross-origin")
		w.Header().Set("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
		w.Header().Set("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
		next.ServeHTTP(w, r)
	})
}

// AuthMiddleware validates JWT from cookie or Authorization header.
func AuthMiddleware(a *auth.Authenticator) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tokenStr := ""

			// 1. Cookie (primary for browser clients; SSE 经 withCredentials 携带)
			if c, err := r.Cookie("chiron_token"); err == nil && c.Value != "" {
				tokenStr = c.Value
			}

			// 2. Authorization: Bearer <token> (for API clients)
			//
			// `sk-` 前缀的 Bearer 是**用户 API Key**，不是 JWT：OpenAI SDK 与 curl
			// 只会发 `Authorization: Bearer <key>`，不认自定义头。不接受它，就等于
			// 「兼容层要求客户端多带一个非标准头」—— 那就不叫兼容。
			// JWT 是 `eyJ` 开头的 base64url，与 `sk-` 不可能混淆。
			apiKeyStr := ""
			if tokenStr == "" {
				if ah := r.Header.Get("Authorization"); ah != "" {
					if strings.HasPrefix(ah, "Bearer ") {
						bearer := strings.TrimPrefix(ah, "Bearer ")
						if strings.HasPrefix(bearer, apiKeyPrefix) {
							apiKeyStr = bearer
						} else {
							tokenStr = bearer
						}
					}
				}
			}

			// 3. X-API-Key header（本系统客户端的既有形态）
			if tokenStr == "" && apiKeyStr == "" {
				apiKeyStr = r.Header.Get("X-API-Key")
			}

			// API Key 通道：与 JWT 通道并列，不走 ValidateToken。
			if tokenStr == "" && apiKeyStr != "" {
				if key := apiKeyStr; key != "" {
					// Validate API key against PostgreSQL（含 tenant_id 与 revoked 状态校验，多租户隔离）
					// 同时取回 ak.id：鉴权成功后要按节流回写 last_used_at（见 markAPIKeyUsed）
					var keyID, userID, role, tenantID string
					var keyIssuedAt int64
					keyHash := sha256.Sum256([]byte(key))
					err := db.GlobalDBManager.QueryRow(r.Context(),
						apiKeyAuthQuery,
						hex.EncodeToString(keyHash[:])).Scan(&keyID, &userID, &role, &tenantID, &keyIssuedAt)
					if err == nil {
						// P1-5: tenant_id 为空直接拒绝，不再回退 DefaultTenantID。
						// 历史数据中 tenant_id=NULL 的 user 走 DefaultTenantID 会落到默认租户，
						// 造成跨租户数据访问；多租户部署必须强制每个用户绑定租户。
						if tenantID == "" {
							slog.Warn("API key bound to user with null tenant_id, rejecting",
								"user_id", userID)
							Unauthorized(w, "user has no tenant binding; contact admin")
							return
						}
						// ── 会话纪元检查（与 JWT 分支同一口径，fail-close）──
						//
						// 缺了这一条，`revokeUserSessions` 只对 JWT 有效：改密 / 密码重置 /
						// 管理员踢人之后，此前签发的 **API key 依然能用** —— 而 API key 恰恰是
						// 最长寿、最容易落在 CI / 第三方系统里的那类凭据，泄漏后"改密止血"
						// 对它完全无效。三步撤销（改密 → 全部会话失效）必须把两条凭据通道都覆盖。
						//
						// 比对的是 key 的**签发时刻**而不是 JWT 的 iat：API key 没有 iat。
						// `keyIssuedAt` 由 SQL 按会话时区还原成真实 Unix 秒（见 apiKeyAuthQuery）。
						epoch, epochErr := sessionEpoch(r.Context(), userID)
						if epochErr != nil {
							slog.Warn("session epoch check failed for api key (fail-close)",
								"user_id", userID, "error", epochErr)
							Unauthorized(w, "authentication service unavailable")
							return
						}
						if epoch > 0 && epoch > keyIssuedAt {
							Unauthorized(w, "api key has been revoked")
							return
						}
						// API Key 是长期凭据（不过期或长过期），泄漏面远大于短期 JWT：
						// 这里只授予常规业务权限，剔除管理类权限（admin/users/policy/
						// ent/market/audit）—— 管理操作必须走交互式登录。
						claims := &auth.Claims{
							UserID:   userID,
							Role:     role,
							TenantID: parseTenantID(tenantID),
							Perms:    auth.APIKeyPermissions(role),
						}
						ctx := auth.WithClaims(r.Context(), claims)
						markAPIKeyUsed(keyID)
						next.ServeHTTP(w, r.WithContext(withTenantCtx(ctx, claims)))
						return
					}
					Unauthorized(w, "invalid API key")
					return
				}
			}

			if tokenStr == "" {
				Unauthorized(w, "missing authorization")
				return
			}

			claims, err := a.ValidateToken(tokenStr)
			if err != nil {
				Unauthorized(w, "invalid or expired token")
				return
			}

			// ── 会话纪元检查（改密/密码重置后旧 token 立即失效）──
			// 与黑名单同为 fail-close：Redis 不可用时拒绝请求，避免撤销在故障期间失效。
			// 纪元 > token.iat ⇒ 该 token 签发于撤销之前 ⇒ 拒绝。
			epoch, epochErr := sessionEpoch(r.Context(), claims.UserID)
			if epochErr != nil {
				slog.Warn("session epoch check failed (fail-close)", "error", epochErr)
				Unauthorized(w, "authentication service unavailable")
				return
			}
			if epoch > 0 && claims.IssuedAt != nil && epoch > claims.IssuedAt.Unix() {
				Unauthorized(w, "token has been revoked")
				return
			}

			// ── JWT 黑名单检查（登出后的 token 立即失效）──
			// fail-close：Redis 不可用/检查失败时拒绝请求（安全优先），
			// 避免故障期间登出 token 被复用。
			if claims.ID != "" {
				blacklisted, err := checkJWTBlacklisted(r.Context(), claims.ID)
				if err != nil {
					slog.Warn("jwt blacklist check failed (fail-close)", "error", err)
					Unauthorized(w, "authentication service unavailable")
					return
				}
				if blacklisted {
					Unauthorized(w, "token has been revoked")
					return
				}
			}

			ctx := auth.WithClaims(r.Context(), claims)
			next.ServeHTTP(w, r.WithContext(withTenantCtx(ctx, claims)))
		})
	}
}

// RequirePermission checks that the authenticated user has the specified permission.
func RequirePermission(perm string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			claims := auth.GetClaims(r.Context())
			if !auth.HasPermission(claims, perm) {
				Forbidden(w, "insufficient permissions")
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// RequirePlatformOwner 是**平台级**端点的门禁：既要权限点，也要平台 owner 角色。
//
// 为什么不能只用 RequirePermission：`admin:write` 是**租户**内置 admin 角色就持有的
// 权限（auth.RolePermissions["admin"]，见 internal/auth/jwt.go）。于是
// `POST /v1/admin/database/query`、`DELETE /v1/admin/tenants/{id}` 这类本质上是
// **平台级**的操作，只要租户 admin 一登录就能执行 —— 租户 2 的 admin 可以直接
// `SELECT * FROM users`（拿到全平台 email/password_hash）或删掉租户 1。
//
// 判定口径与 admin.go「只有 owner 能授予 owner 角色」、ent_identity_handler.go:333、
// sso_providers_admin.go:177 一致：平台级操作 = `claims.Role == "owner"`。
// 租户内的管理操作（成员、邀请码、harness 配置）继续走租户权限点，不受影响。
func RequirePlatformOwner(perm string) func(http.Handler) http.Handler {
	permMW := RequirePermission(perm)
	return func(next http.Handler) http.Handler {
		return permMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if !isPlatformOwner(auth.GetClaims(r.Context())) {
				Forbidden(w, "platform owner only")
				return
			}
			next.ServeHTTP(w, r)
		}))
	}
}

// RateLimitMiddleware implements a simple in-memory token bucket per IP.
// For production, this should use Redis (sliding window).
type RateLimiter struct {
	mu       sync.Mutex
	visitors map[string]*visitor
	rpm      int
	stopCh   chan struct{}
}

type visitor struct {
	count   int
	resetAt time.Time
}

func NewRateLimiter(rpm int) *RateLimiter {
	return &RateLimiter{
		visitors: make(map[string]*visitor),
		rpm:      rpm,
		stopCh:   make(chan struct{}),
	}
}

func (rl *RateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ip := r.RemoteAddr
		if idx := strings.LastIndex(ip, ":"); idx >= 0 {
			ip = ip[:idx]
		}

		rl.mu.Lock()
		v, exists := rl.visitors[ip]
		now := time.Now()

		if !exists || now.After(v.resetAt) {
			rl.visitors[ip] = &visitor{count: 1, resetAt: now.Add(1 * time.Minute)}
			rl.mu.Unlock()
			next.ServeHTTP(w, r)
			return
		}

		if v.count >= rl.rpm {
			// 这个桶的窗口长度是**已知的**（`resetAt`），所以能给出真实等待秒数，
			// 而不是让客户端猜。原样重发在窗口重置前必然再被拒。
			wait := int(math.Ceil(time.Until(v.resetAt).Seconds()))
			rl.mu.Unlock()
			TooManyRequestsAfterSeconds(w, wait)
			return
		}

		v.count++
		rl.mu.Unlock()
		next.ServeHTTP(w, r)
	})
}

// CleanupVisitors periodically removes stale entries.
func (rl *RateLimiter) CleanupVisitors(interval time.Duration) {
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		defer func() {
			if r := recover(); r != nil {
				slog.Error("rate limiter cleanup panic", "panic", r)
			}
		}()
		for {
			select {
			case <-ticker.C:
				rl.mu.Lock()
				now := time.Now()
				for ip, v := range rl.visitors {
					if now.After(v.resetAt) {
						delete(rl.visitors, ip)
					}
				}
				rl.mu.Unlock()
			case <-rl.stopCh:
				return
			}
		}
	}()
}

// Stop cleanly terminates the cleanup goroutine.
func (rl *RateLimiter) Stop() {
	close(rl.stopCh)
}

// MonitoringMiddleware tracks request counts and request latency.
//
// 除了原有的累计计数与直方图，这里还把「时间戳 + 状态码 + 耗时」写进
// monitor.RequestWindowStats —— 后台「运行时监控」页的「近 N 分钟请求量 / 错误率 /
// P50/P95/P99」全部来自这份窗口采样。没有状态码就没有错误率；没有时间戳就没有
// 「近 15 分钟」这个限定（旧直方图只有最近 N 条，窗口是猜的）。
func MonitoringMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		monitor.IncRequests()
		start := time.Now()
		// 路由分类在**请求开始时**取定（r.URL.Path 之后不会被改写），供分钟聚合
		// 按 route_class 分组；低基数，见 routeClassOf。
		class := routeClassOf(r.URL.Path)
		sw := &monitorStatusWriter{ResponseWriter: w, status: http.StatusOK}
		defer func() {
			monitor.DecRequests()
			d := time.Since(start)
			monitor.RequestHistogram.Record(d)
			monitor.RequestWindowStats.RecordClass(class, d, sw.status)
		}()
		// panic 的请求实际回给客户端的是 500（由外层 RecoverMiddleware 写出），
		// 但此刻 sw 还没写过状态码。先把它记成 500 再放行 panic，
		// 否则错误率会漏掉最严重的那一类请求。
		defer func() {
			if rec := recover(); rec != nil {
				sw.status = http.StatusInternalServerError
				panic(rec)
			}
		}()
		next.ServeHTTP(sw, r)
	})
}

// monitorStatusWriter 只做一件事：记住最终写出的状态码。
//
// 不直接断言底层是 *responseWriter：中间件顺序会变（LoggingMiddleware 在最外层，
// 但单测与其它入口可能单独挂 MonitoringMiddleware），依赖具体类型会在重组时
// 静默退化成「永远 200、错误率恒为 0」—— 那正是本页要避免的假数据。
type monitorStatusWriter struct {
	http.ResponseWriter
	status int
	wrote  bool
}

func (w *monitorStatusWriter) WriteHeader(code int) {
	if !w.wrote {
		w.status = code
		w.wrote = true
	}
	w.ResponseWriter.WriteHeader(code)
}

func (w *monitorStatusWriter) Write(b []byte) (int, error) {
	if !w.wrote {
		w.status = http.StatusOK
		w.wrote = true
	}
	return w.ResponseWriter.Write(b)
}

// Flush 透传：SSE 端点依赖它，被中间件包一层后不能丢。
func (w *monitorStatusWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// Hijack 透传：WebSocket 升级依赖它。
func (w *monitorStatusWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	if hj, ok := w.ResponseWriter.(http.Hijacker); ok {
		return hj.Hijack()
	}
	return nil, nil, fmt.Errorf("underlying ResponseWriter does not support hijacking")
}

// Unwrap 让 http.NewResponseController 能穿透到原始 ResponseWriter。
func (w *monitorStatusWriter) Unwrap() http.ResponseWriter {
	return w.ResponseWriter
}
