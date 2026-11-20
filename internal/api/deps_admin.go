package api

// deps_admin.go —— 后台「Python 依赖管理」的网关代理。
//
// 权威实现在引擎（`python-engine/app/api/deps.py`）：pip 跑在**引擎自己的解释器**里，
// 所以"装包装到哪儿"只有一个答案。网关这一层只做四件事，每件都在下面对应位置写明理由：
//
//  1. **权限**：平台级读写门禁（`RequirePlatformOwner`）。装/卸包会改变整台机器上所有
//     租户共用的运行环境 —— 这不是租户 admin 该有的权力，与 `database/query`、
//     `redis/flush-all` 同一口径。
//  2. **令牌透传**：引擎端点要求 `X-Internal-Token`。用 `PythonClient.GetJSON/PostJSON`
//     （而不是 `ForwardRequest`）既自动注入该 token，又不会把客户端的任何头带过去。
//  3. **审计**：install/uninstall 是**改变运行环境**的操作，必须能回答"谁在什么时候
//     装/卸了什么"。走既有 `db.AuditLog`（Redis Stream `audit:events`），与
//     `gateway.reset_breaker` 同款。
//  4. **互斥**：同一时刻只允许一个变更任务进入引擎。引擎自己也有单任务闸（跨进程有效），
//     这里这把是"同一网关进程内不再往上游打第二个请求"的前置闸 —— 两道闸的分工必须写清楚，
//     否则后人会以为其中一道是多余的。
//
// 注意：本层**不做**任何 pip 调用，也不解析引擎的响应体结构；透传引擎返回的 JSON
// （用 `OK()` 包一层 `data`），前端按 `data.*` 取用。

import (
	"encoding/json"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// 与引擎 `app/api/deps.py` 的 `PIP_SPEC_RE` / `PACKAGE_NAME_RE` **同一语义**。
//
// 为什么网关也要查一遍：引擎是权威（它直接构造 pip 参数），但网关是最先看到请求的地方，
// 在入口处拒绝非法规格可以让"注入尝试"落在网关日志里而不是引擎的 400 里；也让前端拿到
// 更快的失败。两处必须保持一致 —— 放宽任何一处，另一处的检查就变成摆设。
//
// 名称必须以字母/数字开头结尾：否则 `.env` / `-r` 这类"由名称字符合法组成"的串会漏过去，
// 而 pip 会把它们当本地路径或选项处理（`pip install .env` = 从本地目录安装）。
var (
	depsSpecRe = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:[<>=!~]=?[A-Za-z0-9.*+_-]+)?$`)
	depsNameRe = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$`)
	// 任务 id 由引擎生成（12 位 hex）；这里只做形态约束，防止路径拼接穿越。
	depsJobIDRe = regexp.MustCompile(`^[A-Za-z0-9-]{1,64}$`)
	// 快照 id 由引擎生成：`<UTC 时间戳到毫秒>Z-<6 位小写随机>`（见引擎的
	// `SNAPSHOT_ID_RE`）。**刻意不含 `.` 与 `/`** —— id 会被拼进上游 URL 的路径段，
	// 放过点号就等于放过 `..`。前后两份正则必须一起改。
	depsSnapshotIDRe = regexp.MustCompile(`^[0-9]{8}T[0-9]{9}Z-[a-z0-9]{6}$`)
)

const (
	maxDepsSpecLen = 128
	maxDepsNameLen = 128
	// 与引擎 `MAX_SNAPSHOT_LABEL_LEN` 一致：标签只用于展示，超长直接拒绝而不是截断
	// （截断会让用户以为标签被正确保存）。
	maxDepsSnapshotLabelLen = 120
)

// DepsHandler 代理依赖管理端点。
type DepsHandler struct {
	pythonClient *engine.PythonClient
	// busy 是"同一时刻只允许一个安装/卸载请求"的**进程内**闸门（1 = 有请求在途）。
	//
	// 它与引擎侧 `_running_job_id` 的分工：引擎那把保证**全站**只有一个任务（安装是
	// 后台任务，会持续数分钟，闸在引擎里才有意义）；这把只覆盖"网关收到请求 → 上游返回"
	// 这个短窗口，用来挡住同一用户的连点/并发提交，避免在引擎已经忙时白白打一轮上游。
	busy int32
}

