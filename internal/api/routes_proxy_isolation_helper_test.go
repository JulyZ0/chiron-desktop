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

// 「受限令牌执行助手」三条端点在网关上的存在性。
//
// 回归背景（真机事故）：`GET/POST/DELETE /v1/system/isolation/helper` 在
// `registerProxyRoutes` 里**一条都没注册**。引擎侧
// （`python-engine/app/api/isolation.py`）三条都有，于是表现为：
//
//	设置页点「启用助手」→ 网关 404 → 前端 catch 落到兜底文案
//	→ 「受限令牌执行助手：启用助手失败：本机无法启用此档位」
//
// 关键点：这跟管理员身份、令牌权限、UAC 都无关 —— **任何机器都必然失败**。
// 排查时极易误判成"缺 SeIncreaseQuotaPrivilege"（那是引擎侧 `restricted`
// 不可用的原因，不是"启用助手失败"的原因）。
//
// 这三条缺任何一条都会退化成不同的假象，所以三条一起钉：
//   - 缺 GET：`helperStatus` 恒 null，界面既不提示"助手挡住了 restricted"，
//     也不会出现「启用助手」按钮；
//   - 缺 POST：点了只有兜底文案；
//   - 缺 DELETE：用户无法撤销这条提权通道。
func TestIsolationHelperRoutesForwardToEngine(t *testing.T) {
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.Method+" "+r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"available":true,"detail":"","engine_elevated":false,"supported":true}`))
	}))
	defer srv.Close()

	// 只保留"注入 claims"这一层：鉴权/限流各有自己的测试，这里只钉路由存在性。
	// 角色用 admin —— 三条路由都要求 admin:read / admin:write。
	withClaims := func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := auth.WithClaims(r.Context(), &auth.Claims{UserID: "owner", TenantID: 7, Role: "admin"})
			h.ServeHTTP(w, r.WithContext(ctx))
		})
	}

	mux := http.NewServeMux()
	registerProxyRoutes(mux, withClaims, withClaims, withClaims, engine.NewPythonClient(srv.URL), NewUserSemaphorePool(nil, 0))

	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodDelete} {
		req := httptest.NewRequest(method, "/v1/system/isolation/helper", nil)
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)

		if rec.Code == http.StatusNotFound || rec.Code == http.StatusMethodNotAllowed {
			t.Fatalf("%s /v1/system/isolation/helper 没有到达引擎（网关响应 %d: %s）——"+
				"正是这条缺失让前端只能显示「本机无法启用此档位」", method, rec.Code, rec.Body.String())
		}
		if rec.Code != http.StatusOK {
			t.Fatalf("%s /v1/system/isolation/helper 失败：%d %s", method, rec.Code, rec.Body.String())
		}
	}

	want := []string{
		"GET /v1/system/isolation/helper",
		"POST /v1/system/isolation/helper",
		"DELETE /v1/system/isolation/helper",
	}
	if len(paths) != len(want) {
		t.Fatalf("期望 %d 次转发，实际 %d 次：%v", len(want), len(paths), paths)
	}
	for i, w := range want {
		if paths[i] != w {
			t.Errorf("第 %d 次转发路径不对：期望 %q，实际 %q", i, w, paths[i])
		}
	}
}

// 失败原因必须**原样**透传到前端。
//
// 「启用助手失败」有三种性质完全不同的事故，界面文案也完全不同：
//   - 用户取消 UAC（`cancelled=true`，正常选择，不该报错）；
//   - 真的起不来（`reason` 是能照做的指引或 Win32 细节）；
//   - 网关/网络错误（前端兜底文案）。
//
// 网关若把 `reason` 吞掉或改写，第二类就只能显示成"本机无法启用此档位"，
// 而用户需要的那句"要在 UAC 里允许一次"就丢了。
func TestIsolationHelperFailureReasonIsPassedThrough(t *testing.T) {
	const reason = "启动提权 helper 被拒绝，ShellExecuteW=5（用户取消了 UAC 授权）"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		payload, _ := json.Marshal(map[string]interface{}{
			"ok": false, "cancelled": false, "reason": reason,
			"helper": map[string]interface{}{
				"available": false, "detail": "", "engine_elevated": false, "supported": true,
			},
		})
		_, _ = w.Write(payload)
	}))
	defer srv.Close()

	withClaims := func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			ctx := auth.WithClaims(r.Context(), &auth.Claims{UserID: "owner", TenantID: 7, Role: "admin"})
			h.ServeHTTP(w, r.WithContext(ctx))
		})
	}

	mux := http.NewServeMux()
	registerProxyRoutes(mux, withClaims, withClaims, withClaims, engine.NewPythonClient(srv.URL), NewUserSemaphorePool(nil, 0))

	req := httptest.NewRequest(http.MethodPost, "/v1/system/isolation/helper", strings.NewReader(""))
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("网关响应 %d: %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, "启动提权 helper 被拒绝") {
		t.Fatalf("引擎给的 reason 没有透传到客户端，前端只能显示兜底文案：%s", body)
	}
	if !strings.Contains(body, `"ok":false`) {
		t.Errorf("ok=false 没有透传：%s", body)
	}
}
