package api

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── 可选 SPA 静态托管（CHIRON_WEB_ROOT）──
//
// 分享链接要有一个「别人也能打开」的宿主：网关在自己的端口上托管 dist/，未命中静态
// 文件的 GET 回落 index.html（客户端路由）。这组断言守三件事：
// 能打开分享页、不列目录、不吞 API。

// newWebRootFixture 造一个最小 dist/：index.html + assets/app.js + 一个"敏感文件"。
func newWebRootFixture(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "assets"), 0o755); err != nil {
		t.Fatalf("mkdir assets: %v", err)
	}
	write := func(name, body string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(body), 0o644); err != nil {
			t.Fatalf("write %s: %v", name, err)
		}
	}
	write("index.html", `<!doctype html><html><head></head><body><script src="./assets/app.js"></script>spa-shell</body></html>`)
	write(filepath.Join("assets", "app.js"), "console.log('app')")
	write(".env", "SECRET=1")
	// 目录外文件：路径穿越若被拦错，会把它当静态文件吐出去。
	if err := os.WriteFile(filepath.Join(filepath.Dir(root), "outside-secret.txt"),
		[]byte("leaked"), 0o644); err != nil {
		t.Fatalf("write outside secret: %v", err)
	}
	return root
}

func serveSPA(t *testing.T, h http.Handler, method, target string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(method, target, nil))
	return rec
}

// 分享页路径 /share/<token> 必须回落 index.html —— 这是引入 SPA 托管的**唯一目的**。
func TestSPAHandler_InjectsRootBaseForRelativeAssets(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	rec := serveSPA(t, h, http.MethodGet, "/s/oqum3iu4opa75c7l")
	if got := rec.Body.String(); !strings.Contains(got, `<base href="/">`) {
		t.Fatalf("托管 index.html 必须注入 <base href=\"/\">，否则 /s/<token> 下 ./assets/* 会 404: %q", got)
	}
}

func TestSPAHandler_FallsBackToIndexForSharePage(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	for _, target := range []string{"/", "/share/oqum3iu4opa75c7l", "/settings/data", "/assets"} {
		rec := serveSPA(t, h, http.MethodGet, target)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s: want 200, got %d", target, rec.Code)
		}
		if !strings.Contains(rec.Body.String(), "spa-shell") {
			t.Fatalf("%s: 应回落 index.html, got %q", target, rec.Body.String())
		}
		if ct := rec.Header().Get("Content-Type"); !strings.Contains(ct, "text/html") {
			t.Fatalf("%s: Content-Type 应为 html, got %q", target, ct)
		}
	}
}

// 真实存在的静态文件按文件返回（JS 的 Content-Type 由 FileServer 给出）。
func TestSPAHandler_ServesExistingAsset(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	rec := serveSPA(t, h, http.MethodGet, "/assets/app.js")
	if rec.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "console.log") {
		t.Fatalf("静态文件未返回正文: %q", rec.Body.String())
	}
	if ct := rec.Header().Get("Content-Type"); !strings.Contains(ct, "javascript") {
		t.Fatalf("Content-Type 应为 javascript, got %q", ct)
	}
}

// API 前缀不能被 SPA 兜底吞掉：未注册的 /v1/* 必须是 404，而不是一页 HTML。
func TestSPAHandler_DoesNotSwallowAPI(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	for _, target := range []string{
		"/v1/does-not-exist",
		"/v1",
		"/healthz",
		"/readyz",
		"/metrics",
		"/docs/index.html",
	} {
		rec := serveSPA(t, h, http.MethodGet, target)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s: want 404, got %d", target, rec.Code)
		}
		if strings.Contains(rec.Body.String(), "spa-shell") {
			t.Fatalf("%s: API 路径回落了 index.html", target)
		}
	}
}

// 只 GET（含 HEAD）；写方法不接管。
func TestSPAHandler_OnlyReadMethods(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodDelete, http.MethodPatch} {
		rec := serveSPA(t, h, method, "/share/tok")
		if rec.Code != http.StatusNotFound && rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s: 应拒绝, got %d", method, rec.Code)
		}
	}
}

// 不回显目录内容，也不许隐藏文件外泄。
func TestSPAHandler_NoListingNoDotfiles(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	for _, target := range []string{"/assets/", "/.env", "/%2eenv"} {
		rec := serveSPA(t, h, http.MethodGet, target)
		body := rec.Body.String()
		if strings.Contains(body, "console.log") || strings.Contains(body, "SECRET=1") {
			t.Fatalf("%s: 泄漏了目录内容/隐藏文件: %q", target, body)
		}
	}
}

// 路径穿越必须被拦下：`../outside-secret.txt` 不能读到 web root 之外。
func TestSPAHandler_RejectsPathTraversal(t *testing.T) {
	h, err := newSPAHandler(newWebRootFixture(t))
	if err != nil {
		t.Fatalf("newSPAHandler: %v", err)
	}
	for _, target := range []string{
		"/../outside-secret.txt",
		"/assets/../../outside-secret.txt",
		"/..%2foutside-secret.txt",
	} {
		rec := serveSPA(t, h, http.MethodGet, target)
		if strings.Contains(rec.Body.String(), "leaked") {
			t.Fatalf("%s: 路径穿越读到了 web root 之外的内容", target)
		}
	}
}

// 未配置 / 配置错误时不挂载，也不让网关起不来。
func TestMountSPA_DisabledByDefaultAndOnBadRoot(t *testing.T) {
	mux := http.NewServeMux()
	if mountSPA(mux, "") {
		t.Fatal("空 CHIRON_WEB_ROOT 不该挂载 SPA")
	}
	if mountSPA(mux, filepath.Join(t.TempDir(), "missing")) {
		t.Fatal("目录不存在时不该挂载 SPA")
	}
	// 兜底未被注册 ⇒ "/" 仍是 404（与引入前一致）。
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("未挂载时 / 应为 404, got %d", rec.Code)
	}
}

// 挂载后不与更具体的 API 路由抢：ServeMux 按最具体匹配，/v1/ping 仍归它自己。
func TestMountSPA_DoesNotShadowAPIRoutes(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/ping", func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("pong"))
	})
	if !mountSPA(mux, newWebRootFixture(t)) {
		t.Fatal("合法目录应挂载成功")
	}

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/ping", nil))
	if rec.Body.String() != "pong" {
		t.Fatalf("API 路由被 SPA 兜底覆盖: %q", rec.Body.String())
	}

	rec = httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/share/tok", nil))
	if !strings.Contains(rec.Body.String(), "spa-shell") {
		t.Fatalf("分享页未回落到 SPA: %q", rec.Body.String())
	}
}
