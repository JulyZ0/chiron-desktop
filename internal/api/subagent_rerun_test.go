package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// ── 子 Agent 重跑（P0-2）──────────────────────────────────────────────
//
// 这组用例盯的是**路由与判定**，不是引擎侧的派发（那条链路由 python-engine 的
// tests/test_subagent_rerun.py 与 tests/test_internal_subagent_rerun.py 覆盖）。
// 这里最要紧的两件事：
//
//  1. `POST /v1/subagent/runs/{id}/rerun` 必须真的挂在网关路由表上，且经过 auth+限流 ——
//     重跑会真的消耗 token，漏挂 authMW 等于给匿名用户开一条烧钱的通道；
//  2. 「能不能重跑」的判定可单测：非终态必须 409（否则会派出第二份并发作业），
//     别人的 run 必须 404（不泄露存在性）。

// TestSubagentRerunRouteIsRegistered：路由存在 + 挂齐中间件。
func TestSubagentRerunRouteIsRegistered(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerSubagentRoutes(mux, authProbe.reject(http.StatusUnauthorized),
		rlProbe.passthrough, NewSubagentHandler(nil, nil))

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/subagent/runs/rs_1/rerun", nil))

	if !authProbe.hit("/v1/subagent/runs/rs_1/rerun") {
		t.Fatal("POST /v1/subagent/runs/{id}/rerun 未经过 authMW：匿名可以重跑（真实烧 token）")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证的重跑应被拒，得到 %d", rec.Code)
	}
}

// TestSubagentRerunRouteIsRateLimited：重跑是一条"会真的花钱"的写路径，必须限流。
func TestSubagentRerunRouteIsRateLimited(t *testing.T) {
	mux := http.NewServeMux()
	authProbe := &middlewareProbe{}
	rlProbe := &middlewareProbe{}
	registerSubagentRoutes(mux, authProbe.passthrough,
		rlProbe.reject(http.StatusTooManyRequests), NewSubagentHandler(nil, nil))

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/subagent/runs/rs_1/rerun", nil))

	if !rlProbe.hit("/v1/subagent/runs/rs_1/rerun") {
		t.Fatal("POST /v1/subagent/runs/{id}/rerun 未经过 rlMW：重跑成了绕过限流的第二通道")
	}
	if rec.Code != http.StatusTooManyRequests {
		t.Errorf("被限流的重跑应返回 429，得到 %d", rec.Code)
	}
}

// TestSubagentRerunGuard：只有**终态**才能重跑。
func TestSubagentRerunGuard(t *testing.T) {
	for _, status := range []string{"completed", "failed", "cancelled", "lost", "partial", "COMPLETED"} {
		if reason := subagentRerunGuard(status); reason != "" {
			t.Errorf("终态 %q 应当可重跑，得到拒因 %q", status, reason)
		}
	}
	for _, status := range []string{"running", "queued", "reasoning", "tool", " "} {
		if reason := subagentRerunGuard(status); reason == "" {
			t.Errorf("非终态 %q 必须被拒（否则会派出第二份并发作业）", status)
		}
	}
	if reason := subagentRerunGuard(""); reason != "run_status_unknown" {
		t.Errorf("空状态应报 run_status_unknown，得到 %q", reason)
	}
}

// TestTenantInt：发给引擎的 tenant 必须是整数（引擎侧 tenant_id 是 smallint）。
func TestTenantInt(t *testing.T) {
	if got := tenantInt(nil); got != 0 {
		t.Errorf("nil claims → 0, got %d", got)
	}
	if got := tenantInt(&auth.Claims{TenantID: 7, UserID: "u1"}); got != 7 {
		t.Errorf("claims.TenantID 优先，得到 %d", got)
	}
	if got := tenantInt(&auth.Claims{UserID: "12"}); got != 12 {
		t.Errorf("无租户时回退解析 user id，得到 %d", got)
	}
	if got := tenantInt(&auth.Claims{UserID: "u-uuid"}); got != 0 {
		t.Errorf("回退解析失败应为 0（调用方据此按不可见处理），得到 %d", got)
	}
}

