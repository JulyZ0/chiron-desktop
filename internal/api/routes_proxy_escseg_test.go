package api

import (
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

// 回归背景（P0，越权 / IDOR）：
//
// 网关把客户端路径参数拼进转发给 Python 引擎的 URL，而 r.PathValue 返回的是
// **已解码**的值 —— 请求里的 %3F / %23 / %2F 到 handler 里已经还原成 ? / # / /。
// 旧实现直接字符串拼接：
//
//	proxiedPath := buildPath(r) + "?user_id=" + claims.UserID + "&tenant_id=" + ...
//
// 于是 `POST /v1/kb/abc%3Fuser_id%3Dvictim%26x%3D/documents` 会转发成
//
//	/v1/kb/abc?user_id=victim&x=/documents?user_id=<真实用户>&tenant_id=1
//
// query 里第一个 user_id 胜出，网关追加的那个被顶掉。而引擎侧身份完全取自 query
// （`user_id: str = Query("")`，见 python-engine/app/api/knowledge.py 全部端点），
// 所以任何拿到 kb_id 的人都能以受害者身份读写其知识库。
//
// 这些用例钉住两件事：① 注入的 ? 与 # 不能改变转发 URL 的结构；
// ② 注入的 %2F 不能穿越到别的路由。

// 攻击串里的 ? 必须被编码，否则它提前结束 path、开启一个客户端控制的 query，
// 把网关自己追加的 user_id 顶掉。
func TestEscSegBlocksQueryInjection(t *testing.T) {
	id := escSeg("abc?user_id=victim&x=")
	proxied := "/v1/kb/" + id + "?user_id=" + url.QueryEscape("real-user") + "&tenant_id=1"

	u, err := url.Parse("http://127.0.0.1:9999" + proxied)
	if err != nil {
		t.Fatalf("拼接后的 URL 必须可解析，实际报错：%v（拼接结果 %q）", err, proxied)
	}

	// 核心断言：user_id 必须仍是网关写入的真实用户。
	if got := u.Query().Get("user_id"); got != "real-user" {
		t.Fatalf("user_id 被路径参数覆盖：期望 real-user，实际 %q；转发 URL=%q", got, proxied)
	}
	if got := u.Query().Get("x"); got != "" {
		t.Errorf("路径参数引入了额外的 query 键 x=%q", got)
	}
	// ? 必须落在单个 path 段内，不能被当成 query 起始符。
	if want := "/v1/kb/" + id; u.EscapedPath() != want {
		t.Fatalf("路径结构被注入改变：期望 %q，实际 %q", want, u.EscapedPath())
	}
}

// %2F 解码成 / 会让路径参数跨越路由边界，必须被编码回去。
func TestEscSegBlocksPathTraversal(t *testing.T) {
	proxied := "/v1/kb/" + escSeg("a/b") + "?user_id=real-user&tenant_id=1"

	u, err := url.Parse("http://127.0.0.1:9999" + proxied)
	if err != nil {
		t.Fatalf("拼接后的 URL 必须可解析，实际报错：%v", err)
	}
	// 引擎侧按 EscapedPath 分段，%2F 保证 a/b 仍是一段而不是两级目录。
	if got := u.EscapedPath(); got != "/v1/kb/a%2Fb" {
		t.Fatalf("%%2F 未被编码，路径参数跨越了路由边界：期望 /v1/kb/a%%2Fb，实际 %q", got)
	}
}

// 端到端：真实 ServeMux 解出路径参数，再经 escSeg 拼接，注入不能生效。
func TestProxyPathParamQueryInjectionEndToEnd(t *testing.T) {
	var forwarded string
	mux := http.NewServeMux()
	// 与 registerProxyRoutes 中 "POST /v1/kb/{id}/documents" 的路由形状一致。
	mux.HandleFunc("POST /v1/kb/{id}/documents", func(w http.ResponseWriter, r *http.Request) {
		forwarded = "/v1/kb/" + escSeg(r.PathValue("id")) + "/documents?user_id=" +
			url.QueryEscape("real-user") + "&tenant_id=" + url.QueryEscape("1")
		w.WriteHeader(http.StatusOK)
	})

	// 攻击串：解码后 id 是 "abc?user_id=victim&x="，直接拼接会把 path 撑开。
	attack := "/v1/kb/abc%3Fuser_id%3Dvictim%26x%3D/documents"
	mux.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodPost, attack, nil))

	if forwarded == "" {
		t.Fatal("handler 未被调用：攻击串被路由拒绝，本用例失去意义（检查 mux 注册形状）")
	}
	u, err := url.Parse("http://127.0.0.1:9999" + forwarded)
	if err != nil {
		t.Fatalf("转发 URL 不可解析：%v（%q）", err, forwarded)
	}
	// 修复前的实际行为：这里会得到 "victim"，越权成立。
	if got := u.Query().Get("user_id"); got != "real-user" {
		t.Fatalf("越权成立：转发后 user_id=%q，网关写入的 real-user 被路径参数覆盖；转发 URL=%q", got, forwarded)
	}
	if got := u.Query().Get("x"); got != "" {
		t.Errorf("路径参数引入了额外 query 键 x=%q，转发 URL=%q", got, forwarded)
	}
}
