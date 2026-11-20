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
	"errors"
	"net/http"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

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

// depsEngineConflicts 取引擎 409 里的**冲突文本**（`detail.conflicts`）。
//
// 为什么单独取而不是塞进 error 文案：`pip check` 的输出是多行、带包名与版本约束，
// 前端要按 `<pre>` 原样展示（并允许用户复制去修）。混进一句话的错误里会丢换行，
// 也让人以为那是网关拼出来的结论。放 `params.conflicts` 与既有错误码参数同口径。
func depsEngineConflicts(body string) string {
	body = strings.TrimSpace(body)
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(body), &payload); err != nil {
		return ""
	}
	detail, ok := payload["detail"].(map[string]interface{})
	if !ok {
		return ""
	}
	conflicts, ok := detail["conflicts"].(string)
	if !ok {
		return ""
	}
	return strings.TrimSpace(conflicts)
}

// depsEngineFailure 把上游错误映射成合适的网关响应。
//
// 判据顺序（**必须先分类再取文案**）：
//
//  1. 拿到响应 = `engine.UpstreamError`（带状态码）。401/404 之类的**业务结论**原样透传；
//     5xx 一对一转成可读文案，而不是统统折成 "unreachable"。
//  2. 没拿到响应 = `engine.TransportError`。再分"超时"（引擎在跑但慢，pip 冷启动）
//     与"连不上"（进程没起/地址错）—— 这两种的处置完全不同。
//  3. 其它（本包未分类的错误）：保留旧的错误串解析兜底，文案不猜原因。
//
// 修复前的形态：5xx 会走重试，耗尽后错误串是 `request failed after 4 retries:
// server error 502`，不匹配 `returned <code>:` 正则 ⇒ 一律落到 "python engine
// unreachable"。而真实情况恰恰是**引擎活着**、pip list 自己失败/超时（引擎回 502）。
func depsEngineFailure(w http.ResponseWriter, err error) {
	var up *engine.UpstreamError
	if errors.As(err, &up) {
		depsUpstreamFailure(w, err, up)
		return
	}

	switch engine.FailureKindOf(err) {
	case engine.FailureTimeout:
		logAndRespondRetryable(w, err, http.StatusGatewayTimeout,
			"python engine timed out while running the dependency command — retry shortly (pip cold start can take a while)",
			boolPtr(true))
	case engine.FailureUnreachable:
		logAndRespondRetryable(w, err, http.StatusBadGateway,
			"python engine unreachable: connection failed (engine process down or address misconfigured)",
			boolPtr(true))
	default:
		// 兜底：错误串是旧格式（`python GET <path> returned <code>: <body>`）时
		// 仍能按状态码处置 —— 本包之外构造的错误、以及历史包装点都走这里。
		if m := depsEngineStatusRe.FindStringSubmatch(err.Error()); m != nil {
			status, _ := strconv.Atoi(m[1])
			depsUpstreamFailure(w, err, &engine.UpstreamError{
				Method: "<unknown>", Path: "<unknown>", Status: status, Body: m[2],
			})
			return
		}
		// 响应体解不出来（引擎给了 200 但不是 JSON，常见于反向代理的错误页）：
		// 这说明"契约对不上"，而不是引擎忙/网络断，文案要能区分。
		var syn *json.SyntaxError
		var typ *json.UnmarshalTypeError
		if errors.As(err, &syn) || errors.As(err, &typ) {
			logAndRespond(w, err, http.StatusBadGateway,
				"python engine returned a malformed response (not JSON)")
			return
		}
		logAndRespond(w, err, http.StatusBadGateway, "python engine request failed")
	}
}

// depsUpstreamFailure 处理"引擎确实回了非 2xx"的情况。
//
// 状态码 → 对外文案（错误码矩阵见下）：
//
//	400/404/409 → 原样透传 + 引擎 detail（参数非法 / 路由或任务不存在 / 已有任务）
//	401/403     → 502 + 提示令牌不一致：这是**网关→引擎**的凭据问题（INTERNAL_TOKEN），
//	              不是用户请求的问题，回 401 会让用户以为要重新登录
//	429         → 503 + 可重试：引擎在限流，过一会儿再来
//	502         → 502 + 引擎 detail（pip list 失败/超时会被引擎包成 502，这条最有信息量）
//	503         → 503 + detail（引擎自己说未就绪，如 INTERNAL_TOKEN 未配置）
//	504         → 504 + 可重试（上游超时）
//	其它 5xx    → 502 笼统文案：**不**透出响应体（可能是堆栈/内部路径）
func depsUpstreamFailure(w http.ResponseWriter, err error, up *engine.UpstreamError) {
	// 只信引擎自己产的 JSON 体：代理/反向代理的 HTML 错误页不该被当成用户提示。
	body, jsonOK := depsEngineBody(up.Body)
	detail := ""
	if jsonOK {
		detail = depsEngineDetail(body)
	}

	switch up.Status {
	case http.StatusBadRequest, http.StatusNotFound, http.StatusConflict:
		msg := detail
		if msg == "" {
			msg = http.StatusText(up.Status)
		}
		// 409 可能是"已有任务在跑"，也可能是"环境已依赖冲突"（引擎跑过 pip check）。
		// 后者带着多行冲突文本，必须原样透传：前端要把它摆给用户去修，而不是只说
		// "冲突了"。其余 409/400/404 没有 conflicts，params 留空不影响既有契约。
		resp := APIResponse{Success: false, Error: msg}
		if conflicts := depsEngineConflicts(body); conflicts != "" {
			resp.Params = map[string]interface{}{"conflicts": conflicts}
		}
		JSON(w, up.Status, resp)
	case http.StatusUnauthorized, http.StatusForbidden:
		logAndRespond(w, err, http.StatusBadGateway,
			"python engine rejected the gateway credentials ("+strconv.Itoa(up.Status)+
				"): X-Internal-Token mismatch between gateway and engine")
	case http.StatusTooManyRequests:
		logAndRespondRetryable(w, err, http.StatusServiceUnavailable,
			"python engine is throttling gateway requests (429) — retry shortly", boolPtr(true))
	case http.StatusServiceUnavailable:
		msg := "python engine not ready (503)"
		if detail != "" {
			msg = detail
		}
		logAndRespondRetryable(w, err, http.StatusServiceUnavailable, msg, boolPtr(true))
	case http.StatusGatewayTimeout:
		logAndRespondRetryable(w, err, http.StatusGatewayTimeout,
			"python engine timed out (504)", boolPtr(true))
	case http.StatusBadGateway:
		msg := "python engine reported a failure (502)"
		if detail != "" {
			msg = "python engine error: " + detail
		}
		logAndRespond(w, err, http.StatusBadGateway, msg)
	default:
		// 其它 5xx：状态码写进文案（运维据此判断是 500 还是别的），响应体不外泄。
		logAndRespond(w, err, http.StatusBadGateway,
			"python engine reported an internal error ("+strconv.Itoa(up.Status)+")")
	}
}

