package api

// diagnostics_summary.go —— 「配了但不生效」的聚合清单（P0-1）。
//
//	GET /v1/admin/diagnostics/summary
//
// 与 GET /v1/admin/diagnostics（`diagnostics_handler.go`）的分工：
//
//   - 后者是**逐项体检**：依赖、配置、能力，一条一条说；
//   - 这里是**按主题聚合的五行清单**：schema / 必需表 / 知识库 / 检查点 / 依赖，
//     每行只回答一件事 —— 它到底是"好的"、"可疑的"、"坏的"，还是"看不见"。
//
// 为什么不复用 `/diagnostics` 的结论：那一份的粒度是"某个开关有没有配"，而运维
// 真正要的是一个可勾选的清单（schema 升了没、表齐了没、知识库服务起了没、检查点
// 还在不在写）。两者的判据不同，硬塞进一个端点会让任一侧的语义被拉扯。
//
// ## 三条硬规则
//
//  1. **必需表清单不在 Go 侧复制**。权威定义在 `python-engine/app/db.py::REQUIRED_TABLES`，
//     这里只调引擎的 `/internal/diagnostics/tables`（与启动自检同源）。复制一份的
//     下场是两边漂移：迁移加了表、引擎改了，而 Go 那副本还是旧的，页面就会对着一张
//     "表都在"的报告去查一个真实的 `relation does not exist`。
//  2. **引擎不可达不是 500**。知识库/必需表/依赖三项的答案可以暂时"未知"，
//     但那是一次**业务结果**（status=unknown + 可操作的 fix），把整个端点打成 5xx
//     只会让页面显示"诊断不可用"，连 schema 那一行能给的结论也一起丢了。
//  3. **判定是纯函数**。所有 IO（DB 查询 / 引擎 HTTP）与"这算 ok 还是 warn"分开，
//     后者留在 `schemaItem` / `tablesItem` / `knowledgeItem` / `checkpointsItem` /
//     `depsItem` 里，便于单测覆盖每个分支（见 diagnostics_summary_test.go）。

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 诊断项状态。四值而不是三值：**unknown 与 warn 不是一回事** ——
// warn 是"看到了问题"，unknown 是"根本没看到"。排障时前者去修，后者先恢复观测。
const (
	diagStateOK      = "ok"
	diagStateWarn    = "warn"
	diagStateError   = "error"
	diagStateUnknown = "unknown"
)

// diagSummaryTTL 是单项探测的上限。任何一项卡住都不该让整个卡片转圈：
// 诊断页的价值在于"立刻给结论"，慢结论等于没有结论。
const (
	diagSchemaTimeout     = 3 * time.Second
	diagCheckpointTimeout = 5 * time.Second
	diagTablesTimeout     = 4 * time.Second
	// 引擎侧 `_probe` 自己有 10s 上限（`internal_knowledge.py`），这里必须比它长，
	// 否则我们会先超时、把"那台检索服务连不上"误报成"引擎不可达"——
	// 两种结论的排查方向完全不同。
	diagKnowledgeTimeout = 12 * time.Second
	diagDepsTimeout      = 3 * time.Second
)

// DiagSummaryCheck 是清单里的一行。字段是前后端的契约，五个都不能省：
// key 决定图标/跳转，status 决定徽标颜色，fix 是"我该做什么"。
type DiagSummaryCheck struct {
	Key    string `json:"key"`
	Title  string `json:"title"`
	Status string `json:"status"`
	Detail string `json:"detail"`
	Fix    string `json:"fix"`
}

// DiagSummaryResponse 是 GET /v1/admin/diagnostics/summary 的响应体。
type DiagSummaryResponse struct {
	GeneratedAt int64              `json:"generated_at"`
	Status      string             `json:"status"`
	Counts      map[string]int     `json:"counts"`
	Checks      []DiagSummaryCheck `json:"checks"`
}

// GetDiagnosticsSummary GET /v1/admin/diagnostics/summary —— 一次返回分组检查项。
func (h *AdminHandler) GetDiagnosticsSummary(w http.ResponseWriter, r *http.Request) {
	checks := []DiagSummaryCheck{
		h.checkSchema(r.Context()),
		h.checkTables(r.Context()),
		h.checkKnowledge(r.Context()),
		h.checkCheckpoints(r.Context(), auth.GetClaims(r.Context())),
		h.checkDeps(r.Context()),
	}
	OK(w, buildDiagSummary(checks, time.Now().Unix()))
}

