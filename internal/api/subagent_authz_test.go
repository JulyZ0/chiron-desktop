package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// ── H27：子 Agent 观测/取消的按用户隔离回归 ──
//
// 修复前的行为：所有入口只校验 tenant（同租户即可读/停别人的子 Agent），
// Redis 运行期缓存更是只按 tenant 分键。这里用假 store 钉住三件事：
//  1. 跨用户读详情/事件 → 404；
//  2. 跨用户取消失败在"可见性判定"，不会触达广播；
//  3. 别人的会话 id 不能用来列整棵树；
// 以及 SQL 谓词必须真的带 user_id（而不是只靠 handler 层的 if）。

type fakeSubagentStore struct {
	runs     map[string]map[string]interface{}
	sessions map[string]map[string]interface{}
	lastSQL  string
	lastArgs []interface{}
}

func (f *fakeSubagentStore) record(sql string, args []interface{}) {
	f.lastSQL, f.lastArgs = sql, args
}

func (f *fakeSubagentStore) FetchOne(_ context.Context, sql string, args ...interface{}) (map[string]interface{}, error) {
	f.record(sql, args)
	if strings.Contains(sql, "FROM sessions") {
		row := f.sessions[stringArg(args, 0)]
		if row == nil {
			return nil, nil
		}
		if user := stringArg(args, 1); row["user_id"] != user {
			return nil, nil
		}
		if tenant := intArg(args, 2); tenant != 0 && intOf(row["tenant_id"]) != tenant {
			return nil, nil
		}
		return row, nil
	}
	row := f.runs[stringArg(args, 0)]
	if row == nil {
		return nil, nil
	}
	if tenant := stringArg(args, 1); tenant != "" && intOf(row["tenant_id"]) != atoiOrZero(tenant) {
		return nil, nil
	}
	// user 谓词：与生产 SQL 的 `user_id = $3 OR user_id IS NULL` 同口径。
	if user := stringArg(args, 2); user != "" {
		owner, _ := row["user_id"].(string)
		if owner != "" && owner != user {
			return nil, nil
		}
	}
	return row, nil
}

func (f *fakeSubagentStore) FetchAll(_ context.Context, sql string, args ...interface{}) ([]map[string]interface{}, error) {
	f.record(sql, args)
	return []map[string]interface{}{}, nil
}

func (f *fakeSubagentStore) Execute(_ context.Context, sql string, args ...interface{}) (int64, error) {
	f.record(sql, args)
	return 0, nil
}

func stringArg(args []interface{}, i int) string {
	if i >= len(args) {
		return ""
	}
	s, _ := args[i].(string)
	return s
}

func intArg(args []interface{}, i int) int {
	if i >= len(args) {
		return 0
	}
	switch v := args[i].(type) {
	case int:
		return v
	case int16:
		return int(v)
	}
	return 0
}

func atoiOrZero(s string) int {
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return 0
		}
		n = n*10 + int(r-'0')
	}
	return n
}

func newSubagentAuthzFixture() (*SubagentHandler, *fakeSubagentStore) {
	store := &fakeSubagentStore{
		runs: map[string]map[string]interface{}{
			"rs_owned": {
				"id": "rs_owned", "tenant_id": 1, "user_id": "user-1",
				"status": "running", "root_session_id": "sess-1",
				"depth": 1, "input_tokens": 1, "output_tokens": 2, "steps": 3,
			},
		},
		sessions: map[string]map[string]interface{}{
			"sess-1": {"user_id": "user-1", "tenant_id": 1},
		},
	}
	return &SubagentHandler{store: store}, store
}

func subagentAuthzRequest(t *testing.T, method, target, runID string, claims *auth.Claims) *http.Request {
	t.Helper()
	req := httptest.NewRequest(method, target, nil)
	if runID != "" {
		req.SetPathValue("run_id", runID)
	}
	return req.WithContext(auth.WithClaims(req.Context(), claims))
}

