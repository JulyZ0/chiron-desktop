package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func testMismatch() schemaMismatch {
	return schemaMismatch{
		Expected: "20260901_0001",
		Actual:   "20260801_0007",
		Reason:   schemaMismatchRevision,
	}
}

// /healthz 必须是 200 且带 expected/actual/fix —— Electron 壳的就绪探测与前端守卫
// 都依赖这个"能探到、读得到原因"的契约；回 503 会让用户重新掉回白屏。
func TestSchemaMismatchHandlerHealthz(t *testing.T) {
	rec := httptest.NewRecorder()
	newSchemaMismatchHandler(testMismatch()).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("healthz status = %d, want 200", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("healthz content-type = %q, want application/json", ct)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("healthz body is not JSON: %v (%s)", err, rec.Body.String())
	}
	if body["status"] != "schema_mismatch" {
		t.Errorf("status = %v, want schema_mismatch", body["status"])
	}
	if body["expected"] != "20260901_0001" {
		t.Errorf("expected = %v", body["expected"])
	}
	if body["actual"] != "20260801_0007" {
		t.Errorf("actual = %v", body["actual"])
	}
	if body["fix"] != "run alembic upgrade head" {
		t.Errorf("fix = %v, want `run alembic upgrade head`", body["fix"])
	}
	if _, has := body["error"]; has {
		t.Errorf("healthz must not carry an error field: %v", body["error"])
	}
}

// 其它路径一律 503 + 同一个 JSON（带 error）：网关"活着但不可用"要说清楚，
// 而不是让客户端在 404/超时之间猜。
func TestSchemaMismatchHandlerOtherPaths503(t *testing.T) {
	h := newSchemaMismatchHandler(testMismatch())
	for _, path := range []string{"/", "/v1/chat", "/api/v1/agents/dispatch", "/ready", "/health"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code != http.StatusServiceUnavailable {
			t.Errorf("%s status = %d, want 503", path, rec.Code)
			continue
		}
		var body map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
			t.Errorf("%s body is not JSON: %v", path, err)
			continue
		}
		if body["status"] != "schema_mismatch" {
			t.Errorf("%s status field = %v", path, body["status"])
		}
		if body["error"] == nil || body["error"] == "" {
			t.Errorf("%s must explain why it is unavailable", path)
		}
		if body["fix"] != "run alembic upgrade head" {
			t.Errorf("%s fix = %v", path, body["fix"])
		}
	}
}

// 缺失表场景：reason/missing_tables 必须透出，页面才能说清"版本对但表缺"。
func TestSchemaMismatchHandlerMissingTables(t *testing.T) {
	m := schemaMismatch{
		Expected: "20260901_0001",
		Actual:   "20260901_0001",
		Reason:   schemaMismatchMissingTables,
		Missing:  []string{"users", "sessions"},
	}
	rec := httptest.NewRecorder()
	newSchemaMismatchHandler(m).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/healthz", nil))
	var body struct {
		Reason  string   `json:"reason"`
		Missing []string `json:"missing_tables"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Reason != schemaMismatchMissingTables {
		t.Errorf("reason = %q", body.Reason)
	}
	if len(body.Missing) != 2 || body.Missing[0] != "users" {
		t.Errorf("missing_tables = %v", body.Missing)
	}
}

// /healthz 只读：写方法必须被拒，避免有人把它当"触发器"打。
func TestSchemaMismatchHandlerHealthzRejectsWrite(t *testing.T) {
	rec := httptest.NewRecorder()
	newSchemaMismatchHandler(testMismatch()).ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/healthz", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /healthz status = %d, want 405", rec.Code)
	}
}