// depsEngineStatusRe 从 PythonClient 的错误串里取回上游状态码。
// 形态固定为 `python POST <path> returned <code>: <body>`（见 internal/engine/python_client.go）。
var depsEngineStatusRe = regexp.MustCompile(`returned (\d{3}): ([\s\S]*)$`)

// depsEngineDetail 从引擎错误体里取出给用户看的文案。
//
// 引擎的 4xx 是 FastAPI 的 `{"detail": "..."}`（安装/卸载失败时 detail 是对象，带
// error 与 output）。这两类都要能显示 —— 只回一句 "internal error" 会让用户无从知道
// 是版本不存在还是网络断了。
func depsEngineDetail(body string) string {
	body = strings.TrimSpace(body)
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(body), &payload); err == nil {
		switch d := payload["detail"].(type) {
		case string:
			if d != "" {
				return d
			}
		case map[string]interface{}:
			if msg, ok := d["error"].(string); ok && msg != "" {
				return msg
			}
		}
	}
	if len(body) > 400 {
		return body[:400]
	}
	if body == "" {
		return "python engine error"
	}
	return body
}

// depsEngineFailure 把上游错误映射成合适的网关响应。
//
// 400/404/409/503 原样透传（那是引擎给出的**业务结论**：参数非法 / 任务不存在 /
// 已有任务在跑 / 引擎未就绪），其余一律 502 —— 502 的语义是"引擎这一侧出了问题"，
// 与"你的请求有问题"处置不同。
func depsEngineFailure(w http.ResponseWriter, err error) {
	m := depsEngineStatusRe.FindStringSubmatch(err.Error())
	if m == nil {
		logAndRespond(w, err, http.StatusBadGateway, "python engine unreachable")
		return
	}
	status, _ := strconv.Atoi(m[1])
	detail := depsEngineDetail(m[2])
	switch status {
	case http.StatusBadRequest, http.StatusNotFound, http.StatusConflict, http.StatusServiceUnavailable:
		JSON(w, status, APIResponse{Success: false, Error: detail})
	default:
		// 上游 5xx 不原样回给浏览器（详情进日志），对外统一 502。
		logAndRespond(w, err, http.StatusBadGateway, "python engine error: "+detail)
	}
}

// auditDeps 记一条审计（成功/失败都要）。
//
// `db.AuditLog` 的 `Success` 字段恒为 true，因此结果放进 meta —— 运维排查"谁装了个坏包"
// 时，`success=false` 的那条同样有意义（它说明有人尝试过）。
func auditDeps(r *http.Request, action, resource, detail string, ok bool) {
	claims := auth.GetClaims(r.Context())
	userID := ""
	var tenantID int16
	if claims != nil {
		userID = claims.UserID
		tenantID = claims.TenantID
	}
	db.AuditLog(r.Context(), userID, tenantID, action, resource, detail, r.RemoteAddr,
		map[string]interface{}{"success": ok})
}

// requireEngine 统一判空：引擎不可用时给 503 而不是 500（前端据此提示"引擎未就绪"）。
func (h *DepsHandler) requireEngine(w http.ResponseWriter) bool {
	if h.pythonClient == nil {
		ServiceUnavailable(w, "python engine not available")
		return false
	}
	return true
}

// List GET /v1/admin/deps/list
func (h *DepsHandler) List(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	var resp map[string]interface{}
	if err := h.pythonClient.GetJSON(r.Context(), "/internal/deps/list", &resp); err != nil {
		depsEngineFailure(w, err)
		return
	}
	OK(w, resp)
}

// Search GET /v1/admin/deps/search?q=<name>
//
// PyPI 的 JSON API 是**精确项目名**查询（没有关键词搜索端点），所以这里把 q 当作包名
// 校验后透传；引擎负责缓存与"查无此包 / 网络不可用"的区分。
func (h *DepsHandler) Search(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	q := strings.TrimSpace(r.URL.Query().Get("q"))
	if q == "" {
		BadRequest(w, "q is required")
		return
	}
	if len(q) > maxDepsNameLen || !depsNameRe.MatchString(q) {
		BadRequest(w, "q must be an exact package name (letters, digits, dot, underscore or hyphen)")
		return
	}
	var resp map[string]interface{}
	if err := h.pythonClient.GetJSON(
		r.Context(), "/internal/deps/search?q="+url.QueryEscape(q), &resp,
	); err != nil {
		depsEngineFailure(w, err)
		return
	}
	OK(w, resp)
}