// ── 各项 IO + 判定 ─────────────────────────────────────────────────────

// checkSchema 比对 alembic head 与库里的实际版本。
func (h *AdminHandler) checkSchema(ctx context.Context) DiagSummaryCheck {
	if db.Pool == nil {
		return schemaItem("", "", false, errDiagnosticsNoDB)
	}
	ctx, cancel := context.WithTimeout(ctx, diagSchemaTimeout)
	defer cancel()
	expected, actual, match, err := db.CheckSchemaVersion(ctx)
	return schemaItem(expected, actual, match, err)
}

// schemaItem 把 CheckSchemaVersion 的四个返回值翻译成一行的结论。
//
// 错误分两类：
//   - expected 空 ⇒ **解析**失败（迁移目录不可读/分叉/无 head），是部署物的问题；
//   - expected 非空 ⇒ **查询**失败（连不上库 / 权限），是运行环境的问题。
//
// 两类 fix 完全不同，混成一句"schema 检查失败"等于让人两边乱试。
func schemaItem(expected, actual string, match bool, err error) DiagSummaryCheck {
	item := DiagSummaryCheck{
		Key:    "schema",
		Title:  "Schema 版本",
		Status: diagStateUnknown,
	}
	switch {
	case errors.Is(err, errDiagnosticsNoDB):
		item.Detail = "数据库连接池未初始化，无法比对 schema 版本"
		item.Fix = "检查 POSTGRES_DSN 与数据库可达性；库没起来时所有依赖库的检查都会是未知。"
		return item
	case err != nil && expected == "":
		item.Detail = "无法解析迁移 head：" + truncErr(err, 200)
		item.Fix = "确认 MIGRATIONS_DIR 指向 migrations/versions，且迁移链只有一个 head（不要分叉）。"
		return item
	case err != nil:
		item.Detail = fmt.Sprintf("读取 alembic_version 失败（期望 head %s）：%s", expected, truncErr(err, 200))
		item.Fix = "确认库可达且当前账号能读 alembic_version；必要时执行 `alembic upgrade head`。"
		return item
	case actual == "":
		item.Status = diagStateError
		item.Detail = fmt.Sprintf("数据库从未迁移（期望 head %s，alembic_version 不存在或为空）", expected)
		item.Fix = "执行 `python -m alembic -c alembic.ini upgrade head` —— 应用自身不执行迁移。"
		return item
	case !match:
		item.Status = diagStateError
		item.Detail = fmt.Sprintf("版本不一致：期望 head %s，实际 %s", expected, actual)
		item.Fix = "执行 `python -m alembic -c alembic.ini upgrade head` 把 schema 升到 head。"
		return item
	default:
		item.Status = diagStateOK
		item.Detail = fmt.Sprintf("schema 版本 %s，与代码期望的 head 一致", actual)
		return item
	}
}

// engineTablesResult 是引擎 `/internal/diagnostics/tables` 的响应。
type engineTablesResult struct {
	Checked       bool     `json:"checked"`
	RequiredCount int      `json:"required_count"`
	PresentCount  int      `json:"present_count"`
	Missing       []string `json:"missing"`
	MissingCount  int      `json:"missing_count"`
	Detail        string   `json:"detail"`
}

// checkTables 让**引擎**回答"必需表齐了没"（清单只在引擎侧有一份）。
func (h *AdminHandler) checkTables(ctx context.Context) DiagSummaryCheck {
	if h.pythonClient == nil {
		return tablesItem(engineTablesResult{}, errEngineNotConfigured)
	}
	ctx, cancel := context.WithTimeout(ctx, diagTablesTimeout)
	defer cancel()
	var out engineTablesResult
	if err := h.pythonClient.GetJSON(ctx, "/internal/diagnostics/tables", &out); err != nil {
		return tablesItem(engineTablesResult{}, err)
	}
	return tablesItem(out, nil)
}

