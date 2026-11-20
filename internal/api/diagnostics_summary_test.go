package api

// diagnostics_summary_test.go —— 「配了但不生效」聚合清单（P0-1）的判定与接线。
//
// 这些用例刻意**不碰真实 DB / 引擎**：本端点最容易写坏的地方不是 SQL，而是
// "把看不见当成没问题"这一族判断。所以钉住的是：
//
//  1. DB 不可用时五行全部有结论（unknown + fix），而不是 500 或空清单；
//  2. schema 的三种失败（解析失败 / 查询失败 / 从未迁移）fix 各不相同；
//  3. 必需表只认引擎的清单，引擎不可达时**不能**说"表都在"；
//  4. 知识库服务未启动是 unknown（不是 500、也不是 ok）；
//  5. 依赖探测超时不等于故障（`pip list` 本身就慢）；
//  6. 整体状态取最坏项，路由必须挂在平台 owner 读门禁上。

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// withoutDB 把全局连接池摘掉，确保被测路径走"看不见库"的分支。
func withoutDB(t *testing.T) {
	t.Helper()
	prev := db.Pool
	db.Pool = nil
	t.Cleanup(func() { db.Pool = prev })
}

func summaryKeys(checks []DiagSummaryCheck) []string {
	out := make([]string, 0, len(checks))
	for _, c := range checks {
		out = append(out, c.Key)
	}
	return out
}