// Install POST /v1/admin/deps/install {"spec": "requests==2.31.0"}
//
// 引擎立即返回 job（安装是后台任务），前端轮询 Job 端点取进度。
func (h *DepsHandler) Install(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	var body struct {
		Spec string `json:"spec"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	spec := strings.TrimSpace(body.Spec)
	if len(spec) > maxDepsSpecLen || !depsSpecRe.MatchString(spec) {
		BadRequest(w, "invalid package spec: expected a package name optionally followed by one version operator")
		return
	}

	if !atomic.CompareAndSwapInt32(&h.busy, 0, 1) {
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "another dependency task is already running",
		})
		return
	}
	defer atomic.StoreInt32(&h.busy, 0)

	var resp map[string]interface{}
	err := h.pythonClient.PostJSON(
		r.Context(), "/internal/deps/install", map[string]string{"spec": spec}, &resp,
	)
	if err != nil {
		auditDeps(r, "deps.install", "pip:"+spec, "install rejected or failed: "+err.Error(), false)
		depsEngineFailure(w, err)
		return
	}
	auditDeps(r, "deps.install", "pip:"+spec, "install job queued", true)
	OK(w, resp)
}

// Uninstall POST /v1/admin/deps/uninstall {"name": "requests"}
func (h *DepsHandler) Uninstall(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	var body struct {
		Name string `json:"name"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	name := strings.TrimSpace(body.Name)
	if len(name) > maxDepsNameLen || !depsNameRe.MatchString(name) {
		BadRequest(w, "invalid package name: version specifiers, spaces, paths and URLs are not allowed")
		return
	}

	if !atomic.CompareAndSwapInt32(&h.busy, 0, 1) {
		JSON(w, http.StatusConflict, APIResponse{
			Success: false,
			Error:   "another dependency task is already running",
		})
		return
	}
	defer atomic.StoreInt32(&h.busy, 0)

	var resp map[string]interface{}
	err := h.pythonClient.PostJSON(
		r.Context(), "/internal/deps/uninstall", map[string]string{"name": name}, &resp,
	)
	if err != nil {
		auditDeps(r, "deps.uninstall", "pip:"+name, "uninstall failed: "+err.Error(), false)
		depsEngineFailure(w, err)
		return
	}
	auditDeps(r, "deps.uninstall", "pip:"+name, "uninstalled", true)
	OK(w, resp)
}

// Job GET /v1/admin/deps/jobs/{id}
func (h *DepsHandler) Job(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	id := r.PathValue("id")
	if !depsJobIDRe.MatchString(id) {
		BadRequest(w, "invalid job id")
		return
	}
	var resp map[string]interface{}
	if err := h.pythonClient.GetJSON(r.Context(), "/internal/deps/jobs/"+escSeg(id), &resp); err != nil {
		depsEngineFailure(w, err)
		return
	}
	OK(w, resp)
}

// ListSnapshots GET /v1/admin/deps/snapshots
//
// 快照清单（最新在前）。读操作用平台级读门禁：清单本身不含包内容，但"何时有人动过
// 运行环境"属于运维信息，与已安装包列表同口径。
func (h *DepsHandler) ListSnapshots(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	var resp map[string]interface{}
	if err := h.pythonClient.GetJSON(r.Context(), "/internal/deps/snapshots", &resp); err != nil {
		depsEngineFailure(w, err)
		return
	}
	OK(w, resp)
}

// CreateSnapshot POST /v1/admin/deps/snapshots {"label": "before upgrade"}
//
// 手动建快照是**写**操作（会在数据目录落文件），因此走平台级写门禁并记审计：
// "谁在什么时候给运行环境拍了快照"与"谁装了包"同样需要可回溯。
func (h *DepsHandler) CreateSnapshot(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	var body struct {
		Label string `json:"label"`
	}
	// 空 body 是合法的（标签可选）：Decoder 遇到 EOF 时保持零值。
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}
	label := strings.TrimSpace(body.Label)
	if len(label) > maxDepsSnapshotLabelLen {
		BadRequest(w, "label is too long (max 120 characters)")
		return
	}

	var resp map[string]interface{}
	err := h.pythonClient.PostJSON(
		r.Context(), "/internal/deps/snapshots", map[string]string{"label": label}, &resp,
	)
	if err != nil {
		auditDeps(r, "deps.snapshot", "pip:snapshot", "snapshot failed: "+err.Error(), false)
		depsEngineFailure(w, err)
		return
	}
	auditDeps(r, "deps.snapshot", "pip:snapshot", "snapshot created", true)
	OK(w, resp)
}