// depsEngineBody 整份响应体是否是 JSON（引擎产物的最低特征）。
func depsEngineBody(raw string) (string, bool) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return "", false
	}
	var probe map[string]interface{}
	if json.Unmarshal([]byte(trimmed), &probe) != nil {
		return "", false
	}
	return trimmed, true
}

func boolPtr(b bool) *bool { return &b }

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

// depsCallTimeout 是依赖管理端点单次调用的**最短**整体超时预算。
//
// 为什么必须比普通端点长：pip 跑在引擎自己的解释器里，`pip list` 冷启动要扫一遍
// site-packages（首次可达数十秒），`uninstall` 引擎侧给 120s，恢复快照的批量安装
// 给到 900s。而网关同步客户端默认整体超时来自 `PYTHON_ENGINE_TIMEOUT`（5 分钟），
// 在某些部署里会被调小到 30–60s —— 那时"引擎正在正常跑 pip"会被客户端先掐掉，
// 表现成 `context deadline exceeded`，再被误读成"引擎不可达"。
//
// 语义是下界而不是硬值：`depsRequestOptions` 取 max(客户端配置超时, 本值)，
// 不会把部署方放宽的 `PYTHON_ENGINE_TIMEOUT` 又压回去。
const depsCallTimeout = 150 * time.Second

// depsRequestOptions 是依赖管理代理调用的统一策略。
//
//   - 长超时：见 depsCallTimeout（pip 冷启动慢）。
//   - 不重试：上游 5xx/429 在这里几乎都是 pip 自身的结论（超时/编译失败），
//     重试 4 次等于把同一堵墙撞 4 遍 —— 每次都要再等一个 pip 超时，用户看到的是
//     "转圈两分钟然后报不可达"。
//   - 不改熔断：这是后台只读/管理工作流，一次失败不该把引擎地址冷却 5s 从而
//     波及正在进行的对话（会话流式请求与后台刷新共用同一张地址表）。
func depsRequestOptions(pc *engine.PythonClient) []engine.RequestOption {
	timeout := depsCallTimeout
	if pc != nil {
		if configured := pc.SyncTimeout(); configured > timeout {
			timeout = configured
		}
	}
	return []engine.RequestOption{
		engine.WithRequestTimeout(timeout),
		engine.WithoutRetry(),
		engine.WithoutCooldown(),
	}
}

// opts 便捷入口（handler 内部使用）。
func (h *DepsHandler) opts() []engine.RequestOption {
	return depsRequestOptions(h.pythonClient)
}

// List GET /v1/admin/deps/list
func (h *DepsHandler) List(w http.ResponseWriter, r *http.Request) {
	if !h.requireEngine(w) {
		return
	}
	var resp map[string]interface{}
	if err := h.pythonClient.GetJSON(r.Context(), "/internal/deps/list", &resp, h.opts()...); err != nil {
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
		r.Context(), "/internal/deps/search?q="+url.QueryEscape(q), &resp, h.opts()...,
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
		r.Context(), "/internal/deps/install", map[string]string{"spec": spec}, &resp, h.opts()...,
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
		r.Context(), "/internal/deps/uninstall", map[string]string{"name": name}, &resp, h.opts()...,
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
	if err := h.pythonClient.GetJSON(r.Context(), "/internal/deps/jobs/"+escSeg(id), &resp, h.opts()...); err != nil {
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
	if err := h.pythonClient.GetJSON(r.Context(), "/internal/deps/snapshots", &resp, h.opts()...); err != nil {
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
		r.Context(), "/internal/deps/snapshots", map[string]string{"label": label}, &resp, h.opts()...,
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
		h.opts()...,
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
