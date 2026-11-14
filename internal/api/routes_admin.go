package api

import (
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// routes_admin.go —— 管理后台的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Admin ──

func registerAdminRoutes(
	mux *http.ServeMux,
	authMW routeMiddleware,
	rlMW routeMiddleware,
	adminHandler *AdminHandler,
	pythonClient *engine.PythonClient,
) {
	// Admin routes (auth + admin permission + rate limit)
	// P1-3: 所有 admin 路由必须挂限流，防止被劫持的 admin token 无限调用
	// 造成破坏（backup/restore/users DELETE 等敏感操作）。
	// 读操作用 PermAdminRead，写操作（PUT/DELETE/POST）必须 PermAdminWrite。
	// P1-4: 用户管理路由（PUT/DELETE /v1/admin/users）必须 PermUsersManage，
	// 该权限仅 owner 角色持有，普通 admin 不应能删/改用户。
	adminReadMW := RequirePermission(auth.PermAdminRead)
	// 平台级端点：`admin:write` 是租户 admin 就持有的权限，光靠它挡不住跨租户操作。
	// 见 middleware.go 的 RequirePlatformOwner 注释（租户 admin 可读全库 / 删任意租户）。
	platformReadMW := RequirePlatformOwner(auth.PermAdminRead)
	platformWriteMW := RequirePlatformOwner(auth.PermAdminWrite)
	usersManageMW := RequirePermission(auth.PermUsersManage)
	adminMux := http.NewServeMux()
	adminHandler.RegisterRoutes(adminMux)
	// Strip the /v1/admin prefix so adminMux patterns (e.g. "GET /metrics") match correctly
	adminStrip := http.StripPrefix("/v1/admin", adminMux)
	mux.Handle("GET /v1/admin/metrics", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/users/{id}", authMW(rlMW(adminReadMW(adminStrip))))
	// 用户写操作收紧为 PermUsersManage（仅 owner）
	mux.Handle("PUT /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/users/{id}", authMW(rlMW(usersManageMW(adminStrip))))
	mux.Handle("GET /v1/admin/system", authMW(rlMW(adminReadMW(adminStrip))))
	// D1：自诊断报告（只读）—— 权限口径与其它 admin 读端点一致。
	mux.Handle("GET /v1/admin/diagnostics", authMW(rlMW(adminReadMW(adminStrip))))
	// D3：用量目录（只读）—— 与其它 admin 读端点同一权限口径。
	mux.Handle("GET /v1/admin/usage/catalog", authMW(rlMW(adminReadMW(adminStrip))))
	// 新用户引导漏斗（P0-3 验收「完成率可查」）—— 只读统计，与其它 admin 读端点同口径。
	mux.Handle("GET /v1/admin/onboarding/funnel", authMW(rlMW(adminReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/maintenance", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("GET /v1/admin/kb", authMW(rlMW(adminReadMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil || !pythonClient.IsConnected() {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		r.Header.Set("X-User-Role", claims.Role)
		pythonClient.ForwardRequest(w, r, "/v1/admin/kb?user_id="+claims.UserID+"&tenant_id="+tenantStr(claims.TenantID))
	})))))

	// Storage admin routes
	mux.Handle("GET /v1/admin/storage", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/storage", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/storage/test", authMW(rlMW(platformWriteMW(adminStrip))))

	// Redis admin routes
	mux.Handle("GET /v1/admin/redis", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/redis", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/redis/test", authMW(rlMW(platformWriteMW(adminStrip))))
	// 慢日志 / 清库：handler 早已注册在 adminMux（admin_ops.go:56-57），但**从未在此暴露到外部 mux**
	// ⇒ 前端「刷新慢日志」「执行 FLUSHALL」恒 404（与下面 models / settings 是同一个坑）。
	// FLUSHDB 是**破坏性写操作** ⇒ 必须走 adminWriteMW，不能给读取权限。
	mux.Handle("GET /v1/admin/redis/slow-log", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/redis/flush-all", authMW(rlMW(platformWriteMW(adminStrip))))

	// Database admin routes（/admin/datastores?tab=database 整页依赖它们）
	// 同样的问题：handler 早在 adminMux 里注册（admin_ops.go:47-53），但从未暴露 ⇒
	// 「数据库状态 / 配置 / 查询 / 优化」四个端点全部 404。
	// 判据照本文件既定口径（见文件头 P1-3）：GET 用 adminReadMW；
	// 任意 SELECT、VACUUM/ANALYZE 都是**写操作** ⇒ 一律 adminWriteMW。
	// 备份 / 恢复端点已移除：企业部署用云数据库（快照/PITR），容器内 pg_dump/psql 不适用。
	mux.Handle("GET /v1/admin/database/status", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("GET /v1/admin/database/configs", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/database/query", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/database/optimize/{action}", authMW(rlMW(platformWriteMW(adminStrip))))

	// Queue admin routes
	mux.Handle("GET /v1/admin/queue", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/flush", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("POST /v1/admin/queue/pause", authMW(rlMW(platformWriteMW(adminStrip))))

	// Cache admin routes
	mux.Handle("GET /v1/admin/cache/stats", authMW(rlMW(platformReadMW(adminStrip))))

	// Performance admin routes
	mux.Handle("GET /v1/admin/performance", authMW(rlMW(platformReadMW(adminStrip))))

	// LLM 熔断读数 / 手动恢复。
	// 读用 platformReadMW（回的是全局运行状态，租户 admin 不该看见别家的 provider 熔断）；
	// 重置用 platformWriteMW —— 它会把一个被判熔断的 provider **立刻重新放流量**，
	// 是改变系统运行状态的写操作，与 queue/flush、redis/flush-all 同口径。
	mux.Handle("GET /v1/admin/gateway/breakers", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/gateway/reset-breaker", authMW(rlMW(platformWriteMW(adminStrip))))

	// API Key admin routes (direct handlers, avoid adminMux path mismatch)
	mux.Handle("GET /v1/admin/api-keys", authMW(rlMW(platformReadMW(http.HandlerFunc(adminHandler.ListLLMKeys)))))
	mux.Handle("POST /v1/admin/api-keys", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.AddLLMKey)))))
	mux.Handle("PUT /v1/admin/api-keys/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.UpdateLLMKeyStatus)))))
	// M27：改分配权重（smooth WRR 的比例）。与 status 分开是有意的 —— `weight = 0` 的语义是
	// "不参与选择但保留状态"，而 status 的语义是"这把 key 能不能用"。
	mux.Handle("PUT /v1/admin/api-keys/{id}/weight", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.UpdateLLMKeyWeight)))))
	mux.Handle("DELETE /v1/admin/api-keys/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.DeleteLLMKey)))))

	// LLM provider catalog routes（服务提供商目录与端点覆盖）
	mux.Handle("GET /v1/admin/llm-providers", authMW(rlMW(platformReadMW(http.HandlerFunc(adminHandler.ListLLMProviders)))))
	mux.Handle("PUT /v1/admin/llm-providers/{id}", authMW(rlMW(platformWriteMW(http.HandlerFunc(adminHandler.SetLLMProviderConfig)))))

	// 模型配置：决定 /v1/models（进而决定 /chat 的模型下拉）里有哪些模型。
	// adminMux 里**早已注册** GET/POST /models 与 PUT/DELETE /models/{id}（admin_ops.go:60-63），
	// 但从未在此暴露到外部 mux → 前端无论怎么点都调不到，只能依赖模型发现自动写入；
	// 一旦发现失败（provider 无 /models、UA 被拦、网络不可达），可用模型集永远是空的。
	mux.Handle("GET /v1/admin/models", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("POST /v1/admin/models", authMW(rlMW(platformWriteMW(adminStrip))))
	// 批量开关（/models 页的组内全选 → 启用/禁用）。必须**在 {id} 之外单独注册**：
	// 前者是写操作，鉴权口径与 PUT /models/{id} 一致。
	mux.Handle("PUT /v1/admin/models/batch", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("PUT /v1/admin/models/{id}", authMW(rlMW(platformWriteMW(adminStrip))))
	mux.Handle("DELETE /v1/admin/models/{id}", authMW(rlMW(platformWriteMW(adminStrip))))

	// Settings admin routes
	// 读取分组配置此前只在 adminMux 内注册、从未暴露到外部 mux，前端 getSettings 恒 404
	// （页面回填静默失效）。此处补齐 GET，与 PUT 同鉴权口径。
	mux.Handle("GET /v1/admin/settings", authMW(rlMW(platformReadMW(adminStrip))))
	mux.Handle("PUT /v1/admin/settings", authMW(rlMW(platformWriteMW(adminStrip))))

}
