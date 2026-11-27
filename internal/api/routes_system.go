package api

import (
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// routes_system.go —— 系统（工具/健康/追踪）的路由注册。
// 由 gateway_router.go 按业务域拆分而来。编辑器路由曾在此组，已随
// `/api/editor/*` 退役整体移除（读能力由 project_files.go 按项目根提供）。

// ── System (tools / health / trace) ──

func registerSystemRoutes(
	mux *http.ServeMux,
	authMW, rlMW, _ routeMiddleware,
	toolHandler *ToolHandler,
	systemHandler *SystemHandler,
	traceHandler *TraceHandler,
	pythonClient *engine.PythonClient,
) {
	// Tool execution is Agent-only. A public execute route would bypass ToolGuard,
	// Broker authorization, approvals, and hooks.
	mux.Handle("GET /v1/tools", authMW(rlMW(http.HandlerFunc(toolHandler.ListTools))))

	// System (rate limited; spans/traces 仅管理员可见，S 安全修复：原为公开信息泄露)
	// 健康评分属内部运行信息；同组的 spans/traces 都要求 PermAdminRead，此处漏了鉴权。
	// 三处都要求 PermAdminRead：这些端点回的是**全局**运行指标（健康分/span/trace/计数器），
	// 任意登录用户可读会泄漏平台内部状态。此前 health 与 metrics 漏了权限，与同组
	// spans/traces 口径不一致（metrics 更矛盾：公开的 Prometheus /metrics 反而要求 admin）。
	mux.Handle("GET /v1/system/health", authMW(rlMW(RequirePermission(auth.PermAdminRead)(http.HandlerFunc(systemHandler.HealthScores)))))
	// 逐组件健康态：客户端状态栏需要它把"已连接"落到实处（此前只看 navigator.onLine）。
	// 回的是各依赖的可用性与失败原因，不含全局计数/评分，故按登录用户开放即可。
	mux.Handle("GET /v1/system/components", authMW(rlMW(http.HandlerFunc(systemHandler.ComponentsHealth))))
	// F12：当前用户的日费用/预算读数（只读；管理端配额仍是唯一写入口）。
	mux.Handle("GET /v1/usage/budget", authMW(rlMW(http.HandlerFunc(GetUsageBudget))))
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

	// 子 Agent 运行观测 / 中止 / 重跑：见下方 registerSubagentRoutes。
	registerSubagentRoutes(mux, authMW, rlMW, NewSubagentHandler(db.Redis, pythonClient))
}

// registerSubagentRoutes 单独成一个函数：子 Agent 这组端点的权限/路由链路值得被
// **单独**测（`subagent_rerun_test.go` 只挂这一组，不装配整张网关路由表）。
func registerSubagentRoutes(
	mux *http.ServeMux,
	authMW, rlMW routeMiddleware,
	h *SubagentHandler,
) {
	// 子 Agent 运行观测（层级树 / 详情 / 过程）：Redis 优先、DB 回落，租户隔离。
	// 见 docs/subagent-design.md §4.4；前端据此画递归树与侧边栏实时输出。
	mux.Handle("GET /v1/subagent/runs", authMW(rlMW(http.HandlerFunc(h.ListRuns))))
	mux.Handle("GET /v1/subagent/runs/{run_id}", authMW(rlMW(http.HandlerFunc(h.GetRun))))
	mux.Handle("GET /v1/subagent/runs/{run_id}/events", authMW(rlMW(http.HandlerFunc(h.GetRunEvents))))
	// 中止：网关只做租户校验 + Redis 广播，真正取消由持有该 run 的引擎实例执行
	// （引擎侧订阅见 python-engine/app/subagent/registry.py）
	mux.Handle("POST /v1/subagent/runs/{run_id}/cancel", authMW(rlMW(http.HandlerFunc(h.CancelRun))))
	mux.Handle("POST /v1/subagent/sessions/{session_id}/cancel", authMW(rlMW(http.HandlerFunc(h.CancelSessionRuns))))
	// P0-2 重跑：按**原任务描述**重新派发一个新 run（立即返回新 run_id）。
	// 这是「重试」按钮的真实通道 —— 网关校验权限/会话归属/终态后转发给引擎。
	mux.Handle("POST /v1/subagent/runs/{run_id}/rerun", authMW(rlMW(http.HandlerFunc(h.RerunRun))))
	// F1 父→子 steer：只允许本会话的子 run；网关校验归属后按 run 归属转发到
	// **持有它的引擎实例**（run 注册表是进程内的，不能随机打副本）。
	mux.Handle("POST /v1/subagent/runs/{run_id}/steer", authMW(rlMW(http.HandlerFunc(h.SteerRun))))
	// F3：界面直接发起后台委派（会话归属校验后转引擎，与模型自己调 subagent 同源）。
	mux.Handle("POST /v1/sessions/{session_id}/subagents", authMW(rlMW(http.HandlerFunc(h.DispatchSubagent))))
}
