package api

import (
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// SkillHandler proxies skill requests to the Python AI engine.
type SkillHandler struct {
	python *engine.PythonClient
}

func NewSkillHandler(python *engine.PythonClient) *SkillHandler {
	return &SkillHandler{python: python}
}

var validSkillName = regexp.MustCompile(`^[a-zA-Z0-9_.-]+$`)

// RegisterRoutes 注册技能路由。安全修复（P0-S2）：所有路由必须经过 authMW
// （技能即代码，未认证可 install/run = 未认证 RCE），并挂 rlMW 与 sanitizeMW。
func (h *SkillHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW, sanitizeMW routeMiddleware) {
	// 同时注册精确和尾斜杠变体：Go ServeMux 中 "/v1/skills/" 只匹配子树（不匹配 "/v1/skills"），
	// 而前端调用的是 GET /v1/skills，Python 端注册的也是 /v1/skills。
	mux.Handle("GET /v1/skills", authMW(rlMW(http.HandlerFunc(h.proxy))))
	mux.Handle("GET /v1/skills/", authMW(rlMW(http.HandlerFunc(h.proxy))))
	mux.Handle("POST /v1/skills/install", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	mux.Handle("POST /v1/skills/generate", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	mux.Handle("DELETE /v1/skills/{name}", authMW(rlMW(http.HandlerFunc(h.proxyDelete))))
	mux.Handle("GET /v1/skills/discover", authMW(rlMW(http.HandlerFunc(h.proxy))))
	// 启停（PUT）与运行（POST run）——技能工作台主链路
	mux.Handle("PUT /v1/skills/{name}", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	mux.Handle("POST /v1/skills/{name}/run", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.proxy)))))
	// 市场技能注册（P1 修复：前端 SkillMarketCard 调用 /v1/skills/{id}/register，
	// 此前两端均无此路由导致注册 404）
	mux.Handle("POST /v1/skills/{name}/register", authMW(rlMW(http.HandlerFunc(h.register))))
	// P1-2：技能升级（只对市场来源有效）。独立 handler 而不是走 proxy ——
	// proxy 的 POST 分支把身份写进 **body**，而引擎的 upgrade handler 从 **query**
	// 读 user_id / tenant_id（与 GET 一致），body 里的身份会被 FastAPI 直接丢掉。
	mux.Handle("POST /v1/skills/{name}/upgrade", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.upgrade)))))
	// P1-2：触发策略开关（auto_inject）—— 同样是"身份经 query"的 PATCH。
	mux.Handle("PATCH /v1/skills/{name}/meta", authMW(rlMW(sanitizeMW(http.HandlerFunc(h.patchMeta)))))
}

// patchMeta 转发元数据更新（`PATCH /v1/skills/{name}/meta`，body: {auto_inject}）。
func (h *SkillHandler) patchMeta(w http.ResponseWriter, r *http.Request) {
	if !h.prepare(w, r, "meta") {
		return
	}
	body := map[string]interface{}{}
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}
	h.forwardIdentity(w, r, "/v1/skills/"+r.PathValue("name")+"/meta", body, http.MethodPatch)
}

// prepare 做 name 校验与引擎可用性检查；校验失败时已写好响应，返回 false 即可结束。
//
// **先校验名字再看引擎**：非法名字（`../evil`）根本不该被转发，即便引擎不可用也应回
// 400 而不是 500 —— 否则调用方会把"自己传错了"理解成"服务坏了"。
func (h *SkillHandler) prepare(w http.ResponseWriter, r *http.Request, _ string) bool {
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "name is required")
		return false
	}
	if !validSkillName.MatchString(name) {
		BadRequest(w, "invalid skill name")
		return false
	}
	if h.python == nil {
		InternalError(w, "python engine not available")
		return false
	}
	return true
}

