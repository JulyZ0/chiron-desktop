package api

import (
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// routes_system.go —— 系统（工具/健康/追踪）的路由注册。
// 由 gateway_router.go 按业务域拆分而来。编辑器路由曾在此组，已随
// `/api/editor/*` 退役整体移除（读能力由 project_files.go 按项目根提供）。

// ── System (tools / health / trace) ──

func registerSystemRoutes(
	mux *http.ServeMux,
	authMW, rlMW, sanitizeMW routeMiddleware,
	toolHandler *ToolHandler,
	systemHandler *SystemHandler,
	traceHandler *TraceHandler,
) {
	// Tools (rate limited, proxies to Python)
	// 工具面属于内部信息（含 MCP 注入工具名/描述/完整入参 schema），
	// 未认证即可枚举等于免费交付攻击面清单。同组 /v1/tools/execute 早有 authMW。
	mux.Handle("GET /v1/tools", authMW(rlMW(http.HandlerFunc(toolHandler.ListTools))))
	mux.Handle("POST /v1/tools/execute", authMW(rlMW(sanitizeMW(http.HandlerFunc(toolHandler.ExecuteTool)))))

	// System (rate limited; spans/traces 仅管理员可见，S 安全修复：原为公开信息泄露)
	// 健康评分属内部运行信息；同组的 spans/traces 都要求 PermAdminRead，此处漏了鉴权。
	// 三处都要求 PermAdminRead：这些端点回的是**全局**运行指标（健康分/span/trace/计数器），
	// 任意登录用户可读会泄漏平台内部状态。此前 health 与 metrics 漏了权限，与同组
	// spans/traces 口径不一致（metrics 更矛盾：公开的 Prometheus /metrics 反而要求 admin）。
	mux.Handle("GET /v1/system/health", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.HealthScores)))))
	// 逐组件健康态：客户端状态栏需要它把"已连接"落到实处（此前只看 navigator.onLine）。
	// 回的是各依赖的可用性与失败原因，不含全局计数/评分，故按登录用户开放即可。
	mux.Handle("GET /v1/system/components", authMW(rlMW(http.HandlerFunc(systemHandler.ComponentsHealth))))
	mux.Handle("GET /v1/system/spans", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.Spans)))))
	mux.Handle("GET /v1/system/traces", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.Traces)))))
	mux.Handle("GET /v1/metrics", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.Metrics)))))
	mux.Handle("POST /v1/admin/log-level", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(http.HandlerFunc(systemHandler.SetLogLevel)))))
	// 手动触发一轮模型价格同步：后台循环 30min 一轮，新装/换模型后不该干等。
	// 写操作（出站拉取 + 写 model_prices），与 log-level 同权限口径。
	mux.Handle("POST /v1/billing/prices/sync", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(http.HandlerFunc(systemHandler.PriceSync)))))

	// Trace (user-level call chain tracing, tenant-isolated)
	if traceHandler != nil {
		mux.Handle("GET /v1/traces", authMW(rlMW(http.HandlerFunc(traceHandler.ListTraces))))
		mux.Handle("GET /v1/traces/{trace_id}", authMW(rlMW(http.HandlerFunc(traceHandler.GetTrace))))
		// 差距C：执行图（父子连通）—— GetTrace 给的是扁平 span 列表，这里给的是"从哪来"。
		mux.Handle("GET /v1/traces/{trace_id}/graph", authMW(rlMW(http.HandlerFunc(traceHandler.GetTraceGraph))))
	}

	// 子 Agent 运行观测（层级树 / 详情 / 过程）：Redis 优先、DB 回落，租户隔离。
	// 见 docs/subagent-design.md §4.4；前端据此画递归树与侧边栏实时输出。
	subagentHandler := NewSubagentHandler(db.Redis)
	mux.Handle("GET /v1/subagent/runs", authMW(rlMW(http.HandlerFunc(subagentHandler.ListRuns))))
	mux.Handle("GET /v1/subagent/runs/{run_id}", authMW(rlMW(http.HandlerFunc(subagentHandler.GetRun))))
	mux.Handle("GET /v1/subagent/runs/{run_id}/events", authMW(rlMW(http.HandlerFunc(subagentHandler.GetRunEvents))))
	// 中止：网关只做租户校验 + Redis 广播，真正取消由持有该 run 的引擎实例执行
	// （引擎侧订阅见 python-engine/app/subagent/registry.py）
	mux.Handle("POST /v1/subagent/runs/{run_id}/cancel", authMW(rlMW(http.HandlerFunc(subagentHandler.CancelRun))))
	mux.Handle("POST /v1/subagent/sessions/{session_id}/cancel", authMW(rlMW(http.HandlerFunc(subagentHandler.CancelSessionRuns))))
}