func TestDiagnosticsSummaryDBUnavailableStillAnswersEveryRow(t *testing.T) {
	withoutDB(t)

	// 引擎客户端也没配（AdminHandler 零值）：这正是"刚装好、什么都没起来"的现场。
	h := &AdminHandler{}
	rec := httptest.NewRecorder()
	h.GetDiagnosticsSummary(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/diagnostics/summary", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("DB/引擎不可用时仍须回 200（各项给 unknown），得到 %d", rec.Code)
	}
	var body struct {
		Success bool `json:"success"`
		Data    DiagSummaryResponse
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	if !body.Success {
		t.Error("success 应为 true：unknown 是业务结果，不是请求失败")
	}
	if len(body.Data.Checks) != 5 {
		t.Fatalf("清单应有 5 行，得到 %d", len(body.Data.Checks))
	}
	wantKeys := []string{"schema", "tables", "knowledge", "checkpoints", "deps"}
	gotKeys := summaryKeys(body.Data.Checks)
	for i, k := range wantKeys {
		if gotKeys[i] != k {
			t.Errorf("第 %d 行应为 %q，得到 %q", i, k, gotKeys[i])
		}
	}
	for _, c := range body.Data.Checks {
		if c.Title == "" || c.Detail == "" {
			t.Errorf("%s: title/detail 不能为空（页面只能靠它说明现状）", c.Key)
		}
		switch c.Status {
		case diagStateUnknown:
			// 看不见 ⇒ 必须给 fix，否则用户只能盯着"未知"发愣。
			if c.Fix == "" {
				t.Errorf("%s: unknown 必须带 fix", c.Key)
			}
		case diagStateOK:
			// 什么都没起却报 ok 是最坏的一种谎。
			t.Errorf("%s: 依赖全不可用时不该是 ok（%s）", c.Key, c.Detail)
		}
	}
	if body.Data.Status != diagStateUnknown {
		t.Errorf("整体状态应为 unknown，得到 %q", body.Data.Status)
	}
	if body.Data.Counts[diagStateUnknown] != 5 {
		t.Errorf("unknown 计数应为 5，得到 %d", body.Data.Counts[diagStateUnknown])
	}
	if body.Data.GeneratedAt <= 0 {
		t.Error("generated_at 应是 unix 秒")
	}
}

func TestSchemaItemBranches(t *testing.T) {
	cases := []struct {
		name     string
		expected string
		actual   string
		match    bool
		err      error
		want     string
		fixHint  string
	}{
		{"一致", "0002_x", "0002_x", true, nil, diagStateOK, ""},
		{"版本落后", "0002_x", "0001_y", false, nil, diagStateError, "alembic"},
		{"从未迁移", "0002_x", "", false, nil, diagStateError, "alembic"},
		{"解析失败（expected 空）", "", "", false, errors.New("no migration files"), diagStateUnknown, "MIGRATIONS_DIR"},
		{"查询失败（expected 非空）", "0002_x", "", false, errors.New("conn refused"), diagStateUnknown, "alembic_version"},
		{"连接池未初始化", "", "", false, errDiagnosticsNoDB, diagStateUnknown, "POSTGRES_DSN"},
	}
	for _, c := range cases {
		got := schemaItem(c.expected, c.actual, c.match, c.err)
		if got.Status != c.want {
			t.Errorf("%s: status = %q, want %q（detail=%s）", c.name, got.Status, c.want, got.Detail)
		}
		if c.fixHint != "" && !contains(got.Fix, c.fixHint) {
			t.Errorf("%s: fix 应提到 %q，得到 %q", c.name, c.fixHint, got.Fix)
		}
		if got.Status == diagStateOK && got.Fix != "" {
			t.Errorf("%s: ok 时不该给 fix（噪音）", c.name)
		}
		if got.Title == "" || got.Detail == "" || got.Key != "schema" {
			t.Errorf("%s: key/title/detail 不完整", c.name)
		}
	}
}

func TestTablesItemNeverClaimsOKWithoutEngineEvidence(t *testing.T) {
	// 引擎不可达：看不见表，**不能**说"表都在"。
	got := tablesItem(engineTablesResult{}, errors.New("dial tcp: connection refused"))
	if got.Status != diagStateUnknown {
		t.Fatalf("引擎不可达时 status 应为 unknown，得到 %q", got.Status)
	}
	if !contains(got.Detail, "引擎未启动") {
		t.Errorf("detail 应明确指出引擎未启动，得到 %q", got.Detail)
	}

	// 引擎在、但自己读不到 information_schema：同样是"无法判定"。
	if got := tablesItem(engineTablesResult{Checked: false, Detail: "permission denied"}, nil); got.Status != diagStateUnknown {
		t.Errorf("checked=false 时 status 应为 unknown，得到 %q", got.Status)
	}

	// 有缺失表 ⇒ error，且必须把表名说出来（只说"缺表"等于没说）。
	missing := tablesItem(engineTablesResult{
		Checked: true, RequiredCount: 25, Missing: []string{"project_checkpoints", "checkpoint_untracked"}, MissingCount: 2,
	}, nil)
	if missing.Status != diagStateError {
		t.Fatalf("缺表时 status 应为 error，得到 %q", missing.Status)
	}
	for _, name := range []string{"project_checkpoints", "checkpoint_untracked"} {
		if !contains(missing.Detail, name) {
			t.Errorf("detail 应列出缺失表 %q，得到 %q", name, missing.Detail)
		}
	}
	if !contains(missing.Fix, "alembic") {
		t.Errorf("缺表的 fix 应指向 alembic，得到 %q", missing.Fix)
	}

	// 齐全 ⇒ ok。
	ok := tablesItem(engineTablesResult{Checked: true, RequiredCount: 25, PresentCount: 25}, nil)
	if ok.Status != diagStateOK || !contains(ok.Detail, "25") {
		t.Errorf("齐全时应为 ok 且带上表数，得到 %q / %q", ok.Status, ok.Detail)
	}
}

func TestKnowledgeItemBranches(t *testing.T) {
	// 引擎不可达：**不是 500**，而是 unknown + "服务未启动"。
	down := knowledgeItem(engineKnowledgeState{}, errors.New("dial tcp: refused"))
	if down.Status != diagStateUnknown {
		t.Fatalf("引擎不可达时 status 应为 unknown，得到 %q", down.Status)
	}
	if !contains(down.Detail, "未启动") {
		t.Errorf("detail 应说明服务未启动，得到 %q", down.Detail)
	}

	// 超时是"引擎在、但探测没回来"：报 warn 且不说"引擎不可达"——
	// 否则用户会去重启一个活着的引擎。
	timeout := knowledgeItem(engineKnowledgeState{}, context.DeadlineExceeded)
	if timeout.Status != diagStateWarn || !contains(timeout.Detail, "超时") {
		t.Errorf("超时应为 warn 且说明超时，得到 %q / %q", timeout.Status, timeout.Detail)
	}
	if contains(timeout.Detail, "不可达，无法读取") {
		t.Errorf("超时不该被报成引擎不可达：%q", timeout.Detail)
	}

	// 未配置地址（本地模式下没有内置服务）：warn，因为建库会 400。
	noAddr := knowledgeItem(engineKnowledgeState{Mode: "local"}, nil)
	if noAddr.Status != diagStateWarn || !contains(noAddr.Detail, "未配置") {
		t.Errorf("未配置地址应为 warn 且说明未配置，得到 %q / %q", noAddr.Status, noAddr.Detail)
	}

	// 引擎在、探测失败：warn，并保留原因（"配置本身对、只是那台机器没起"与"地址写错"不同）。
	probeFail := engineKnowledgeState{Mode: "remote", ServiceURL: "http://pi:8100"}
	probeFail.Probe = &struct {
		OK         bool   `json:"ok"`
		StatusCode int    `json:"status_code"`
		Detail     string `json:"detail"`
	}{OK: false, Detail: "服务不可达: dial tcp"}
	if got := knowledgeItem(probeFail, nil); got.Status != diagStateWarn || !contains(got.Detail, "dial tcp") {
		t.Errorf("探测失败应为 warn 且带原因，得到 %q / %q", got.Status, got.Detail)
	}

	// 探测成功：ok，detail 里要有地址、模式与默认 provider（缺一个都不算回答清楚）。
	good := engineKnowledgeState{
		Mode: "local", ServiceURL: "http://127.0.0.1:8100", DefaultProvider: "selfhosted", ServiceReady: true,
	}
	good.Probe = &struct {
		OK         bool   `json:"ok"`
		StatusCode int    `json:"status_code"`
		Detail     string `json:"detail"`
	}{OK: true}
	got := knowledgeItem(good, nil)
	if got.Status != diagStateOK {
		t.Fatalf("探测成功应为 ok，得到 %q / %q", got.Status, got.Detail)
	}
	for _, frag := range []string{"local", "127.0.0.1:8100", "selfhosted"} {
		if !contains(got.Detail, frag) {
			t.Errorf("detail 应包含 %q，得到 %q", frag, got.Detail)
		}
	}
	if got.Fix != "" {
		t.Error("ok 时不该给 fix")
	}
}

func TestCheckpointsItemSeparatesNeverOKFromOccasionalFailure(t *testing.T) {
	const okAt = "2026-11-20 10:00"

	healthy := checkpointsItem(checkpointStats{Failed24h: 0, LastOKText: okAt, Tenant: "租户 1"}, nil)
	if healthy.Status != diagStateOK {
		t.Errorf("有近期成功且无失败应为 ok，得到 %q", healthy.Status)
	}
	if !contains(healthy.Detail, "租户 1") {
		t.Errorf("detail 应写明统计口径（%q）", healthy.Detail)
	}

	// 个别失败但仍有成功 ⇒ warn（保护还在，只是有回合没保住）。
	someFail := checkpointsItem(checkpointStats{Failed24h: 3, LastOKText: okAt}, nil)
	if someFail.Status != diagStateWarn || !contains(someFail.Detail, "失败 3") {
		t.Errorf("有失败应为 warn 且带失败数，得到 %q / %q", someFail.Status, someFail.Detail)
	}

	// 从未成功 + 近 24h 有失败 ⇒ error（这条链根本没生效）。
	never := checkpointsItem(checkpointStats{Failed24h: 2, LastOKText: ""}, nil)
	if never.Status != diagStateError || !contains(never.Detail, "从未成功") {
		t.Errorf("从未成功应为 error，得到 %q / %q", never.Status, never.Detail)
	}

	// 没失败但也从未成功、且有会话没有检查点 ⇒ warn（可疑，不足以断言坏）。
	suspect := checkpointsItem(checkpointStats{LastOKText: "", SessionsWithoutCheckpoints7d: 4}, nil)
	if suspect.Status != diagStateWarn || !contains(suspect.Detail, "4 个") {
		t.Errorf("应为 warn 且带会话数，得到 %q / %q", suspect.Status, suspect.Detail)
	}

	// DB 不可用 ⇒ unknown（不是 error：没看到问题 vs 看不到）。
	noDB := checkpointsItem(checkpointStats{}, errDiagnosticsNoDB)
	if noDB.Status != diagStateUnknown || noDB.Fix == "" {
		t.Errorf("DB 不可用应为 unknown 且带 fix，得到 %q / %q", noDB.Status, noDB.Fix)
	}

	// 查询失败（例如缺表）⇒ unknown，并给出 alembic 线索。
	queryErr := checkpointsItem(checkpointStats{}, errors.New(`ERROR: relation "project_checkpoints" does not exist (SQLSTATE 42P01)`))
	if queryErr.Status != diagStateUnknown || !contains(queryErr.Fix, "alembic") {
		t.Errorf("查询失败应提示 alembic，得到 %q / %q", queryErr.Status, queryErr.Fix)
	}
}

func TestDepsItemTreatsTimeoutAsWarnNotFailure(t *testing.T) {
	if got := depsItem(nil); got.Status != diagStateOK || got.Fix != "" {
		t.Errorf("可达应为 ok 且无 fix，得到 %q / %q", got.Status, got.Fix)
	}
	// `pip list` 本身就慢：超时只说明"没等到"，不能报成故障。
	timeout := depsItem(context.DeadlineExceeded)
	if timeout.Status != diagStateWarn || !contains(timeout.Detail, "未响应") {
		t.Errorf("超时应为 warn 且说明未响应，得到 %q / %q", timeout.Status, timeout.Detail)
	}
	// 客户端未配置与"请求失败"要分开：前者是部署问题，fix 不同。
	if got := depsItem(errEngineNotConfigured); got.Status != diagStateUnknown || got.Fix == "" {
		t.Errorf("未配置引擎应为 unknown 且带 fix，得到 %q / %q", got.Status, got.Fix)
	}
	if got := depsItem(errors.New("python GET /internal/deps/list returned 503")); got.Status != diagStateWarn {
		t.Errorf("503（内部令牌未配）应为 warn，得到 %q", got.Status)
	}
}

func TestBuildDiagSummaryWorstStatusWins(t *testing.T) {
	checks := []DiagSummaryCheck{
		{Key: "a", Status: diagStateOK},
		{Key: "b", Status: diagStateUnknown},
		{Key: "c", Status: diagStateWarn},
	}
	if got := buildDiagSummary(checks, 1); got.Status != diagStateWarn {
		t.Errorf("有 warn 时整体应为 warn，得到 %q", got.Status)
	}
	checks = append(checks, DiagSummaryCheck{Key: "d", Status: diagStateError})
	got := buildDiagSummary(checks, 1)
	if got.Status != diagStateError {
		t.Errorf("有 error 时整体应为 error，得到 %q", got.Status)
	}
	if got.Counts[diagStateError] != 1 || got.Counts[diagStateOK] != 1 || got.Counts[diagStateWarn] != 1 || got.Counts[diagStateUnknown] != 1 {
		t.Errorf("counts 没按状态归类: %v", got.Counts)
	}
	// 空清单不该 panic，也不该报 ok 以外的东西。
	if got := buildDiagSummary(nil, 5); got.Status != diagStateOK || len(got.Checks) != 0 {
		t.Errorf("空清单应回 ok 空列表，得到 %q", got.Status)
	}
}

func TestDiagnosticsSummaryRouteIsExposedAndGated(t *testing.T) {
	// handler 写好但没接线时前端恒 404 —— 与"功能没做"在界面上长得一样。
	mux := http.NewServeMux()
	auth := &middlewareProbe{}
	rl := &middlewareProbe{}
	registerAdminRoutes(mux, auth.reject(http.StatusUnauthorized), rl.passthrough, &AdminHandler{}, nil)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/diagnostics/summary", nil))
	if !auth.hit("/v1/admin/diagnostics/summary") {
		t.Fatal("GET /v1/admin/diagnostics/summary 没有匹配到任何注册模式 ⇒ 前端会拿到 404")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证时应被拦在 401，得到 %d", rec.Code)
	}
}

// contains 是 strings.Contains 的小名，让表格用例读起来更短。
func contains(haystack, needle string) bool {
	return strings.Contains(haystack, needle)
}

// ── 引擎侧契约（用假引擎走真实 HTTP）──────────────────────────────────────
//
// 这一组钉住的是**字段名与路径**：`engineTablesResult` / `engineKnowledgeState` 的
// json tag 写错一个字符，端点照样 200，但每一行都会显示成"0 张表齐全"/"未配置地址"——
// 那是最难自查的一种谎（数据在，解码不到）。所以用假引擎把请求路径与响应字段
// 端到端跑一遍，并顺带确认内部令牌真的被注入（引擎侧 fail-closed，缺它会 503）。

// fakeEngine 起一个只实现三个路径的假引擎，并记录收到的路径与内部令牌。
func fakeDiagEngine(t *testing.T, handlers map[string]string) (*httptest.Server, *[]string, *[]string) {
	t.Helper()
	paths := &[]string{}
	tokens := &[]string{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*paths = append(*paths, r.URL.Path)
		*tokens = append(*tokens, r.Header.Get("X-Internal-Token"))
		body, ok := handlers[r.URL.Path]
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"detail":"not found"}`))
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv, paths, tokens
}

func summaryByKey(t *testing.T, checks []DiagSummaryCheck, key string) DiagSummaryCheck {
	t.Helper()
	for _, c := range checks {
		if c.Key == key {
			return c
		}
	}
	t.Fatalf("清单里没有 %q 这一项", key)
	return DiagSummaryCheck{}
}

func TestDiagnosticsSummaryDecodesEnginePayloads(t *testing.T) {
	withoutDB(t)
	srv, paths, tokens := fakeDiagEngine(t, map[string]string{
		"/internal/diagnostics/tables": `{"checked":true,"required_count":25,"present_count":25,"missing":[],"missing_count":0,"detail":""}`,
		"/v1/internal/knowledge-runtime": `{"mode":"local","service_url":"http://127.0.0.1:8100",
			"builtin_service_url":"http://127.0.0.1:8100","default_provider":"selfhosted",
			"service_ready":true,"service_detail":"","probe":{"ok":true,"status_code":200,"detail":""}}`,
		"/internal/deps/list": `{"python":"3.12.0","packages":[],"count":0}`,
	})

	client := engine.NewPythonClient(srv.URL)
	client.SetInternalToken("internal-token-for-test")
	h := &AdminHandler{pythonClient: client}

	rec := httptest.NewRecorder()
	h.GetDiagnosticsSummary(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/diagnostics/summary", nil))
	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d", rec.Code)
	}
	var body struct {
		Data DiagSummaryResponse
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}

	if got := summaryByKey(t, body.Data.Checks, "tables"); got.Status != diagStateOK || !contains(got.Detail, "25") {
		t.Errorf("tables 应为 ok 并显示 25 张表，得到 %q / %q", got.Status, got.Detail)
	}
	know := summaryByKey(t, body.Data.Checks, "knowledge")
	if know.Status != diagStateOK {
		t.Fatalf("knowledge 应为 ok，得到 %q / %q", know.Status, know.Detail)
	}
	// 三项事实缺一都不能算"回答清楚"：内置服务地址、默认 provider、模式。
	for _, frag := range []string{"127.0.0.1:8100", "selfhosted", "local"} {
		if !contains(know.Detail, frag) {
			t.Errorf("knowledge detail 应包含 %q，得到 %q", frag, know.Detail)
		}
	}
	if got := summaryByKey(t, body.Data.Checks, "deps"); got.Status != diagStateOK {
		t.Errorf("deps 应为 ok，得到 %q / %q", got.Status, got.Detail)
	}
	// DB 不可用 ⇒ schema / checkpoints 仍是 unknown（引擎好不代表库好）。
	for _, key := range []string{"schema", "checkpoints"} {
		if got := summaryByKey(t, body.Data.Checks, key); got.Status != diagStateUnknown {
			t.Errorf("%s 应为 unknown（DB 未起），得到 %q", key, got.Status)
		}
	}

	// 路径必须与引擎注册的完全一致 —— 少一层前缀就是 404，而 404 会被读成"引擎坏了"。
	for _, want := range []string{"/internal/diagnostics/tables", "/v1/internal/knowledge-runtime", "/internal/deps/list"} {
		found := false
		for _, p := range *paths {
			if p == want {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("没有请求 %q，实际请求了 %v", want, *paths)
		}
	}
	// 内部令牌必须被注入：引擎侧未配置/不匹配会 fail-closed 返回 401/503。
	for i, tok := range *tokens {
		if tok != "internal-token-for-test" {
			t.Errorf("第 %d 个请求缺少 X-Internal-Token（得到 %q）", i, tok)
		}
	}
}

func TestDiagnosticsSummarySurfacesMissingTablesFromEngine(t *testing.T) {
	withoutDB(t)
	srv, _, _ := fakeDiagEngine(t, map[string]string{
		"/internal/diagnostics/tables": `{"checked":true,"required_count":25,"present_count":23,
			"missing":["project_checkpoints","checkpoint_untracked"],"missing_count":2,
			"detail":"缺少必需表，请执行 alembic upgrade head"}`,
		"/v1/internal/knowledge-runtime": `{"mode":"remote","service_url":"http://pi.internal:9100",
			"default_provider":"selfhosted","probe":{"ok":false,"status_code":0,"detail":"服务不可达: dial tcp"}}`,
		"/internal/deps/list": `{"python":"3.12.0","packages":[],"count":0}`,
	})
	h := &AdminHandler{pythonClient: engine.NewPythonClient(srv.URL)}

	checks := []DiagSummaryCheck{h.checkTables(context.Background()), h.checkKnowledge(context.Background())}

	tables := summaryByKey(t, checks, "tables")
	if tables.Status != diagStateError {
		t.Fatalf("缺表应为 error，得到 %q / %q", tables.Status, tables.Detail)
	}
	for _, name := range []string{"project_checkpoints", "checkpoint_untracked"} {
		if !contains(tables.Detail, name) {
			t.Errorf("detail 应列出 %q，得到 %q", name, tables.Detail)
		}
	}
	know := summaryByKey(t, checks, "knowledge")
	if know.Status != diagStateWarn {
		t.Fatalf("检索服务探不通应为 warn，得到 %q / %q", know.Status, know.Detail)
	}
	if !contains(know.Detail, "dial tcp") {
		t.Errorf("detail 应保留引擎给的失败原因，得到 %q", know.Detail)
	}
}