// forwardIdentity 把目标路径 + query 身份 + 请求体转发给引擎（POST / PATCH）。
//
// 为什么不用 proxy：proxy 把身份塞进 body，而这两个引擎端点从 **query** 读身份
// （FastAPI 把非模型参数的 handler 参数当 query），identity 写 body 等于没传 ——
// 引擎会回退到全局共享层，把技能改到"所有人"那一层去。
func (h *SkillHandler) forwardIdentity(
	w http.ResponseWriter, r *http.Request, target string, body map[string]interface{}, method string,
) {
	if claims := auth.GetClaims(r.Context()); claims != nil {
		target += "?" + identityQuery(claims)
	}
	var result map[string]interface{}
	var err error
	if method == http.MethodPatch {
		err = h.python.PatchJSON(r.Context(), target, body, &result)
	} else {
		err = h.python.PostJSON(r.Context(), target, body, &result)
	}
	if err != nil {
		writeSkillEngineError(w, "skill meta", err)
		return
	}
	OK(w, result)
}

// writeSkillEngineError 把引擎的 4xx（尤其升级端点的 409「不可升级 / 没有来源」）
// 原样透出，而不是一律折成 500 —— 前端要按状态码显示可操作的提示。
func writeSkillEngineError(w http.ResponseWriter, prefix string, err error) {
	var up *upstreamError
	if errors.As(classifyUpstream(prefix, err), &up) {
		JSON(w, up.status, APIResponse{Success: false, Error: up.detail})
		return
	}
	logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
}

// upgrade 转发技能升级请求到引擎（`POST /v1/skills/{name}/upgrade`）。
//
// 身份走 query（引擎侧从 query 读），请求体原样透传（`source_url` 可选）。
func (h *SkillHandler) upgrade(w http.ResponseWriter, r *http.Request) {
	if !h.prepare(w, r, "upgrade") {
		return
	}
	body := map[string]interface{}{}
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}
	h.forwardIdentity(w, r, "/v1/skills/"+r.PathValue("name")+"/upgrade", body, http.MethodPost)
}

// register 将能力注册中心的技能注册为本地技能（转发 Python /v1/skills/{name}/register）。
func (h *SkillHandler) register(w http.ResponseWriter, r *http.Request) {
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}
	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "name is required")
		return
	}
	if !validSkillName.MatchString(name) {
		BadRequest(w, "invalid skill name")
		return
	}
	claims := auth.GetClaims(r.Context())
	target := "/v1/skills/" + name + "/register"
	if claims != nil {
		target += "?user_id=" + url.QueryEscape(claims.UserID) + "&tenant_id=" + url.QueryEscape(tenantStr(claims.TenantID))
	}
	var body map[string]interface{}
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	} else {
		body = map[string]interface{}{}
	}
	var result map[string]interface{}
	if err := h.python.PostJSON(r.Context(), target, body, &result); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, result)
}

// proxy forwards the request to the Python engine.
// identityParams 构造网关注入的身份参数（`user_id` / `tenant_id`）。
//
// 引擎无鉴权，网关是**唯一可信边界**：POST/PUT 把身份写进 body（见 proxy），
// 而 GET/DELETE 没有 body，必须走 query —— 引擎侧 handler 正是从 query 读这两个字段
// （如 app/api/skills.py 的 `list_skills(user_id, tenant_id)`）。
//
// 历史缺陷：GET / DELETE 此前**不传任何身份**，引擎收到空 user_id/tenant_id 后
// 一律回退全局共享层 —— 用户看不到自己的私有技能与租户共享技能（删除也打不准目标层）。
func identityParams(r *http.Request) url.Values {
	q := url.Values{}
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		return q
	}
	if claims.UserID != "" {
		q.Set("user_id", claims.UserID)
	}
	if tid := ResolveTenantID(r); tid != 0 {
		q.Set("tenant_id", tenantStr(tid))
	}
	return q
}

