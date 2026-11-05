package api

import (
	"net/http"
)

// routes_conversation.go —— 会话的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// ── Conversations ──

func registerConversationRoutes(
	mux *http.ServeMux,
	conversationHandler *ConversationHandler,
	shareHandler *ShareHandler,
	authMW, rlMW routeMiddleware,
) {
	// Conversations (auth + rate limited)
	mux.Handle("GET /v1/conversations", authMW(rlMW(http.HandlerFunc(conversationHandler.List))))
	mux.Handle("POST /v1/conversations", authMW(rlMW(http.HandlerFunc(conversationHandler.Create))))
	mux.Handle("GET /v1/conversations/{id}", authMW(rlMW(http.HandlerFunc(conversationHandler.Get))))
	// 「自定义分叉」的选点列表。必须与复制窗口同口径（ORDER BY created_at, id），
	// 不能拿 GET /v1/conversations/{id} 的消息下标当分叉点 —— 那条路径按 seq 排序，
	// 存量数据上与复制口径可能不一致（见 conversation.go::ForkPoints）。
	mux.Handle("GET /v1/conversations/{id}/fork-points", authMW(rlMW(http.HandlerFunc(conversationHandler.ForkPoints))))
	mux.Handle("PUT /v1/conversations/{id}", authMW(rlMW(http.HandlerFunc(conversationHandler.Update))))
	mux.Handle("DELETE /v1/conversations/{id}", authMW(rlMW(http.HandlerFunc(conversationHandler.Delete))))

	// 工具调用 output 的按需分段读取。列表接口只给前 4096 字符预览，
	// 全文由这里取（见 internal/api/tool_output.go 与 session.GetToolCallOutput）。
	mux.Handle("GET /v1/chat/sessions/{id}/tool-calls/{tcID}/output",
		authMW(rlMW(http.HandlerFunc(conversationHandler.ToolOutput))))

	// Conversation shares (auth + rate limited; public GET in registerPublicEndpoints)
	mux.Handle("POST /v1/conversations/{id}/share", authMW(rlMW(http.HandlerFunc(shareHandler.Create))))
	mux.Handle("GET /v1/conversations/{id}/share", authMW(rlMW(http.HandlerFunc(shareHandler.GetActive))))
	mux.Handle("DELETE /v1/conversations/{id}/share", authMW(rlMW(http.HandlerFunc(shareHandler.Revoke))))

	// 会话地图布局：Redis 热层 + 异步落 PG（见 internal/api/sessionmap.go）
	smHandler := NewSessionMapHandler()
	mux.Handle("GET /v1/session-map/workspaces", authMW(rlMW(http.HandlerFunc(smHandler.ListWorkspaces))))
	mux.Handle("POST /v1/session-map/workspaces", authMW(rlMW(http.HandlerFunc(smHandler.CreateWorkspace))))
	mux.Handle("GET /v1/session-map/workspaces/{id}", authMW(rlMW(http.HandlerFunc(smHandler.GetWorkspace))))
	mux.Handle("PUT /v1/session-map/workspaces/{id}", authMW(rlMW(http.HandlerFunc(smHandler.SaveWorkspace))))
	mux.Handle("DELETE /v1/session-map/workspaces/{id}", authMW(rlMW(http.HandlerFunc(smHandler.DeleteWorkspace))))
	mux.Handle("POST /v1/session-map/workspaces/{id}/fork-node", authMW(rlMW(http.HandlerFunc(smHandler.ForkNode))))
	// 项目（Workspace 即项目）：会话地图改造后的主要入口，语义见
	// docs/project-management-design.md。项目 = root_path 非空的 workspace。
	mux.Handle("GET /v1/session-map/projects", authMW(rlMW(http.HandlerFunc(smHandler.ListProjects))))
	mux.Handle("POST /v1/session-map/projects", authMW(rlMW(http.HandlerFunc(smHandler.CreateProject))))
	mux.Handle("PATCH /v1/session-map/projects/{id}", authMW(rlMW(http.HandlerFunc(smHandler.PatchProject))))
	// 删除项目直接复用 DeleteWorkspace：项目就是 workspace，删它只级联清掉
	// session_map_nodes（含归属记录），sessions 本身完好（I5）。单独实现一遍只会
	// 多出一份缓存清理逻辑，两处早晚不一致。
	mux.Handle("DELETE /v1/session-map/projects/{id}", authMW(rlMW(http.HandlerFunc(smHandler.DeleteWorkspace))))
	mux.Handle("GET /v1/session-map/projects/{id}/sessions", authMW(rlMW(http.HandlerFunc(smHandler.ListProjectSessions))))
	mux.Handle("PUT /v1/session-map/projects/{id}/sessions/{sid}", authMW(rlMW(http.HandlerFunc(smHandler.BindProjectSession))))
	mux.Handle("DELETE /v1/session-map/projects/{id}/sessions/{sid}", authMW(rlMW(http.HandlerFunc(smHandler.UnbindProjectSession))))
	// 未归入任何项目的会话：存量会话全无归属，没有这个分组它们在改造后会从侧边栏消失。
	mux.Handle("GET /v1/session-map/unassigned-sessions", authMW(rlMW(http.HandlerFunc(smHandler.ListUnassignedSessions))))
	// 当前用户的分享列表（数据管理 → 分享管理）；与上面的按会话接口同组、同鉴权。
	mux.Handle("GET /v1/shares", authMW(rlMW(http.HandlerFunc(shareHandler.List))))
}
