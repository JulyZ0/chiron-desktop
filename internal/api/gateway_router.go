package api

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/athenavi/chiron/internal/session"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/athenavi/chiron/internal/storage"
)

func metricsAuthMW(cfg *config.Config, authMW routeMiddleware, h http.HandlerFunc) http.Handler {
	if cfg == nil || cfg.MetricsToken == "" {
		return authMW(RequirePermission(auth.PermAdminRead)(h))
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte("Bearer "+cfg.MetricsToken)) == 1 {
			h(w, r)
			return
		}
		authMW(RequirePermission(auth.PermAdminRead)(h)).ServeHTTP(w, r)
	})
}

// sessionCancels tracks running session contexts for cancellation support.
//
// ⚠️ 值必须是 ***sessionCancel**（指针）。`sessionCancel` 里有 `cancel` 函数字段，
// 函数不可比较 —— 存值类型会让 `sync.Map.CompareAndDelete` 直接 panic
// （`sync: comparing non-comparable value`），而取消路径正是靠它做原子认领的。
var sessionCancels sync.Map

// sessionCancel tracks the owner and cancel function of a running session task.
type sessionCancel struct {
	userID string
	cancel context.CancelFunc
}

// routeMiddleware is a middleware wrapper used by route registration helpers.
type routeMiddleware func(http.Handler) http.Handler

// middlewareChain wraps an http.Handler with a list of middleware functions.
func middlewareChain(h http.Handler, mws ...func(http.Handler) http.Handler) http.Handler {
	for i := len(mws) - 1; i >= 0; i-- {
		h = mws[i](h)
	}
	return h
}

// requestIDHeader generates a lightweight request ID.
func requestIDHeader(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var buf [8]byte
		if _, err := rand.Read(buf[:]); err != nil {
			// 极端回退：rand 失败用时间纳秒填充
			nano := time.Now().UnixNano()
			for i := range buf {
				buf[i] = byte(nano >> (i * 8))
			}
		}
		id := hex.EncodeToString(buf[:])
		r.Header.Set("X-Request-ID", id)
		w.Header().Set("X-Request-ID", id)
		next.ServeHTTP(w, r)
	})
}

// realIPHeader extracts the real IP from X-Forwarded-For / X-Real-IP,
// but ONLY when the direct peer is a trusted reverse proxy (P1 修复)：
// 无条件信任客户端可伪造的 XFF 会绕过按 IP 的限流与验证码失败升级。
// isTrustedProxyPeer 判断请求来源是否在可信代理网段内（与 realIPHeader 同一套判定）。
// 用于决定是否采信客户端提供的 Host / X-Forwarded-* 头。
//
// 注意：TRUSTED_PROXY_CIDRS 为空 ⇒ 一律返回 false（fail-close）—— 不配置就等于
// "没有可信代理"，此时不采信任何由客户端声明的来源信息。
func isTrustedProxyPeer(remoteAddr string, trustedCIDRs []string) bool {
	if len(trustedCIDRs) == 0 {
		return false
	}
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		host = remoteAddr
	}
	ip := net.ParseIP(strings.TrimSpace(host))
	if ip == nil {
		return false
	}
	for _, c := range trustedCIDRs {
		if _, n, err := net.ParseCIDR(strings.TrimSpace(c)); err == nil && n.Contains(ip) {
			return true
		}
	}
	return false
}

func realIPHeader(trustedCIDRs []string) func(http.Handler) http.Handler {
	var trusted []*net.IPNet
	for _, c := range trustedCIDRs {
		if _, n, err := net.ParseCIDR(strings.TrimSpace(c)); err == nil {
			trusted = append(trusted, n)
		}
	}
	peerTrusted := func(remoteAddr string) bool {
		if len(trusted) == 0 {
			return false
		}
		host, _, err := net.SplitHostPort(remoteAddr)
		if err != nil {
			host = remoteAddr
		}
		ip := net.ParseIP(strings.TrimSpace(host))
		if ip == nil {
			return false
		}
		for _, n := range trusted {
			if n.Contains(ip) {
				return true
			}
		}
		return false
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if peerTrusted(r.RemoteAddr) {
				if fwd := r.Header.Get("X-Forwarded-For"); fwd != "" {
					if idx := strings.Index(fwd, ","); idx >= 0 {
						r.RemoteAddr = strings.TrimSpace(fwd[:idx])
					} else {
						r.RemoteAddr = strings.TrimSpace(fwd)
					}
				} else if realIP := r.Header.Get("X-Real-IP"); realIP != "" {
					r.RemoteAddr = realIP
				}
			}
			next.ServeHTTP(w, r)
		})
	}
}