// RestoreSnapshot POST /v1/admin/deps/snapshots/{id}/restore {"dry_run": true}
//
// `dry_run=true` 只算差异（前端二次确认弹窗的数据来源），不占用互斥闸；
// `dry_run=false` 真的建恢复任务 —— 那是**改变运行环境**的操作，必须过互斥闸并记审计。
// 引擎侧还有一道全站单任务闸（跨进程有效），两把闸的分工见文件头注释。
func (h *DepsHandler) RestoreSnapshot(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	id := r.PathValue("id")
	if !depsSnapshotIDRe.MatchString(id) {
		BadRequest(w, "invalid snapshot id")
		return
	}

	var body struct {
		DryRun bool `json:"dry_run"`
	}
	if r.ContentLength > 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}

	if !body.DryRun {
		if !atomic.CompareAndSwapInt32(&h.busy, 0, 1) {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "another dependency task is already running",
			})
			return
		}
		defer atomic.StoreInt32(&h.busy, 0)
	}

	var resp map[string]interface{}
	err := h.pythonClient.PostJSON(
		r.Context(),
		"/internal/deps/snapshots/"+escSeg(id)+"/restore",
		map[string]bool{"dry_run": body.DryRun},
		&resp,
	)
	if err != nil {
		if !body.DryRun {
			auditDeps(r, "deps.restore", "pip:snapshot:"+id, "restore rejected or failed: "+err.Error(), false)
		}
		depsEngineFailure(w, err)
		return
	}
	if !body.DryRun {
		auditDeps(r, "deps.restore", "pip:snapshot:"+id, "restore job queued", true)
	}
	OK(w, resp)
}

// registerDepsRoutes 注册依赖管理路由（全部为管理员端点）。
//
// 读（list/search/jobs/snapshots）用平台级读门禁，写（install/uninstall/创建快照/
// 恢复快照）用平台级写门禁：已安装清单与 PyPI 查询只暴露"这台机器装了什么"，但列表里
// 可能含内部工具包名，与 `/v1/admin/performance` 同口径收紧到平台 owner。
func registerDepsRoutes(
	mux *http.ServeMux,
	authMW, rlMW routeMiddleware,
	pythonClient *engine.PythonClient,
) {
	h := &DepsHandler{pythonClient: pythonClient}
	readMW := RequirePlatformOwner(auth.PermAdminRead)
	writeMW := RequirePlatformOwner(auth.PermAdminWrite)

	mux.Handle("GET /v1/admin/deps/list", authMW(rlMW(readMW(http.HandlerFunc(h.List)))))
	mux.Handle("GET /v1/admin/deps/search", authMW(rlMW(readMW(http.HandlerFunc(h.Search)))))
	mux.Handle("GET /v1/admin/deps/jobs/{id}", authMW(rlMW(readMW(http.HandlerFunc(h.Job)))))
	mux.Handle("GET /v1/admin/deps/snapshots", authMW(rlMW(readMW(http.HandlerFunc(h.ListSnapshots)))))
	mux.Handle("POST /v1/admin/deps/install", authMW(rlMW(writeMW(http.HandlerFunc(h.Install)))))
	mux.Handle("POST /v1/admin/deps/uninstall", authMW(rlMW(writeMW(http.HandlerFunc(h.Uninstall)))))
	mux.Handle("POST /v1/admin/deps/snapshots", authMW(rlMW(writeMW(http.HandlerFunc(h.CreateSnapshot)))))
	mux.Handle(
		"POST /v1/admin/deps/snapshots/{id}/restore",
		authMW(rlMW(writeMW(http.HandlerFunc(h.RestoreSnapshot)))),
	)
}