// tablesItem 判断必需表检查的结论。
func tablesItem(res engineTablesResult, err error) DiagSummaryCheck {
	item := DiagSummaryCheck{
		Key:    "tables",
		Title:  "必需数据表",
		Status: diagStateUnknown,
	}
	switch {
	case err != nil:
		// 引擎不可达 ⇒ 看不见，不等于表不在。这一步刻意不自己查 information_schema：
		// Go 侧一旦有清单，就会与引擎的 REQUIRED_TABLES 漂移。
		item.Detail = "引擎未启动或无法读取必需表清单：" + truncErr(err, 200)
		item.Fix = "启动 Python 引擎后重试；引擎启动日志会直接列出缺失表名。"
		return item
	case !res.Checked:
		item.Detail = "引擎无法校验必需表：" + strings.TrimSpace(res.Detail)
		item.Fix = "确认引擎能连上同一个数据库（POSTGRES_DSN），然后重试。"
		return item
	case res.MissingCount > 0 || len(res.Missing) > 0:
		item.Status = diagStateError
		item.Detail = fmt.Sprintf("缺少 %d 张必需表：%s",
			maxOf(len(res.Missing), res.MissingCount), strings.Join(res.Missing, ", "))
		item.Fix = "执行 `python -m alembic -c alembic.ini upgrade head`；应用不建表。"
		return item
	default:
		item.Status = diagStateOK
		item.Detail = fmt.Sprintf("%d 张必需表齐全", res.RequiredCount)
		return item
	}
}

// engineKnowledgeState 是引擎知识库状态里本端点关心的字段。
//
// 字段名与 `python-engine/app/api/internal_knowledge.py` 逐字对应：
// `runtime.state()` 给 mode/service_url/bootstrap_url，`_builtin_facts()` 补
// builtin_service_url/default_provider/service_ready，`probe` 是连通性探测结果。
type engineKnowledgeState struct {
	Mode              string `json:"mode"`
	ServiceURL        string `json:"service_url"`
	BootstrapURL      string `json:"bootstrap_url"`
	UsingBootstrap    bool   `json:"using_bootstrap"`
	BuiltinServiceURL string `json:"builtin_service_url"`
	DefaultProvider   string `json:"default_provider"`
	ServiceReady      bool   `json:"service_ready"`
	ServiceDetail     string `json:"service_detail"`
	Probe             *struct {
		OK         bool   `json:"ok"`
		StatusCode int    `json:"status_code"`
		Detail     string `json:"detail"`
	} `json:"probe"`
}

// checkKnowledge 读引擎的知识库运行时状态（地址 / 健康 / 默认 provider）。
func (h *AdminHandler) checkKnowledge(ctx context.Context) DiagSummaryCheck {
	if h.pythonClient == nil {
		return knowledgeItem(engineKnowledgeState{}, errEngineNotConfigured)
	}
	ctx, cancel := context.WithTimeout(ctx, diagKnowledgeTimeout)
	defer cancel()
	var out engineKnowledgeState
	if err := h.pythonClient.GetJSON(ctx, "/v1/internal/knowledge-runtime?probe=true", &out); err != nil {
		return knowledgeItem(engineKnowledgeState{}, err)
	}
	return knowledgeItem(out, nil)
}

