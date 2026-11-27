package api

import (
	"errors"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// decodeOptionalJSON 解析代理请求里**可选**的 JSON 请求体。
//
// 空 body 是合法的：`POST /v1/kb/{id}/build` 就是不带 body 的语义（重建索引，
// 没有参数）。旧实现对空 body 直接走 DecodeJSON，它返回 io.EOF，于是被当成
// "请求体非法" —— 前端一点「构建索引」就收到 `invalid request body`，
// 而请求本身完全正常。
//
// 只放行 io.EOF（真正的空 body）；语法错误、类型不符仍然 400 —— 把全部解码
// 错误都吞掉会掩盖前端参数写错，那类问题会变成"请求被静默忽略"，更难查。
func decodeOptionalJSON(w http.ResponseWriter, r *http.Request) (map[string]interface{}, bool) {
	body := map[string]interface{}{}
	if r.ContentLength == 0 {
		return body, true
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		if errors.Is(err, io.EOF) {
			return map[string]interface{}{}, true
		}
		BadRequest(w, ErrInvalidReq)
		return nil, false
	}
	if body == nil {
		body = map[string]interface{}{}
	}
	return body, true
}

// routes_proxy.go —— Python 引擎代理（图/工作流/知识库）的路由注册。
// 由 gateway_router.go 按业务域拆分而来（纯移动：函数签名、函数体、调用点一字未改）。

// escSeg 转义一个**来自客户端**的路径参数，使其只能落在单个 URL path 段内。
//
// 为什么必须转义：r.PathValue 返回的是已解码的值 —— 请求里的 %3F / %23 / %2F
// 到这里已经还原成 ? / # / /。这些值随后被拼进转发给 Python 引擎的 URL，
// 不编码就等于让客户端控制 URL 结构：
//
//	POST /v1/kb/abc%3Fuser_id%3Dvictim%26x%3D/documents
//	→ 转发 "/v1/kb/abc?user_id=victim&x=/documents?user_id=<真实用户>&tenant_id=1"
//
// query 里第一个 user_id 会赢，网关自己追加的那个被顶掉；而引擎侧身份完全取自
// query（`user_id: str = Query("")`，见 python-engine/app/api/knowledge.py 等全部
// 知识库端点），越权因此成立 —— 任何人只要拿到 kb_id 就能以他人身份读写其知识库。
// 同类问题还包括 %2F 造成的路由边界穿越。
//
// PathEscape 而非 QueryEscape：它保留 & 和 = 的字面量，却会转义 ? # / %，
// 这正好是拼 path 段所需的最小集合。
func escSeg(s string) string { return url.PathEscape(s) }

// Engine denials use HTTP 200, so only an explicit top-level failure contract may
// override the proxy status; untyped or successful payloads remain unchanged.
func writeTypedProxyError(w http.ResponseWriter, payload interface{}) bool {
	body, ok := payload.(map[string]interface{})
	if !ok {
		return false
	}
	success, ok := body["success"].(bool)
	if !ok || success {
		return false
	}
	errorCode, ok := body["error_code"].(string)
	if !ok {
		return false
	}
	status := 0
	fallback := ""
	switch errorCode {
	case CodeNotFound:
		status, fallback = http.StatusNotFound, ErrNotFound
	case CodeForbidden:
		status, fallback = http.StatusForbidden, "insufficient permissions"
	default:
		return false
	}
	message, _ := body["error"].(string)
	if message == "" {
		message = fallback
	}
	JSONWithCode(w, status, errorCode, nil, message)
	return true
}

// ── Python engine proxy routes (knowledge base / sessions) ──

func registerProxyRoutes(
	mux *http.ServeMux,
	authMW, rlMW, kbRateMW routeMiddleware,
	pythonClient *engine.PythonClient,
	userSem *UserSemaphorePool,
) {
	// pythonProxy is a factory for reverse-proxy handlers to the Python engine.
	// All routes using this factory are wrapped with authMW, so claims are
	// guaranteed present in context.
	type proxyOpt struct {
		methods []string // allowed HTTP methods; empty = GET+POST+PUT+DELETE
		logTag  string
		// mutateBody 在把请求体转发给引擎前改写它（P1-c：统一链路注入会话运行时解析结果，
		// 见 internal/api/session_runtime.go 的 loadRuntime）。返回 false 表示已自行写出响应、代理应中止。
		mutateBody func(w http.ResponseWriter, r *http.Request, body map[string]interface{}) bool
		// guardBody 在 mutateBody **之后**执行准入校验（P2-2b：模型可用范围）。
		//
		// 顺序不能反：mutateBody 会把会话运行时解析出的最终 model 写回 body，
		// 若在此之前校验，看到的是前端随手传的字段而非真正生效的模型。
		// 返回 false 表示已自行写出响应（如 403）、代理应中止。
		guardBody func(w http.ResponseWriter, r *http.Request, body map[string]interface{}) bool
		// forwardQuery 以**白名单**形式透传客户端 query（只在这几个键上生效）。
		//
		// 为什么必须有：转发路径由 `buildPath(r) + "?user_id=…&tenant_id=…"` 拼成，
		// **r.URL.RawQuery 整个被丢弃**。而 C3 的记忆按项目隔离，引擎侧读的正是
		// `?project=`（见 python-engine/app/api/memory.py 的 `_project`）——
		// 到了网关这一跳就没了：记忆页选了项目，看到的仍是未分组，新写入的条目
		// 也落回未分组，隔离"看起来做了、实际完全没生效"。
		//
		// 为什么不是整串透传：客户端自带 `?user_id=victim` 会与网关追加的身份并存，
		// 谁先谁后取决于拼接顺序 —— 这正是 routes_proxy_escseg_test.go 钉住的 P0
		// 越权回归。只按明确的键取值，身份永远来自 claims，伪造的同名参数进不来。
		forwardQuery []string
		// afterResponse 在成功转发、写出响应**之前**收到引擎回包（已解码的 map）。
		//
		// 存在的唯一理由：统一链路 `/v1/chat/submit` 是**直通代理**，网关此前
		// 完全不看回包 —— 于是它产生的用量一条都进不了 `usage_events`（计费页里
		// 这些回合连行都没有）。钩子让记账挂在既有转发上一次做完，而不是再写一条
		// 平行的代理实现（两套就有两处会漂移）。
		//
		// 失败绝不影响响应：钩子内部必须自己吞异常（见 recordUnifiedUsageFromResponse）。
		afterResponse func(r *http.Request, reqBody map[string]any, resp map[string]any)
	}
	newProxy := func(prefix string, opt proxyOpt) func(func(*http.Request) string) http.HandlerFunc {
		return func(buildPath func(*http.Request) string) http.HandlerFunc {
			return func(w http.ResponseWriter, r *http.Request) {
				if pythonClient == nil {
					InternalError(w, "python engine not available")
					return
				}
				claims := auth.GetClaims(r.Context())
				if claims == nil || claims.TenantID == 0 {
					Unauthorized(w, "missing tenant context")
					return
				}
				// 多租户隔离：透传 tenant_id 给 Python 引擎（query 参数兼容，header 见 pythonClient.WithTenant）
				//
				// `forwardQuery` 里的键**先拼**、身份**后拼**：即便某个键在引擎侧与身份同名，
				// 追加的身份也在后面 —— 但真正的保证是白名单里根本不包含 user_id/tenant_id
				// （见 proxyOpt.forwardQuery 的注释）。键名同样走 QueryEscape，值里的
				// `&` / `=` 不会撑开 query 结构。
				proxiedPath := buildPath(r) + "?"
				if len(opt.forwardQuery) > 0 {
					q := r.URL.Query()
					var parts []string
					for _, key := range opt.forwardQuery {
						for _, v := range q[key] {
							parts = append(parts, url.QueryEscape(key)+"="+url.QueryEscape(v))
						}
					}
					if len(parts) > 0 {
						proxiedPath += strings.Join(parts, "&") + "&"
					}
				}
				proxiedPath += "user_id=" + url.QueryEscape(claims.UserID) + "&tenant_id=" + url.QueryEscape(tenantStr(claims.TenantID))
				var resp interface{}
				var err error
				// reqBody 留给 afterResponse：记账要看请求里的 session_id / llm_config。
				var reqBody map[string]interface{}
				switch r.Method {
				case "GET":
					err = pythonClient.GetJSON(r.Context(), proxiedPath, &resp)
				case "POST":
					body, ok := decodeOptionalJSON(w, r)
					if !ok {
						return
					}
					if opt.mutateBody != nil && !opt.mutateBody(w, r, body) {
						return
					}
					if opt.guardBody != nil && !opt.guardBody(w, r, body) {
						return
					}
					reqBody = body
					// 统一链路也要按会话粘滞：同一 session 的请求落同一引擎，
					// 否则多实例下上下文/热缓存在实例间漂移。
					reqCtx := r.Context()
					if sid, _ := body["session_id"].(string); sid != "" {
						reqCtx = engine.WithSession(reqCtx, sid)
					}
					err = pythonClient.PostJSON(reqCtx, proxiedPath, body, &resp)
				case "PUT":
					body, ok := decodeOptionalJSON(w, r)
					if !ok {
						return
					}
					if opt.mutateBody != nil && !opt.mutateBody(w, r, body) {
						return
					}
					if opt.guardBody != nil && !opt.guardBody(w, r, body) {
						return
					}
					reqBody = body
					err = pythonClient.PutJSON(r.Context(), proxiedPath, body, &resp)
				case "DELETE":
					err = pythonClient.DeleteJSON(r.Context(), proxiedPath, &resp)
				}
				if err != nil {
					slog.Error(opt.logTag+" proxy error", "path", proxiedPath, "error", err)
					logAndRespond(w, err, http.StatusInternalServerError, "internal error")
					return
				}
				if writeTypedProxyError(w, resp) {
					return
				}
				// 旁路记账（只有需要的路由注册了钩子）：写法见 unified_usage.go。
				// 放在 OK 之前、转发之后 —— 顺序上它只影响"什么时候写库"，不影响回包；
				// 但必须在这里而不是 goroutine 里，否则进程退出/请求密集时记账会丢。
				if opt.afterResponse != nil {
					if payload, ok := resp.(map[string]interface{}); ok {
						opt.afterResponse(r, reqBody, payload)
					}
				}
				OK(w, resp)
			}
		}
	}

	// Helper: build path functions for parameterised routes
	pathFn := func(static string) func(*http.Request) string {
		return func(*http.Request) string { return static }
	}
	pathParam := func(prefix string) func(*http.Request) string {
		return func(r *http.Request) string { return prefix + "/" + escSeg(r.PathValue("id")) }
	}
	pathParamSuffix := func(prefix, suffix string) func(*http.Request) string {
		return func(r *http.Request) string {
			return prefix + "/" + escSeg(r.PathValue("id")) + suffix
		}
	}
	// pathParamNamed 支持自定义路径参数名（如 {conflict_id}），用于修复参数丢失的代理路由。
	pathParamNamed := func(prefix, param, suffix string) func(*http.Request) string {
		return func(r *http.Request) string {
			return prefix + "/" + escSeg(r.PathValue(param)) + suffix
		}
	}

	// Knowledge Base (auth + rate limited + tenant QPS limit, proxies to Python)
	kbP := newProxy("/v1/kb", proxyOpt{logTag: "kb"})
	mux.Handle("GET /v1/kb", authMW(kbRateMW(kbP(pathFn("/v1/kb")))))
	mux.Handle("POST /v1/kb", authMW(kbRateMW(kbP(pathFn("/v1/kb")))))
	// 用户级知识库设置（provider 二选一 + Cloud key 密文，由引擎侧加密落库）。
	// 字面量路径比 /v1/kb/{id} 更具体，Go 1.22 ServeMux 按具体性匹配，不会被吃掉。
	mux.Handle("GET /v1/kb/settings", authMW(kbRateMW(kbP(pathFn("/v1/kb/settings")))))
	mux.Handle("PUT /v1/kb/settings", authMW(kbRateMW(kbP(pathFn("/v1/kb/settings")))))
	mux.Handle("POST /v1/kb/settings/verify", authMW(kbRateMW(kbP(pathFn("/v1/kb/settings/verify")))))
	mux.Handle("GET /v1/kb/admin/list", authMW(kbRateMW(kbP(pathFn("/v1/kb/admin/list")))))
	mux.Handle("GET /v1/kb/{id}", authMW(kbRateMW(kbP(pathParam("/v1/kb")))))
	mux.Handle("PUT /v1/kb/{id}", authMW(kbRateMW(kbP(pathParam("/v1/kb")))))
	mux.Handle("DELETE /v1/kb/{id}", authMW(kbRateMW(kbP(pathParam("/v1/kb")))))
	mux.Handle("POST /v1/kb/{id}/documents", authMW(kbRateMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		pythonClient.ForwardRequest(w, r, "/v1/kb/"+escSeg(r.PathValue("id"))+"/documents?user_id="+url.QueryEscape(claims.UserID)+"&tenant_id="+url.QueryEscape(tenantStr(claims.TenantID)))
	}))))
	mux.Handle("GET /v1/kb/{id}/documents", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/documents")))))
	mux.Handle("POST /v1/kb/{id}/build", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/build")))))
	mux.Handle("POST /v1/kb/{id}/query", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/query")))))
	// 文档预览（两个路径参数，pathParamSuffix 只支持一个）
	mux.Handle("GET /v1/kb/{id}/documents/{doc_id}/preview", authMW(kbRateMW(kbP(func(r *http.Request) string {
		return "/v1/kb/" + escSeg(r.PathValue("id")) + "/documents/" + escSeg(r.PathValue("doc_id")) + "/preview"
	}))))
	// 知识库删除文档（P1 修复：Python 端已有 DELETE /{kb_id}/documents?doc_id=，
	// 网关此前缺失该路由导致前端删除文档 404）
	mux.Handle("PUT /v1/kb/{id}/visibility", authMW(kbRateMW(http.HandlerFunc(handleKBVisibility))))
	// 知识库启用/停用（用户开关，与索引状态 status 正交）。与 visibility 不同，这里
	// 不自己碰库：属主校验、enabled 列写入都在引擎侧一处实现，网关只做鉴权与转发，
	// 免得两处各判一次属主、口径漂移时就出现"网关放行、引擎拒绝"或反之。
	mux.Handle("PUT /v1/kb/{id}/status", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/status")))))
	// 索引进度（只读）：聚合在引擎侧按文档状态算（pageindex-service 无 job 端点可透传）。
	// 与上面的 PUT 同路径不同方法，语义正交（读索引状态 vs 写启用开关）。
	mux.Handle("GET /v1/kb/{id}/status", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/status")))))
	// 检索观测（P1-6）：近 24h / 7d 计数与失败原因 Top5，回答"为什么没检索到"。
	// 两条路径都转（引擎侧两条都注册）：`/v1/knowledge/{id}/stats` 是对外文档里的名字，
	// `/v1/kb/{id}/stats` 是本项目既有前缀。少了任一条，前端就会拿到 404 ——
	// 而 404 与"这项功能没做"在界面上长得一模一样。
	mux.Handle("GET /v1/kb/{id}/stats", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/stats")))))
	mux.Handle("GET /v1/knowledge/{id}/stats", authMW(kbRateMW(kbP(func(r *http.Request) string {
		return "/v1/knowledge/" + escSeg(r.PathValue("id")) + "/stats"
	}))))
	// 重新提交索引（失败文档 / 全库重建）。幂等与"同库并发 1"都在引擎侧实现，
	// 网关只做鉴权与转发 —— 两处各判一次就会出现"网关放行、引擎拒绝"。
	mux.Handle("POST /v1/kb/{id}/reindex", authMW(kbRateMW(kbP(pathParamSuffix("/v1/kb", "/reindex")))))
	mux.Handle("DELETE /v1/kb/{id}/documents", authMW(kbRateMW(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if pythonClient == nil {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil || claims.TenantID == 0 {
			Unauthorized(w, "missing tenant context")
			return
		}
		// 保留原始 doc_id 查询参数（newProxy 的 buildPath 会丢弃原始 query）
		target := "/v1/kb/" + escSeg(r.PathValue("id")) + "/documents?doc_id=" + url.QueryEscape(r.URL.Query().Get("doc_id")) +
			"&user_id=" + url.QueryEscape(claims.UserID) + "&tenant_id=" + url.QueryEscape(tenantStr(claims.TenantID))
		var resp interface{}
		if err := pythonClient.DeleteJSON(r.Context(), target, &resp); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
			return
		}
		OK(w, resp)
	}))))

	// Unified chat / quick-execute (六大工作台统一入口, proxies to Python TaskRouter)
	//
	// `afterResponse` 是这条链路的**用量记账**（此前完全没有）：回包里带
	// `usage`（引擎 `submit_task` 产出）→ 写 `usage_events`。挂在这里而不是新写
	// 一个专用 handler，是为了让"转发口径"和"记账口径"只有一处实现。
	chatP := newProxy("", proxyOpt{
		logTag:     "chat",
		mutateBody: InjectSessionRuntime,
		afterResponse: func(r *http.Request, reqBody map[string]interface{}, resp map[string]interface{}) {
			recordUnifiedUsageFromResponse(r, auth.GetClaims(r.Context()), reqBody, resp)
			if sid, _ := reqBody["session_id"].(string); sid != "" {
				syncProjectMirrorForSession(r.Context(), sid)
			}
		},
	})
	mux.Handle("POST /v1/chat/submit", authMW(rlMW(userConcurrencyMW(userSem, chatP(pathFn("/v1/chat/submit"))))))
	mux.Handle("GET /v1/chat/sessions/{id}/messages", authMW(rlMW(chatP(pathParamSuffix("/v1/chat/sessions", "/messages")))))
	// quick-execute 为 chat/submit 的语义别名（前端快捷执行入口）
	mux.Handle("POST /v1/quick-execute", authMW(rlMW(userConcurrencyMW(userSem, chatP(pathFn("/v1/chat/submit"))))))

	// Capabilities discovery (能力注册中心: 六大工作台能力发现)
	capP := newProxy("", proxyOpt{logTag: "capabilities"})
	mux.Handle("GET /v1/capabilities", authMW(rlMW(capP(pathFn("/v1/capabilities")))))
	mux.Handle("POST /v1/capabilities/search", authMW(rlMW(capP(pathFn("/v1/capabilities/search")))))

	// Memory (用户长期记忆 L2 档案卡: 列表/CRUD/语义检索/智能整理, 代理 Python)
	//
	// forwardQuery 是 C3「记忆按项目隔离」的**唯一**通路：前端 `projectParams()`
	// 把当前项目作为 `?project=` 挂在 URL 上，引擎侧 `_project()` 从 query 读它。
	// newProxy 默认丢弃原始 query，不透传的话项目维度到网关就断——记忆页选了项目
	// 仍显示未分组，新写入也落回未分组。
	//
	// 只放这三个键，身份（user_id/tenant_id）永远由网关追加：`archived` 是列表
	// 是否含归档，`limit` 是摘要条数（引擎 clamp 到 1..200），都不影响归属判定。
	memP := newProxy("", proxyOpt{logTag: "memory", forwardQuery: []string{"project", "archived", "limit"}})

	mux.Handle("GET /v1/memory/profile", authMW(rlMW(memP(pathFn("/v1/memory/profile")))))
	mux.Handle("POST /v1/memory/profile", authMW(rlMW(memP(pathFn("/v1/memory/profile")))))
	mux.Handle("PUT /v1/memory/profile", authMW(rlMW(memP(pathFn("/v1/memory/profile")))))
	mux.Handle("DELETE /v1/memory/profile/{id}", authMW(rlMW(memP(pathParam("/v1/memory/profile")))))
	// 范围切换（全局 ↔ 某个项目）。刻意**不走** PUT：PUT 按 `?project=` 精确匹配，
	// 项目视图里匹配不上全局条目（这正是"全局条目只读"的实现），而换范围要跨范围动作。
	mux.Handle("POST /v1/memory/profile/{id}/scope", authMW(rlMW(memP(pathParamSuffix("/v1/memory/profile", "/scope")))))
	mux.Handle("POST /v1/memory/profile/clear", authMW(rlMW(memP(pathFn("/v1/memory/profile/clear")))))
	mux.Handle("POST /v1/memory/search", authMW(rlMW(memP(pathFn("/v1/memory/search")))))
	mux.Handle("POST /v1/memory/organize", authMW(rlMW(memP(pathFn("/v1/memory/organize")))))
	mux.Handle("GET /v1/memory/organize/status", authMW(rlMW(memP(pathFn("/v1/memory/organize/status")))))
	mux.Handle("GET /v1/memory/summaries", authMW(rlMW(memP(pathFn("/v1/memory/summaries")))))
	mux.Handle("GET /v1/memory/conflicts", authMW(rlMW(memP(pathFn("/v1/memory/conflicts")))))
	mux.Handle("POST /v1/memory/conflicts/{conflict_id}/resolve", authMW(rlMW(memP(pathParamNamed("/v1/memory/conflicts", "conflict_id", "/resolve")))))
	mux.Handle("DELETE /v1/memory/conflicts/{conflict_id}", authMW(rlMW(memP(pathParamNamed("/v1/memory/conflicts", "conflict_id", "")))))

	// Isolation tier (本机沙箱档位：查询 / 切换 / 撤销)
	//
	// 为什么写操作要 admin 权限：档位是**Python 引擎进程级**的全局状态，一个用户
	// 改了会作用于该引擎上所有后续执行 —— 它不是 per-user 偏好。桌面端是单用户
	// 本地实例，用户即 owner，不受影响；多租户 SaaS 部署下这个限制是必要的。
	//
	// 读也要求 admin：响应里有本机路径（工作区、AppContainer 名）与能力矩阵，
	// 属于内部运行信息，与同组 /v1/system/health 口径一致。
	isoP := newProxy("", proxyOpt{logTag: "isolation"})
	mux.Handle("GET /v1/system/isolation", authMW(rlMW(RequirePermission(auth.PermAdminRead)(isoP(pathFn("/v1/system/isolation"))))))
	mux.Handle("PUT /v1/system/isolation", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(isoP(pathFn("/v1/system/isolation"))))))
	mux.Handle("DELETE /v1/system/isolation", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(isoP(pathFn("/v1/system/isolation"))))))
	mux.Handle("POST /v1/system/isolation/revoke", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(isoP(pathFn("/v1/system/isolation/revoke"))))))
	// 强制复测：清引擎侧冒烟缓存后重算 `selectable`。它**不改档位**，
	// 只解决"助手启用成功但档位仍灰着"的缓存滞留，所以读权限即可。
	mux.Handle("POST /v1/system/isolation/probe", authMW(rlMW(RequirePermission(auth.PermAdminRead)(isoP(pathFn("/v1/system/isolation/probe"))))))
	// 容器挡位的运行时**按需获取**：下载固定版本的 Kiri 包并校验 SHA-256（不安装）。
	// 属于运维/安全管理动作（会触达外网、在磁盘落安装包），用写权限。
	mux.Handle("POST /v1/system/isolation/container/prepare", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(isoP(pathFn("/v1/system/isolation/container/prepare"))))))

	// 提权助手（`restricted` 档的执行前置）：查询 / 启用（**会弹一次 UAC**）/ 停用。
	//
	// 这三条此前**没有注册**，于是设置页每次点「启用助手」都在网关拿到 404，
	// 前端 `catch` 落到兜底文案 —— 用户看到的是
	// 「受限令牌执行助手：启用助手失败：本机无法启用此档位」。
	// 它与管理员身份、令牌权限、`SeAssignPrimaryTokenPrivilege` **全都无关**：
	// 引擎侧的 `/v1/system/isolation/helper` 早就实现了，缺的只是网关这一跳。
	// 也就是说任何机器上都必然失败，而排障线索（"404"）被前端的兜底文案吃掉了。
	//
	// 三条都必须注册，缺任何一条都会退化成不同的假象：
	//   * 缺 GET ⇒ `loadHelper()` 失败，`helperStatus` 恒为 null，界面既不显示
	//     "助手挡住了 restricted"，也永远不会出现「启用助手」按钮；
	//   * 缺 POST ⇒ 点了也只会得到上面那句兜底文案；
	//   * 缺 DELETE ⇒ "停用助手"同样 404，用户无法撤销这条提权通道。
	//
	// 鉴权口径与同组隔离端点一致：读要 admin:read（响应里有本机路径与能力矩阵），
	// 启停要 admin:write（它是能改安全边界的动作）。
	helperPath := pathFn("/v1/system/isolation/helper")
	mux.Handle("GET /v1/system/isolation/helper", authMW(rlMW(RequirePermission(auth.PermAdminRead)(isoP(helperPath)))))
	mux.Handle("POST /v1/system/isolation/helper", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(isoP(helperPath)))))
	mux.Handle("DELETE /v1/system/isolation/helper", authMW(rlMW(RequirePermission(auth.PermAdminWrite)(isoP(helperPath)))))

	// 还原点回滚：把某个检查点重新应用到项目工作区。
	//
	// 这里**不走** `newProxy`：那条通道只把路径与身份透传给引擎，而引擎的回滚端点
	// 拿起 `checkpoint_id` 就直接写磁盘 —— 它无法判断这个 id 是不是调用者自己的。
	// 所以归属校验必须发生在网关（见 checkpoint_restore.go 文件头），handler 自己
	// 查一次 `(id, tenant, user)` 再转发。
	mux.Handle("POST /v1/checkpoints/{cid}/restore", authMW(rlMW(http.HandlerFunc(CheckpointRestoreHandler(pythonClient)))))

	// 还原点差异预览：**只读**，回答"回滚会动哪些文件"。归属校验与回滚同一判据
	// （见 checkpoint_diff.go 文件头）—— 引擎拿到 root 就会去读工作区，
	// 少了这道校验，猜到的 id 就能让引擎去快照别人的目录。
	mux.Handle("POST /v1/checkpoints/{cid}/diff", authMW(rlMW(http.HandlerFunc(CheckpointDiffHandler(pythonClient)))))
}
