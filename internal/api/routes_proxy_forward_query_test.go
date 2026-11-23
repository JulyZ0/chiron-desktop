package api

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

// forwardQuery 是 C3「记忆按项目隔离」在网关层唯一的通路。
//
// 回归背景：newProxy 的转发路径由 `buildPath(r) + "?user_id=…&tenant_id=…"` 拼成，
// **客户端原始 query 整个被丢弃**。而引擎侧从 `?project=` 读项目维度
// （python-engine/app/api/memory.py 的 `_project`），于是记忆页选了项目、
// 到网关这一跳就没了：看到的是全局条目，新写入的也落回全局，
// 隔离"看起来做了、实际完全没生效"。
//
// 这个用例钉住两件事：
//  1. 白名单里的键（project / archived / limit）确实被转发到引擎；
//  2. 身份始终来自 claims —— 客户端自带 `?user_id=victim` 不会跟着走。
func TestProxyForwardsWhitelistedQueryOnly(t *testing.T) {
	var got *url.URL
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r.URL
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	// 三个中间件这里都退化成"注入 claims"：本用例只验证 query 拼接，
	// 鉴权与限流各有自己的测试。
	withClaims := func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := auth.WithClaims(r.Context(), &auth.Claims{UserID: "real-user", TenantID: 7})
			h.ServeHTTP(w, r.WithContext(ctx))
		})
	}

	mux := http.NewServeMux()
	registerProxyRoutes(mux, withClaims, withClaims, withClaims, engine.NewPythonClient(srv.URL), NewUserSemaphorePool(nil, 0))

	req := httptest.NewRequest(
		http.MethodGet,
		"/v1/memory/profile?project=proj-a&archived=true&limit=5&user_id=victim&tenant_id=99&evil=1",
		nil,
	)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if got == nil {
		t.Fatalf("请求没有转发到引擎；网关响应 %d: %s", rec.Code, rec.Body.String())
	}
	q := got.Query()

	if v := q.Get("project"); v != "proj-a" {
		t.Errorf("project 未透传：期望 proj-a，实际 %q（转发到了 %s）", v, got.RequestURI())
	}
	if v := q.Get("archived"); v != "true" {
		t.Errorf("archived 未透传：期望 true，实际 %q", v)
	}
	if v := q.Get("limit"); v != "5" {
		t.Errorf("limit 未透传：期望 5，实际 %q", v)
	}

	// 身份永远来自 claims：客户端伪造的同名参数不能透传过来
	if v := q.Get("user_id"); v != "real-user" {
		t.Fatalf("越权：user_id 应为 real-user，实际 %q；转发 URL=%s", v, got.RequestURI())
	}
	if v := q.Get("tenant_id"); v != "7" {
		t.Fatalf("越权：tenant_id 应为 7，实际 %q", v)
	}
	if _, ok := q["evil"]; ok {
		t.Errorf("白名单外的键 evil 被透传了：%s", got.RequestURI())
	}
}