// knowledgeItem 判断知识库的结论。
//
// 关键区分：
//   - **引擎不可达** ⇒ unknown（"服务未启动"，连状态都读不到）；
//   - **引擎在、但检索服务探不通** ⇒ warn（配置指向了一个没起的地址，是"配了但不生效"的典型）；
//   - 未配置地址 ⇒ warn（本地模式下没有内置服务时，建库会 400）。
func knowledgeItem(st engineKnowledgeState, err error) DiagSummaryCheck {
	item := DiagSummaryCheck{
		Key:    "knowledge",
		Title:  "知识库服务",
		Status: diagStateUnknown,
	}
	if err != nil {
		// 这里**不是** 500：知识库只是诊断清单里的一行，引擎没起时其余各行照旧有结论。
		if errors.Is(err, context.DeadlineExceeded) {
			// 超时说明**引擎在、但探测没在预算内回来**（检索服务很可能不可达）。
			// 与"引擎不可达"分开报，否则用户会去重启一个活着的引擎。
			item.Status = diagStateWarn
			item.Detail = fmt.Sprintf("知识库状态读取超时（%s 内未返回，检索服务可能不可达）", diagKnowledgeTimeout)
			item.Fix = "确认知识库检索服务（内置 pageindex）已启动且引擎侧可达，或在「知识库」页调整地址。"
			return item
		}
		item.Detail = "知识库服务未启动（引擎不可达，无法读取知识库状态）：" + truncErr(err, 200)
		item.Fix = "启动 Python 引擎后重试；引擎未启动时建库、检索、索引都不会工作。"
		return item
	}

	addr := firstNonEmpty(st.ServiceURL, st.BuiltinServiceURL)
	provider := firstNonEmpty(st.DefaultProvider, "（未配置）")
	mode := firstNonEmpty(st.Mode, "unknown")

	switch {
	case addr == "":
		item.Status = diagStateWarn
		item.Detail = fmt.Sprintf("模式 %s，但未配置检索服务地址（默认 provider 为空）", mode)
		item.Fix = "在「知识库」页配置服务地址，或部署内置检索服务（KNOWLEDGE_SERVICE_URL）。"
		return item
	case st.Probe == nil:
		item.Status = diagStateWarn
		item.Detail = fmt.Sprintf("模式 %s，服务地址 %s，但引擎未返回探测结果", mode, addr)
		item.Fix = "检查「知识库」页配置后重试。"
		return item
	case !st.Probe.OK:
		item.Status = diagStateWarn
		item.Detail = fmt.Sprintf("模式 %s，服务地址 %s，探测失败：%s", mode, addr, probeDetail(st.Probe.Detail, st.Probe.StatusCode))
		item.Fix = "确认检索服务已启动且地址可达（引擎侧发起），或在「知识库」页切换模式/地址。"
		return item
	default:
		item.Status = diagStateOK
		item.Detail = fmt.Sprintf("模式 %s，服务地址 %s 可达，默认 provider %s", mode, addr, provider)
		if st.ServiceReady && st.ServiceDetail == "" {
			item.Detail += "（内置自托管就绪）"
		}
		return item
	}
}

// checkpointStats 是检查点健康的三个观测值。
type checkpointStats struct {
	Failed24h int64
	// LastOKText 是"最近一次成功"的展示串（YYYY-MM-DD HH:MM）。
	// 空串表示**从来没有成功过** —— 与"最近成功是三天前"是不同的结论。
	//
	// 用 SQL 侧 `to_char` 而不是扫成 *time.Time：这一行只用来显示，
	// 而 NULL 时间戳扫进指针这条路径一旦有偏差，会让整个检查项变成"查询失败"。
	LastOKText string
	// SessionsWithoutCheckpoints7d 是近 7 天有活动、却一个检查点都没有的会话数。
	SessionsWithoutCheckpoints7d int64
	Tenant                       string
}