func TestSubagentGetRunRejectsCrossUser(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	other := &auth.Claims{UserID: "user-2", TenantID: 1}

	rec := httptest.NewRecorder()
	h.GetRun(rec, subagentAuthzRequest(t, http.MethodGet, "/v1/subagent/runs/rs_owned", "rs_owned", other))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户读子 Agent 详情状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}

	owner := &auth.Claims{UserID: "user-1", TenantID: 1}
	rec = httptest.NewRecorder()
	h.GetRun(rec, subagentAuthzRequest(t, http.MethodGet, "/v1/subagent/runs/rs_owned", "rs_owned", owner))
	if rec.Code != http.StatusOK {
		t.Fatalf("owner 读自己的 run 状态=%d，want 200；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentGetRunEventsRejectsCrossUser(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	req := subagentAuthzRequest(t, http.MethodGet, "/v1/subagent/runs/rs_owned/events", "rs_owned",
		&auth.Claims{UserID: "user-2", TenantID: 1})
	rec := httptest.NewRecorder()
	h.GetRunEvents(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户读事件流状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentCancelRejectsCrossUserBeforeBroadcast(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	req := subagentAuthzRequest(t, http.MethodPost, "/v1/subagent/runs/rs_owned/cancel", "rs_owned",
		&auth.Claims{UserID: "user-2", TenantID: 1})
	rec := httptest.NewRecorder()
	h.CancelRun(rec, req)
	// 404 必须发生在发布取消广播之前：若判定漏了，这里会因 rdb=nil 变成 503。
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户取消状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentListRunsRejectsForeignSession(t *testing.T) {
	h, _ := newSubagentAuthzFixture()

	rec := httptest.NewRecorder()
	h.ListRuns(rec, subagentAuthzRequest(t, http.MethodGet,
		"/v1/subagent/runs?session_id=sess-1", "",
		&auth.Claims{UserID: "user-2", TenantID: 1}))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("别人的会话列树状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}

	rec = httptest.NewRecorder()
	h.ListRuns(rec, subagentAuthzRequest(t, http.MethodGet,
		"/v1/subagent/runs?session_id=sess-1", "",
		&auth.Claims{UserID: "user-1", TenantID: 1}))
	if rec.Code != http.StatusOK {
		t.Fatalf("owner 列自己的会话状态=%d，want 200；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentListRunsRejectsForeignParentRun(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	rec := httptest.NewRecorder()
	h.ListRuns(rec, subagentAuthzRequest(t, http.MethodGet,
		"/v1/subagent/runs?parent_run_id=rs_owned", "",
		&auth.Claims{UserID: "user-2", TenantID: 1}))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("别人的父 run 列子节点状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentRunAccessQueryCarriesOwnerPredicate(t *testing.T) {
	h, store := newSubagentAuthzFixture()
	if _, _, ok := h.runAccessForCaller(context.Background(), "1", "user-1", "rs_owned"); !ok {
		t.Fatal("owner 应可见")
	}
	sql := store.lastSQL
	for _, want := range []string{"tenant_id = $2", "user_id = $3", "root_session_id"} {
		if !strings.Contains(sql, want) {
			t.Errorf("runAccessForCaller SQL 缺少 %q（跨用户可见性会漏判）：\n%s", want, sql)
		}
	}
	args := store.lastArgs
	if len(args) != 3 || args[0] != "rs_owned" || args[1] != "1" || args[2] != "user-1" {
		t.Errorf("runAccessForCaller 参数顺序/取值不对：%#v", args)
	}
}

func TestSubagentCancelSessionRequiresCallerOwnership(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	req := httptest.NewRequest(http.MethodPost, "/v1/subagent/sessions/sess-1/cancel", nil)
	req.SetPathValue("session_id", "sess-1")
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "user-2", TenantID: 1}))
	rec := httptest.NewRecorder()
	h.CancelSessionRuns(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户会话取消状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}
}