// NewGatewayRouter creates a pure gateway router that proxies all business logic to Python.
// lifecycleCtx 用于控制内部后台协程（tenantResMgr / sessionmap 等）的优雅关闭。
func NewGatewayRouter(
	lifecycleCtx context.Context,
	cfg *config.Config,
	pythonClient *engine.PythonClient,
	eventHub *broadcast.Hub,
	sessionMgr *session.Manager,
	fileStore *storage.AtomicStore,
	atomicRedis *db.AtomicRedis,
	rpaHub *RPAHub,
) http.Handler {
	mux := http.NewServeMux()

	// 运行时 CORS 白名单注入（批 B-2′）：CORSMiddleware / checkWebSocketOrigin / billing 统一读该共享源
	SetCORSAllowOrigin(cfg.CORSOrigins)

	// SSE 发布管线指标（B2）：hub 改为异步发布后，`fallback > 0` 表示队列曾被写满
	// （Redis 慢或事件突发，此时回退同步发布、调用方被阻塞），`queued` 持续高位表示
	// 投递跟不上生产。这两项是背压的直接信号，接入 /metrics 与 /v1/system/metrics。
	if eventHub != nil {
		monitor.RegisterExtraStats(func() map[string]interface{} {
			s := eventHub.Stats()
			return map[string]interface{}{
				"sse_publish_enqueued":  s.Enqueued,
				"sse_publish_delivered": s.Delivered,
				"sse_publish_fallback":  s.Fallback,
				"sse_publish_queued":    s.Queued,
			}
		})
	}

	// Rate limiter — 当 Redis 可用时使用分布式限流器。
	// 批 B-1′（令牌桶重构）：global/tenant/user 三级桶均为“每分钟配额（总量）”，
	// 与网关副本数无关——不再按 RATE_LIMIT_INSTANCES 放大，天然适配多副本。
	// 生产策略：
	//   - Redis 可用：用分布式限流器（推荐）
	//   - Redis 不可用 + 生产环境（cfg.RateLimitFailClose=true）：写操作拒绝
	//   - Redis 不可用 + 开发/测试：降级内存限流（单实例有效，多副本失效）
	var rlMW func(http.Handler) http.Handler
	var distLimiter *DistributedRateLimiter
	rateLimitRPM := cfg.RateLimitRPM
	if rateLimitRPM <= 0 {
		slog.Warn("RateLimitRPM is 0 or unset, using default 60 RPM")
		rateLimitRPM = 60
	}
	// 缺省上限（总量语义）：global = RATE_LIMIT_GLOBAL（后台 rate_limit.global 保存后覆盖），
	// tenant = global/10（下限不高于 rateLimitRPM），user = rateLimitRPM。
	globalLimit := cfg.RateLimitGlobal
	if globalLimit <= 0 {
		globalLimit = rateLimitRPM
	}
	tenantLimit := globalLimit / 10
	if tenantLimit < rateLimitRPM {
		tenantLimit = rateLimitRPM
	}
	if atomicRedis != nil {
		distLimiter = NewDistributedRateLimiter(
			atomicRedis.LoadRaw(),
			globalLimit,  // 全局：每分钟配额（总量）
			tenantLimit,  // 租户：每分钟配额
			rateLimitRPM, // 用户：每分钟配额
		)
		rlMW = DistributedRateLimitMiddleware(distLimiter)
		slog.Info("distributed token-bucket rate limiter enabled",
			"global", globalLimit, "tenant", tenantLimit, "user", rateLimitRPM)
	} else if cfg.RateLimitFailClose {
		// 生产 fail-close：只读放行，写操作拒绝
		rlMW = func(next http.Handler) http.Handler {
			return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != http.MethodGet && r.Method != http.MethodHead {
					http.Error(w, "rate limiter unavailable (redis down)", http.StatusServiceUnavailable)
					return
				}
				next.ServeHTTP(w, r)
			})
		}
		slog.Warn("Redis unavailable + fail-close enabled: write operations rejected")
	} else {
		// 开发/测试降级：内存限流（单实例有效，多副本部署下应改用 Redis）
		rateLimiter := NewRateLimiter(rateLimitRPM)
		rateLimiter.CleanupVisitors(5 * time.Minute)
		rlMW = rateLimiter.Middleware
		slog.Warn("local rate limiter enabled (no Redis); not safe for multi-replica production")
	}

	// Input sanitizer (prompt injection protection)
	inputSanitizer := NewInputSanitizer()
	sanitizeMW := SanitizeMiddleware(inputSanitizer)

	publicMW := func(next http.Handler) http.Handler {
		return middlewareChain(next,
			RecoverMiddleware,
			TracingMiddleware,
			LoggingMiddleware,
			SecurityHeadersMiddleware,
			CORSMiddleware(),
			MonitoringMiddleware,
			requestIDHeader,
			realIPHeader(cfg.TrustedProxyCIDRs),
			SanitizeResponseMiddleware, // P0-4: 敏感信息脱敏
		)
	}

	// Auth
	authenticator := auth.NewAuthenticator(cfg.JWTSecret, cfg.JWTExpiration)
	authHandler := NewAuthHandler(cfg)
	authMW := AuthMiddleware(authenticator)

	// 注册邮箱判重布隆过滤器（按租户分片）：默认开启，可用 EMAIL_BLOOM_FILTER_ENABLED=false 关闭。
	// 它只做快速否定以跳过 DB 判重；预热在后台异步进行，完成前一律回落 DB，
	// 真相源始终是 users(tenant_id, email) 唯一索引。
	if cfg.EmailBloomEnabled {
		emailBloom := newUserEmailBloom(cfg.EmailBloomCapacity, cfg.EmailBloomFPRate)
		authHandler.SetEmailBloomHandler(emailBloom)
		go emailBloom.Warmup(lifecycleCtx, pgBloomQuerier{})
		StartUserEmailBloomSubscriber(lifecycleCtx, emailBloom)
	}

	// Agent execution semaphore — global concurrency limit
	agentSem := NewSharedSemaphore(atomicRedis, "agent", cfg.AgentMaxConcurrency)

	// Submit handler (proxies to Python)
	submitHandler := NewSubmitHandler(pythonClient, sessionMgr, eventHub, cfg)

	// Plugins (MCP server config management)
	pluginHandler := NewPluginHandler(cfg, authenticator)

	// Search (auth + rate limited)
	searchHandler := NewSearchHandler()

	// Conversation
	conversationHandler := NewConversationHandler(authenticator, sessionMgr)
	shareHandler := NewShareHandler(authenticator, sessionMgr, cfg)

	// Tool (proxies to Python)
	toolHandler := NewToolHandler(pythonClient, authenticator)

	// System
	systemHandler := NewSystemHandlerWithEngine(pythonClient)

	// Media
	mediaHandler := NewMediaHandler(fileStore, authenticator)
	mediaHandler.SetMediaRoot(cfg.StorageRoot + "/media")

	// 通用分片上传（断点续传）
	uploadHandler := NewUploadHandler(authenticator, cfg.StorageRoot, fileStore)
	uploadHandler.RegisterRoutes(mux, authMW, rlMW)

	// 模型路由：对话可用模型列表
	mux.Handle("GET /v1/models", authMW(rlMW(http.HandlerFunc(ListUserModels))))

	// 新用户引导（P0-3）：三步进度读写。
	onboardingHandler := NewOnboardingHandler()

	// 用户级 API Key 自助（P1-3 / roadmap §11.3）：api_keys 表早已在鉴权链路里，
	// 但此前没有任何写路径 —— 用户只能手工插表才能拿到 key。
	apiKeyHandler := NewAPIKeyHandler()

	// 后台「支付配置」保存在 system_settings.payment 的凭据叠加到 env 之上，
	// 使页面里配置的渠道在重启后依然生效（DB 值优先、env 兜底）。
	var settingsStore *settings.Store
	if db.Pool != nil {
		settingsStore = settings.New(db.Pool, cfg.AppSecret)
	}
	// 后台已保存的存储配置在启动时应用一次（env 初始化之后、订阅之前）：
	// 「后台改过存储后端」的环境重启后应沿用该配置，而非回退 env。
	applyStorageFromSettings(context.Background(), settingsStore, fileStore)

	// 系统设置变更订阅（批 B-2′）：rate_limit / cors / storage 跨副本即时热更。
	StartSettingsSubscriber(lifecycleCtx, atomicRedis, distLimiter, settingsStore, fileStore)

	// Skill handler (proxies to Python)
	skillHandler := NewSkillHandler(pythonClient)

	// 工具授权模式（ask/auto/yolo）不在这里持有状态：它是前端的实时状态，随每次提交经
	// llm_config.tools_mode 携带，引擎侧校验后用于工具裁决（guards.py）。
	// 曾有的 ModeStore（Redis）与 /v1/mode 接口已删除。

	// Trace handler (Redis-backed, tenant-isolated)
	var traceHandler *TraceHandler
	if atomicRedis != nil {
		traceHandler = NewTraceHandler(atomicRedis.LoadRaw())
	}

	// Knowledge base — proxied to Python engine
	// SaaS 安全: 知识库独立限流 (每租户 QPS=50, Burst=100)
	var kbRateRedis db.RedisClient
	if atomicRedis != nil {
		kbRateRedis = atomicRedis.LoadRaw()
	}
	kbRateLimiter := NewTenantRateLimiter(kbRateRedis, 50, 100)
	kbRateMW := kbRateLimiter.Middleware

	// Admin handler
	adminHandler := NewAdminHandler(cfg, authenticator, fileStore, atomicRedis, pythonClient)
	adminHandler.rateLimiter = distLimiter
	adminHandler.appSecret = cfg.AppSecret
	// 复用同一 settings.Store：后台「支付配置」的保存路径要用它加密落库
	adminHandler.settingsStore = settingsStore
	// ── Route registration by functional domain ──

	registerPublicEndpoints(mux, authMW, rlMW, publicMW, searchHandler, shareHandler, systemHandler, mediaHandler, cfg, fileStore)
	registerAgentRoutes(mux, authMW, rlMW, publicMW, sanitizeMW, submitHandler, agentSem, eventHub, sessionMgr, authenticator, rpaHub, cfg.InternalToken, cfg.AgentSubmitTimeout, pythonClient)

	// 会话分叉：地图"按真实分支连线"的前提（见 session_fork.go）。
	// 放这里而不是 registerAgentRoutes：那个函数的作用域里没有 sessionMgr。
	mux.Handle("POST /v1/conversations/{id}/fork", authMW(rlMW(ForkConversationHandler(sessionMgr))))
	// 会话分支（裁剪 + 压缩）：前端用它创建"带核心上下文的分支"（见 session_branch.go
	// 与 docs/session-map-branch-design.md）。与 /fork 共用 BranchSession，只差 mode。
	mux.Handle("POST /v1/conversations/{id}/branch", authMW(rlMW(BranchConversationHandler(sessionMgr, pythonClient))))
	// 重试压缩：压缩是异步的，失败/网关重启会把 branch_state 停在 failed/pending，
	// 没有这个入口那两个状态就是死胡同（见 session_branch.go::BranchRetryHandler）。
	mux.Handle("POST /v1/conversations/{id}/branch/retry", authMW(rlMW(BranchRetryHandler(sessionMgr, pythonClient))))
	registerAuthRoutes(mux, authHandler, authMW, rlMW)

	registerSystemRoutes(mux, authMW, rlMW, sanitizeMW, toolHandler, systemHandler, traceHandler, pythonClient)
	// 六大工作台互联：跨台最近活动聚合（租户+用户隔离）
	mux.Handle("GET /v1/activities", authMW(rlMW(http.HandlerFunc(handleActivities))))
	// 通知中心：只读 + 标已读（写入来自网关内部事件，见 notifications.go）
	NewNotificationHandler().RegisterRoutes(mux, authMW, rlMW)
	registerConversationRoutes(mux, conversationHandler, shareHandler, authMW, rlMW)
	registerMediaRoutes(mux, mediaHandler, authMW, rlMW, cfg.StorageRoot)
	registerPluginRoutes(mux, pluginHandler, authMW, rlMW)
	// 新用户引导（P0-3）：用户级自助，身份取自 token。
	onboardingHandler.RegisterRoutes(mux, authMW, rlMW)
	// 用户级 API Key 自助（P1-3）：普通用户路由，越权靠 SQL 的 user_id 条件挡住。
	apiKeyHandler.RegisterRoutes(mux, authMW, rlMW)
	registerProxyRoutes(mux, authMW, rlMW, kbRateMW, pythonClient)
	// 桌面端云端片段推理（POST /v1/desktop/infer，用户 JWT + 限流，见 routes_desktop.go）
	registerDesktopRoutes(mux, authMW, rlMW, pythonClient)
	// 本地语音转写（POST /v1/audio/transcribe）：音频字节透传给引擎，
	// 由本机 faster-whisper 解码 —— 桌面包里浏览器的 SpeechRecognition 依赖
	// Google 在线服务，Electron 构建不含该密钥，必然失败（见 routes_audio.go）。
	mux.Handle("POST /v1/audio/transcribe", authMW(rlMW(AudioTranscribeHandler(pythonClient))))
	mux.Handle("GET /v1/audio/availability", authMW(rlMW(AudioAvailabilityHandler(pythonClient))))
	// OpenAI 兼容对话（POST /v1/chat/completions）：用户 API Key 可直接接入任意
	// OpenAI 客户端 —— 这是「设置 → API Keys」里那把 key 真正被「用起来」的入口。
	registerOpenAICompatRoutes(mux, authMW, rlMW, pythonClient)

	// Agents (auth + rate limited; DB 驱动 CRUD + 运行会话)
	agentHandler := NewAgentHandler(authenticator, pythonClient, agentSem)
	mux.Handle("GET /v1/agents", authMW(rlMW(http.HandlerFunc(agentHandler.List))))
	mux.Handle("POST /v1/agents", authMW(rlMW(http.HandlerFunc(agentHandler.Create))))
	mux.Handle("GET /v1/agents/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.Get))))
	mux.Handle("PUT /v1/agents/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.Update))))
	mux.Handle("DELETE /v1/agents/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.Delete))))
	mux.Handle("PUT /v1/agents/{id}/visibility", authMW(rlMW(http.HandlerFunc(agentHandler.SetVisibility))))
	mux.Handle("POST /v1/agents/{id}/run", authMW(rlMW(http.HandlerFunc(agentHandler.Run))))
	mux.Handle("GET /v1/agents/sessions", authMW(rlMW(http.HandlerFunc(agentHandler.ListSessions))))
	mux.Handle("GET /v1/agents/sessions/{id}", authMW(rlMW(http.HandlerFunc(agentHandler.GetSession))))

	// Super-agent 接管（见 docs 与 internal/api/routes_superagent.go）：
	// 配置 CRUD 在网关（身份取自 claims），运行时端点（presence/segment/interrupt/status）
	// 代理到引擎 —— 那些是只有引擎才知道的事实。
	superagentHandler := NewSuperagentHandler(pythonClient)
	superagentHandler.RegisterRoutes(mux, authMW, rlMW)

	// 会话运行时状态与遥测（P1 单一事实源；见 docs/session-runtime-spec.md）
	sessionRuntimeHandler := NewSessionRuntimeHandler(db.Redis, sessionMgr)
	mux.Handle("GET /v1/sessions/{session_id}/runtime", authMW(rlMW(http.HandlerFunc(sessionRuntimeHandler.GetRuntime))))
	mux.Handle("PUT /v1/sessions/{session_id}/runtime", authMW(rlMW(http.HandlerFunc(sessionRuntimeHandler.PutRuntime))))
	mux.Handle("GET /v1/sessions/{session_id}/metrics", authMW(rlMW(http.HandlerFunc(sessionRuntimeHandler.GetMetrics))))

	// 用量分摊（计费页）：`usage_events` 是计费域删除后唯一还在写的用量事实源。
	// 此前这条路由从未注册，前端每进一次计费页就 404 —— 页面只剩一句加载失败。
	mux.Handle("GET /v1/billing/usage", authMW(rlMW(http.HandlerFunc(GetUsage))))
	// 价格一览表：多源交叉验证后的参考价（**不参与计费**，见 billing_prices.go 注释）。
	mux.Handle("GET /v1/billing/prices", authMW(rlMW(http.HandlerFunc(ListModelPrices))))
	// dispatch 保留 Python 代理（agent 工具链内部调用，非页面主链路）
	// 安全：必须经过 authMW，否则未认证可触发工具执行
	mux.Handle("POST /v1/agents/dispatch", authMW(rlMW(sanitizeMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		var body map[string]interface{}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, "invalid request")
			return
		}
		// 身份注入（P1）：引擎无鉴权，必须用 JWT claims 覆盖透传的租户/用户身份，
		// 防止客户端伪造 tenant_id/user_id 冒充他人。
		if claims := auth.GetClaims(r.Context()); claims != nil {
			body["tenant_id"] = claims.TenantID
			body["user_id"] = claims.UserID
		}
		var resp map[string]interface{}
		if err := pythonClient.PostJSON(r.Context(), "/v1/agents/dispatch", body, &resp); err != nil {
			InternalError(w, "python agent dispatch failed")
			return
		}
		OK(w, resp)
	})))))

	// Skills (auth + rate limited + prompt sanitized, proxies to Python)
	skillHandler.RegisterRoutes(mux, authMW, rlMW, sanitizeMW)

	// 资源市场（agents / skills / knowledge / mcp 四类统一入口）。
	// 目录聚合层在 internal/market：内置目录随二进制发布（//go:embed），
	// 因此不依赖任何外部服务。外部源**默认关闭**，初装时市场页只有内置 Tab；
	// 开启入口有两处 —— env（MARKET_REMOTE_ENABLED）或后台「系统设置 → market」
	// （system_settings 的 market 分类，DB 覆盖 env），见 market_wiring.go。
	// settingsStore 可能为 nil（无 DB 的本地模式），此时纯用 env。
	marketSvc := newMarketService(cfg, pythonClient, settingsStore)
	marketHandler := NewMarketHandler(marketSvc)
	marketHandler.RegisterRoutes(mux, authMW, rlMW)

	// 安装流水线（独立 handler，便于与源管理分支并行合并）：
	// 只读目录在 market_handler.go，写路径（装/卸/已安装列表）在 market_install.go。
	// 编排（幂等 / 内外源默认启用 / 外部 MCP 门禁）在 internal/market/install.go，
	// 落点（agents 表、plugins.json、Python 技能与知识库）由本层注入。
	NewMarketInstallHandler(marketSvc, pythonClient, pluginHandler, cfg).
		RegisterRoutes(mux, authMW, rlMW)

	// 工具授权模式（GET/POST /v1/mode）已移除：模式不再是服务端状态，而是前端随
	// 每次提交携带的请求参数（llm_config.tools_mode）。见 mode.go 的说明。
	//
	// 旧 /v1/permission/approve|reject 已移除：审批统一由 Python 侧处理
	// （前端经 /v1/agent/approval 提交决定），避免 Go/Python 双实现漂移。

	registerAdminRoutes(mux, authMW, rlMW, adminHandler, pythonClient)

	// 可选 SPA 静态托管（CHIRON_WEB_ROOT）：分享链接要有一个**别人也能打开**的宿主。
	// 不配置时不注册任何东西，"GET /" 仍是 404 —— 行为与引入前完全一致。
	// 必须放在所有 API 路由之后：ServeMux 按最具体的 pattern 匹配，不依赖注册顺序，
	// 但把兜底放在最后读起来才不会被误认为"吞掉 API"。
	//
	// 未显式配置时用 resolver 探测（config.WebRoot）：打包态是 `<安装根>\resources\web`
	// （Go 进程打不开 asar，只能读这份）、开发态是 `frontend-vue/dist`。以前这里空串
	// 就等于"不托管"，而打包态又没人保证 `CHIRON_WEB_ROOT` 一定注入 —— 分享链接于是
	// 静默退化成 `file://`。探测不到仍然是空串（不托管），不会让网关起不来。
	if cfg != nil {
		webRoot := strings.TrimSpace(cfg.WebRoot)
		if webRoot == "" {
			webRoot = config.WebRoot()
		}
		mountSPA(mux, webRoot)
	}

	// Wrap main mux with public middleware.
	return publicMW(mux)
}
