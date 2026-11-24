package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

func proxyMuxWithClaims(t *testing.T, upstream string, userID string) *http.ServeMux {
	t.Helper()
	withClaims := func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := auth.WithClaims(r.Context(), &auth.Claims{UserID: userID, TenantID: 17})
			h.ServeHTTP(w, r.WithContext(ctx))
		})
	}
	mux := http.NewServeMux()
	registerProxyRoutes(mux, withClaims, withClaims, withClaims, engine.NewPythonClient(upstream), NewUserSemaphorePool(nil, 0))
	return mux
}

func TestProxyMapsOnlyExplicitTypedErrors(t *testing.T) {
	tests := []struct {
		name       string
		payload    map[string]interface{}
		wantStatus int
		wantCode   string
	}{
		{
			name: "not found",
			payload: map[string]interface{}{
				"success": false, "error_code": "not_found", "error": "Session not found", "session_id": "private",
			},
			wantStatus: http.StatusNotFound,
			wantCode:   CodeNotFound,
		},
		{
			name: "forbidden",
			payload: map[string]interface{}{
				"success": false, "error_code": "forbidden", "error": "Access denied",
			},
			wantStatus: http.StatusForbidden,
			wantCode:   CodeForbidden,
		},
		{
			name: "success payload is not an error",
			payload: map[string]interface{}{
				"success": true, "error_code": "not_found", "output": "ok",
			},
			wantStatus: http.StatusOK,
		},
		{
			name: "missing success is not an explicit error",
			payload: map[string]interface{}{
				"error_code": "not_found", "error": "Session not found",
			},
			wantStatus: http.StatusOK,
		},
		{
			name: "untyped executor failure stays compatible",
			payload: map[string]interface{}{
				"success": false, "error": "Session not found",
			},
			wantStatus: http.StatusOK,
		},
		{
			name: "unknown typed error stays compatible",
			payload: map[string]interface{}{
				"success": false, "error_code": "session_unavailable", "error": "Session unavailable",
			},
			wantStatus: http.StatusOK,
		},
		{
			name: "nested typed error is not a top-level contract",
			payload: map[string]interface{}{
				"success": false, "data": map[string]interface{}{"error_code": "not_found"},
			},
			wantStatus: http.StatusOK,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if got := r.URL.Query().Get("user_id"); got != "caller" {
					t.Errorf("forwarded user_id = %q, want caller", got)
				}
				w.Header().Set("Content-Type", "application/json")
				if err := json.NewEncoder(w).Encode(tt.payload); err != nil {
					t.Errorf("encode upstream response: %v", err)
				}
			}))
			defer srv.Close()

			mux := proxyMuxWithClaims(t, srv.URL, "caller")
			rec := httptest.NewRecorder()
			mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/chat/sessions/private/messages", nil))

			if rec.Code != tt.wantStatus {
				t.Fatalf("status = %d, want %d; body=%s", rec.Code, tt.wantStatus, rec.Body.String())
			}
			var got struct {
				Success bool            `json:"success"`
				Data    json.RawMessage `json:"data"`
				Error   string          `json:"error"`
				Code    string          `json:"code"`
			}
			if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
				t.Fatalf("decode gateway response: %v", err)
			}
			if tt.wantStatus == http.StatusOK {
				if !got.Success {
					t.Fatalf("compatible payload was changed to a failure: %s", rec.Body.String())
				}
				var data map[string]interface{}
				if err := json.Unmarshal(got.Data, &data); err != nil {
					t.Fatalf("decode response data: %v", err)
				}
				if !reflect.DeepEqual(data, tt.payload) {
					t.Fatalf("response data changed: got %#v, want %#v", data, tt.payload)
				}
				return
			}
			if got.Success || got.Code != tt.wantCode || got.Error == "" {
				t.Fatalf("unexpected typed error response: %s", rec.Body.String())
			}
			if len(got.Data) != 0 {
				t.Fatalf("denied engine payload leaked as success data: %s", rec.Body.String())
			}
		})
	}
}

func TestProxyCrossUserSubmitReturns404WithoutWrites(t *testing.T) {
	var mu sync.Mutex
	messages := []string{}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		userID := r.URL.Query().Get("user_id")
		tenantID := r.URL.Query().Get("tenant_id")
		var body map[string]interface{}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Errorf("decode submit body: %v", err)
		}
		sessionID, _ := body["session_id"].(string)
		if userID != "owner" || tenantID != "17" {
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false, "error_code": "not_found", "error": "Session not found", "session_id": sessionID,
			})
			return
		}
		mu.Lock()
		messages = append(messages, body["message"].(string))
		mu.Unlock()
		_ = json.NewEncoder(w).Encode(map[string]interface{}{
			"success": true, "session_id": sessionID, "output": "ok", "usage": map[string]interface{}{"model": "test-model"},
		})
	}))
	defer upstream.Close()

	usage := captureUsageEvents(t)
	submit := func(userID, message string) *httptest.ResponseRecorder {
		withCaller := proxyMuxWithClaims(t, upstream.URL, userID)
		body := `{"session_id":"private-session","message":"` + message + `","llm_config":{"model":"test-model"}}`
		rec := httptest.NewRecorder()
		withCaller.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/v1/chat/submit", strings.NewReader(body)))
		return rec
	}

	owned := submit("owner", "owner message")
	if owned.Code != http.StatusOK {
		t.Fatalf("owner submit status = %d; body=%s", owned.Code, owned.Body.String())
	}
	var ownedBody struct {
		Success bool `json:"success"`
		Data    struct {
			Success bool   `json:"success"`
			Output  string `json:"output"`
		} `json:"data"`
	}
	if err := json.Unmarshal(owned.Body.Bytes(), &ownedBody); err != nil {
		t.Fatalf("decode owner response: %v", err)
	}
	if !ownedBody.Success || !ownedBody.Data.Success || ownedBody.Data.Output != "ok" {
		t.Fatalf("ordinary success changed: %s", owned.Body.String())
	}

	blocked := submit("intruder", "must not be written")
	if blocked.Code != http.StatusNotFound {
		t.Fatalf("cross-user submit status = %d, want 404; body=%s", blocked.Code, blocked.Body.String())
	}
	if !strings.Contains(blocked.Body.String(), `"code":"not_found"`) {
		t.Fatalf("cross-user response lost typed code: %s", blocked.Body.String())
	}
	mu.Lock()
	gotMessages := append([]string(nil), messages...)
	mu.Unlock()
	if !reflect.DeepEqual(gotMessages, []string{"owner message"}) {
		t.Fatalf("cross-user submit wrote messages: %v", gotMessages)
	}
	if rows := usage.snapshot(); len(rows) != 1 {
		t.Fatalf("denied submit ran success-only side effects: usage rows=%d, want 1", len(rows))
	}
}