// TestRerunRunRejectsAnonymous：没有 claims 一律 401。
func TestRerunRunRejectsAnonymous(t *testing.T) {
	h := NewSubagentHandler(nil, nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/subagent/runs/rs_1/rerun", nil)
	req.SetPathValue("run_id", "rs_1")

	h.RerunRun(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Errorf("匿名重跑应为 401，得到 %d", rec.Code)
	}
}

// TestRerunRunHidesMissingRun：无 DB / 无这条 run 都按 404（不泄露存在性）。
func TestRerunRunHidesMissingRun(t *testing.T) {
	h := NewSubagentHandler(nil, nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/subagent/runs/rs_1/rerun", nil)
	req.SetPathValue("run_id", "rs_1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))

	h.RerunRun(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("查不到归属的 run 应 404，得到 %d", rec.Code)
	}
}

// TestRerunRunRequiresTenant：没有租户上下文 ⇒ 没有归属可言 ⇒ 404（而不是把
// tenant_id=0 发给引擎换一个 400）。
func TestRerunRunRequiresTenant(t *testing.T) {
	h := NewSubagentHandler(nil, nil)
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/subagent/runs/rs_1/rerun", nil)
	req.SetPathValue("run_id", "rs_1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u-uuid"}))

	h.RerunRun(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("无租户上下文应 404，得到 %d", rec.Code)
	}
}

// TestEngineDetailParsing：引擎的错误体形状是 `{"detail": {"code","error"}}`，
// 解析失败必须回退到兜底文案（不能让前端拿到空原因）。
func TestEngineDetailParsing(t *testing.T) {
	raw := []byte(`{"detail":{"code":"task_missing","error":"run rs_1 has no stored task"}}`)
	if got := engineDetailCode(raw, "run_not_terminal"); got != "task_missing" {
		t.Errorf("code = %q", got)
	}
	if got := engineDetailError(raw, "fallback"); !strings.Contains(got, "no stored task") {
		t.Errorf("error = %q", got)
	}
	// 非 JSON：一律兜底
	if got := engineDetailCode([]byte("boom"), "run_not_terminal"); got != "run_not_terminal" {
		t.Errorf("非 JSON 应回退兜底码，得到 %q", got)
	}
	if got := engineDetailError([]byte("boom"), "fallback"); got != "fallback" {
		t.Errorf("非 JSON 应回退兜底文案，得到 %q", got)
	}
}

// TestRunHitBudgetWall：面板的「预算超限」标记只能来自引擎的权威证据。
func TestRunHitBudgetWall(t *testing.T) {
	cases := []struct {
		status, errText string
		want            bool
	}{
		{"partial", "", true},
		{"failed", "budget_exceeded:tokens", true},
		{"failed", "budget_exceeded:wall", true},
		{"failed", "upstream 500", false},
		{"completed", "", false},
		// 前缀判定：错误正文里"提到"预算不算撞墙（只有开头那一段算）
		{"failed", "error: budget_exceeded:tokens", false},
	}
	for _, c := range cases {
		if got := runHitBudgetWall(c.status, c.errText); got != c.want {
			t.Errorf("runHitBudgetWall(%q, %q) = %v, want %v", c.status, c.errText, got, c.want)
		}
	}
	// 视图契约：标记出现在 normalizeRunRow 的输出里，前端才有得读
	view := normalizeRunRow(map[string]interface{}{
		"id": "rs_1", "status": "failed", "error": "budget_exceeded:tokens",
	})
	if view["budget_exceeded"] != true {
		t.Errorf("撞预算墙的 run 应带 budget_exceeded=true：%+v", view)
	}
	if _, present := normalizeRunRow(map[string]interface{}{"id": "rs_2", "status": "completed"})["budget_exceeded"]; present {
		t.Error("没撞墙的 run 不该带这个字段（多余字段会让前端误判）")
	}
}
