package api

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// 知识库检索观测（P1-6）必须能从网关走通。
//
// 回归背景：引擎侧注册了 `GET /v1/kb/{id}/stats` 与 `GET /v1/knowledge/{id}/stats`，
// 但网关的转发表是**逐条**声明的 —— 漏一条，前端拿到 404，而 404 与"这项功能
// 没做"在界面上长得一模一样（正是 TestKnowledgeRoutesAreExposed 钉过的同一类问题）。
// 这个用例钉住两条路径都能转发到引擎，且身份来自 claims。
func TestKnowledgeStatsRoutesForwarded(t *testing.T) {
	var got []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = append(got, r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"kb_id":"kb-1","available":true}`))
	}))
	defer srv.Close()

	withClaims := func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := auth.WithClaims(r.Context(), &auth.Claims{UserID: "real-user", TenantID: 7})
			h.ServeHTTP(w, r.WithContext(ctx))
		})
	}

	mux := http.NewServeMux()
	registerProxyRoutes(mux, withClaims, withClaims, withClaims, engine.NewPythonClient(srv.URL), NewUserSemaphorePool(nil, 0))

	for _, path := range []string{"/v1/kb/kb-1/stats", "/v1/knowledge/kb-1/stats"} {
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code != http.StatusOK {
			t.Fatalf("GET %s 应转发成功，得到 %d: %s", path, rec.Code, rec.Body.String())
		}
	}
	if len(got) != 2 {
		t.Fatalf("两条统计路径都应转发到引擎，实际转发 %d 次: %v", len(got), got)
	}
	if got[0] != "/v1/kb/kb-1/stats" || got[1] != "/v1/knowledge/kb-1/stats" {
		t.Fatalf("转发路径不匹配：%v", got)
	}
}