// forwardedQuery 合并客户端原始 query 与网关注入的身份参数。
//
// 历史缺陷：GET 分支此前只转发 identityParams(r).Encode()，把路径上客户端带的 query
// **整个丢掉**。表现是 `GET /v1/skills/discover?url=...` 到引擎时已经没有 url，
// 引擎于是走"列出本地技能"分支 —— 远端目录永远发现不到，而且**不报错**（静默降级，
// 前端只会看到一份与本地列表一模一样的"发现结果"）。
//
// 身份参数后写覆盖：引擎无鉴权，网关是唯一可信边界，客户端不能靠 query 里的
// user_id / tenant_id 冒充别人（见 identityParams 的说明）。
func forwardedQuery(r *http.Request) url.Values {
	q := r.URL.Query()
	for k, vs := range identityParams(r) {
		q.Del(k)
		for _, v := range vs {
			q.Add(k, v)
		}
	}
	return q
}

// withQuery 把 query 追加到 path（path 可能已自带 query）。
func withQuery(path, query string) string {
	if query == "" {
		return path
	}
	if strings.Contains(path, "?") {
		return path + "&" + query
	}
	return path + "?" + query
}

func (h *SkillHandler) proxy(w http.ResponseWriter, r *http.Request) {
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	var result map[string]interface{}
	var err error

	switch r.Method {
	case "GET":
		// 规范化转发路径：Python 端注册的是 /v1/skills（无尾斜杠）
		basePath := strings.TrimSuffix(r.URL.Path, "/")
		// 身份注入：GET 无 body，只能走 query（见 identityParams 的说明）。
		// 客户端原始 query 必须一并转发（如 discover 的 `url` / 删除的 `scope`）。
		path := withQuery(basePath, forwardedQuery(r).Encode())
		err = h.python.GetJSON(r.Context(), path, &result)
	case "POST", "PUT":
		var body map[string]interface{}
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
		// 身份注入（网关为唯一可信边界）：引擎无鉴权，必须由网关注入租户/用户身份。
		if tid := ResolveTenantID(r); tid != 0 {
			body["tenant_id"] = tid
		}
		if uid := auth.GetClaims(r.Context()); uid != nil && uid.UserID != "" {
			body["user_id"] = uid.UserID
		}
		if r.Method == "PUT" {
			err = h.python.PutJSON(r.Context(), r.URL.Path, body, &result)
		} else {
			err = h.python.PostJSON(r.Context(), r.URL.Path, body, &result)
		}
	default:
		BadRequest(w, "unsupported method")
		return
	}

	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, result)
}

// proxyDelete forwards DELETE requests to the Python engine.
//
// `?scope=tenant` 删的是**租户共享层**（团队资产），因此需要技能管理权限；
// 缺省 / `scope=private` 仍是调用者私有目录，保持既有行为（任何登录用户可删自己的）。
func (h *SkillHandler) proxyDelete(w http.ResponseWriter, r *http.Request) {
	if h.python == nil {
		InternalError(w, "python engine not available")
		return
	}

	name := r.PathValue("name")
	if name == "" {
		BadRequest(w, "name is required")
		return
	}
	if !validSkillName.MatchString(name) {
		BadRequest(w, "invalid skill name")
		return
	}

	scope := r.URL.Query().Get("scope")
	if scope == "tenant" {
		claims := auth.GetClaims(r.Context())
		if !auth.HasPermission(claims, auth.PermMarketManage) {
			Forbidden(w, "insufficient permissions to delete tenant-shared skills")
			return
		}
	}

	// DELETE 无 body：身份与 scope 都经 query 下发（见 identityParams）。
	params := identityParams(r)
	if scope != "" {
		params.Set("scope", scope)
	}

	var result map[string]interface{}
	if err := h.python.DeleteJSON(
		r.Context(), withQuery("/v1/skills/"+name, params.Encode()), &result,
	); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "python engine error")
		return
	}
	OK(w, result)
}