// checkCheckpoints 查 project_checkpoints 的近 24h 失败数、最近成功时间，
// 以及近 7 天有活动但没有任何检查点的会话数（按 tenant 过滤）。
func (h *AdminHandler) checkCheckpoints(ctx context.Context, claims *auth.Claims) DiagSummaryCheck {
	if db.Pool == nil {
		return checkpointsItem(checkpointStats{}, errDiagnosticsNoDB)
	}
	ctx, cancel := context.WithTimeout(ctx, diagCheckpointTimeout)
	defer cancel()

	// tenant_id = 0 表示平台 owner 视角：不按租户切分（清单要能回答"全平台有没有问题"）。
	tenantID := int16(0)
	tenantLabel := "全平台"
	if claims != nil && claims.TenantID != 0 {
		tenantID = claims.TenantID
		tenantLabel = "租户 " + tenantStr(tenantID)
	}

	// 一条语句取三个标量：三次往返会把"检查点面板"变成三倍延迟，而三者的
	// tenant 过滤条件完全一致，合并没有语义损失。
	//
	// `to_char(...)` 而不是直接取 timestamptz：这一列只用来显示，NULL 时间戳扫进
	// 指针的路径一旦有偏差，就会把整个检查项误报成"查询失败"。
	const q = `
SELECT
  (SELECT COUNT(*) FROM project_checkpoints c
    WHERE ($1::smallint = 0 OR c.tenant_id = $1::smallint)
      AND c.state <> 'ok'
      AND c.created_at > now() - interval '24 hours'),
  (SELECT COALESCE(to_char(MAX(c.created_at), 'YYYY-MM-DD HH24:MI'), '')
     FROM project_checkpoints c
    WHERE ($1::smallint = 0 OR c.tenant_id = $1::smallint)
      AND c.state = 'ok'),
  (SELECT COUNT(*) FROM sessions s
    WHERE s.updated_at IS NOT NULL
      AND s.updated_at > now() - interval '7 days'
      AND ($1::smallint = 0 OR s.tenant_id = $1::smallint)
      AND NOT EXISTS (
        SELECT 1 FROM project_checkpoints c
         WHERE c.session_id = s.id
           AND c.created_at > now() - interval '7 days'))`

	stats := checkpointStats{Tenant: tenantLabel}
	if err := db.Pool.QueryRow(ctx, q, tenantID).Scan(
		&stats.Failed24h, &stats.LastOKText, &stats.SessionsWithoutCheckpoints7d,
	); err != nil {
		return checkpointsItem(checkpointStats{Tenant: tenantLabel}, err)
	}
	return checkpointsItem(stats, nil)
}

// checkpointsItem 判断检查点健康的结论。
//
// 取舍：**有过失败但近期仍有成功** ⇒ warn（保护还在，但有个别回合没保住）；
// **从来没有成功过** ⇒ error（这条链根本没生效，是"配了但不生效"）。两者 fix 不同。
func checkpointsItem(st checkpointStats, err error) DiagSummaryCheck {
	item := DiagSummaryCheck{
		Key:    "checkpoints",
		Title:  "检查点保护",
		Status: diagStateUnknown,
	}
	scope := st.Tenant
	if scope == "" {
		scope = "全平台"
	}
	if err != nil {
		if errors.Is(err, errDiagnosticsNoDB) {
			item.Detail = "数据库连接池未初始化，无法读取检查点"
			item.Fix = "检查 POSTGRES_DSN 与数据库可达性。"
			return item
		}
		item.Detail = "查询 project_checkpoints 失败：" + truncErr(err, 200)
		item.Fix = "若提示表/列不存在，先执行 `alembic upgrade head`；否则检查库连接与权限。"
		return item
	}

	parts := make([]string, 0, 3)
	parts = append(parts, fmt.Sprintf("%s：近 24h 失败 %d 条", scope, st.Failed24h))
	if st.LastOKText == "" {
		parts = append(parts, "从未成功建过检查点")
	} else {
		parts = append(parts, "最近成功 "+st.LastOKText)
	}
	parts = append(parts, fmt.Sprintf("近 7 天有活动但无检查点的会话 %d 个", st.SessionsWithoutCheckpoints7d))
	item.Detail = strings.Join(parts, "；")

	neverOK := st.LastOKText == ""
	switch {
	case st.Failed24h > 0 && neverOK:
		item.Status = diagStateError
		item.Fix = "确认引擎的 checkpoint_hooks 已启用（工作区绑定 git 仓库），并检查引擎日志里的 error_code。"
	case neverOK && st.SessionsWithoutCheckpoints7d > 0:
		item.Status = diagStateWarn
		item.Fix = "近 7 天没有任何检查点：确认项目已绑定 git 仓库、checkpoint 功能未被关闭。"
	case st.Failed24h > 0:
		item.Status = diagStateWarn
		item.Fix = "查看失败检查点的 error_code（时间线里可见）：常见是工作区未绑定 git 或仓库过大。"
	case st.SessionsWithoutCheckpoints7d > 0:
		item.Status = diagStateWarn
		item.Fix = "部分会话没有检查点：确认这些会话对应的项目已绑定 git 仓库。"
	default:
		item.Status = diagStateOK
	}
	return item
}

