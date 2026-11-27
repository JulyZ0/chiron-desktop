package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

func dispatchRequest(t *testing.T, sessionID, body string, claims *auth.Claims) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/v1/sessions/"+sessionID+"/subagents",
		strings.NewReader(body))
	req.SetPathValue("session_id", sessionID)
	req.Header.Set("Content-Type", "application/json")
	return req.WithContext(auth.WithClaims(req.Context(), claims))
}

func TestDispatchSubagentRejectsForeignSession(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	other := &auth.Claims{UserID: "user-2", TenantID: 1}
	rec := httptest.NewRecorder()
	h.DispatchSubagent(rec, dispatchRequest(t, "sess-1", `{"task":"review"}`, other))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("跨用户委派状态=%d，want 404；body=%s", rec.Code, rec.Body.String())
	}
}

func TestDispatchSubagentRequiresTask(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	owner := &auth.Claims{UserID: "user-1", TenantID: 1}
	rec := httptest.NewRecorder()
	h.DispatchSubagent(rec, dispatchRequest(t, "sess-1", `{"task":"   "}`, owner))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("空任务状态=%d，want 400；body=%s", rec.Code, rec.Body.String())
	}
}

func TestDispatchSubagentForwardsIdentityAndRunID(t *testing.T) {
	h, _ := newSubagentAuthzFixture()
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != subagentDispatchPath {
			t.Errorf("引擎路径=%s，want %s", r.URL.Path, subagentDispatchPath)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		_, _ = w.Write([]byte(`{"ok":true,"run_id":"rs_new","status":"running"}`))
	}))
	defer srv.Close()
	h.engine = engine.NewPythonClient(srv.URL)

	owner := &auth.Claims{UserID: "user-1", TenantID: 1}
	rec := httptest.NewRecorder()
	h.DispatchSubagent(rec, dispatchRequest(t, "sess-1",
		`{"task":"review the diff","profile":"reviewer","allow_write":false}`, owner))
	if rec.Code != http.StatusOK {
		t.Fatalf("委派状态=%d，want 200；body=%s", rec.Code, rec.Body.String())
	}
	if got["session_id"] != "sess-1" || got["user_id"] != "user-1" || got["profile"] != "reviewer" {
		t.Fatalf("转发身份不对: %+v", got)
	}
	if !strings.Contains(rec.Body.String(), "rs_new") {
		t.Fatalf("响应缺少 run_id: %s", rec.Body.String())
	}
}
