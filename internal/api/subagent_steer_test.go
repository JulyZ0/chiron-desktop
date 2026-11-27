package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

// F1 父→子 steer 的网关闸门：归属、终态、空文本、引擎不可用。
// 成功转发需要真实归属映射（Redis），由集成/引擎侧用例覆盖；这里钉住"不该送出去"的路径。

func steerRequest(t *testing.T, runID, body string, claims *auth.Claims) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/subagent/runs/"+runID+"/steer",
		strings.NewReader(body))
	req.SetPathValue("run_id", runID)
	req.Header.Set("Content-Type", "application/json")
	return req.WithContext(auth.WithClaims(req.Context(), claims))
}

func TestSubagentSteerRejectsCrossUser(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	other := &auth.Claims{UserID: "user-2", TenantID: 1}

	rec := httptest.NewRecorder()
	h.SteerRun(rec, steerRequest(t, "rs_owned", `{"text":"先跑单测"}`, other))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户 steer 状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentSteerRejectsFinishedRun(t *testing.T) {
	h, store := newSubagentAuthzFixture()
	store.runs["rs_done"] = map[string]interface{}{
		"id": "rs_done", "tenant_id": 1, "user_id": "user-1",
		"status": "completed", "root_session_id": "sess-1",
	}
	owner := &auth.Claims{UserID: "user-1", TenantID: 1}

	rec := httptest.NewRecorder()
	h.SteerRun(rec, steerRequest(t, "rs_done", `{"text":"late"}`, owner))
	if rec.Code != http.StatusConflict {
		t.Fatalf("已结束 run 的 steer 状态=%d，want 409；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentSteerRejectsEmptyText(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	owner := &auth.Claims{UserID: "user-1", TenantID: 1}

	rec := httptest.NewRecorder()
	h.SteerRun(rec, steerRequest(t, "rs_owned", `{"text":"   "}`, owner))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("空指令状态=%d，want 400；body=%s", rec.Code, rec.Body.String())
	}
}

func TestSubagentSteerWithoutEngineIs503(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	owner := &auth.Claims{UserID: "user-1", TenantID: 1}

	rec := httptest.NewRecorder()
	h.SteerRun(rec, steerRequest(t, "rs_owned", `{"text":"continue"}`, owner))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("引擎不可用时状态=%d，want 503；body=%s", rec.Code, rec.Body.String())
	}
}