// checkDeps 用极短的超时探一次引擎依赖接口，**只判断可达**，不取包列表。
//
// 为什么不去解析包列表：诊断页要的是"依赖通道通不通"，而 `pip list` 的输出有
// 几百行、还可能因为 vendor 目录很大而慢到秒级。把它塞进摘要页既无用又拖慢首屏。
func (h *AdminHandler) checkDeps(ctx context.Context) DiagSummaryCheck {
	if h.pythonClient == nil {
		return depsItem(errEngineNotConfigured)
	}
	ctx, cancel := context.WithTimeout(ctx, diagDepsTimeout)
	defer cancel()
	var out map[string]any
	return depsItem(h.pythonClient.GetJSON(ctx, "/internal/deps/list", &out))
}

// depsItem 判断依赖通道的结论。
func depsItem(err error) DiagSummaryCheck {
	item := DiagSummaryCheck{
		Key:    "deps",
		Title:  "Python 依赖通道",
		Status: diagStateUnknown,
	}
	switch {
	case err == nil:
		item.Status = diagStateOK
		item.Detail = "引擎 /internal/deps/list 可达"
		return item
	case errors.Is(err, errEngineNotConfigured):
		item.Detail = "引擎客户端未配置，无法探测依赖通道"
		item.Fix = "配置引擎地址并启动 Python 引擎后重试。"
		return item
	case errors.Is(err, context.DeadlineExceeded):
		// 超时不等于坏了：`pip list` 本身可能就要几秒。如实说"没等到"，别报成故障。
		item.Status = diagStateWarn
		item.Detail = fmt.Sprintf("依赖接口在 %s 内未响应（pip list 可能较慢，不代表不可用）", diagDepsTimeout)
		item.Fix = "若「依赖管理」页能正常列出包，可忽略此项；否则检查引擎进程与 INTERNAL_TOKEN。"
		return item
	default:
		item.Status = diagStateWarn
		item.Detail = "引擎依赖接口不可达：" + truncErr(err, 200)
		item.Fix = "确认引擎已启动、INTERNAL_TOKEN 与网关一致（未配置时引擎会 fail-closed 返回 503）。"
		return item
	}
}

// ── 聚合（纯函数）──────────────────────────────────────────────────────

// buildDiagSummary 汇总成响应：整体状态取最坏的一项，counts 供前端做概览。
//
// unknown 的排序低于 warn：warn 是"看到了问题"，unknown 是"没看到"。
// 但只要有任何 error，整体就必须是 error —— 否则卡片会把一个明确的故障
// 显示成"待确认"。
func buildDiagSummary(checks []DiagSummaryCheck, now int64) DiagSummaryResponse {
	counts := map[string]int{
		diagStateOK: 0, diagStateWarn: 0, diagStateError: 0, diagStateUnknown: 0,
	}
	worst := diagStateOK
	rank := map[string]int{
		diagStateOK: 0, diagStateUnknown: 1, diagStateWarn: 2, diagStateError: 3,
	}
	for i := range checks {
		status := checks[i].Status
		if status == "" {
			status = diagStateUnknown
			checks[i].Status = status
		}
		counts[status]++
		if rank[status] > rank[worst] {
			worst = status
		}
	}
	return DiagSummaryResponse{
		GeneratedAt: now,
		Status:      worst,
		Counts:      counts,
		Checks:      checks,
	}
}

// ── 小工具 ────────────────────────────────────────────────────────────

// errDiagnosticsNoDB 是一个哨兵：把"数据库没起来"与"查询报错"区分开。
// 两者的 fix 完全不同（前者去查 DSN/进程，后者去查表/权限）。
var errDiagnosticsNoDB = errors.New("database pool not initialized")

// errEngineNotConfigured 是"引擎客户端压根没接上"的哨兵（区别于请求失败）。
var errEngineNotConfigured = errors.New("python engine client not configured")

// probeDetail 把探测失败的原因说完整：没有 detail 时至少给出状态码，
// 空串会被读成"没有任何原因"。
func probeDetail(detail string, statusCode int) string {
	if s := strings.TrimSpace(detail); s != "" {
		return s
	}
	if statusCode > 0 {
		return fmt.Sprintf("HTTP %d", statusCode)
	}
	return "未知原因"
}

func maxOf(a, b int) int {
	if a > b {
		return a
	}
	return b
}
