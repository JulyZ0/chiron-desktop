package api

// deps_admin_test.go —— 依赖管理代理的四条不变量。
//
// 这些用例都**不需要**数据库/Redis：`db.AuditLog` 在 `db.Redis == nil` 时是 no-op，
// 而权限/透传/校验/互斥都发生在 handler 内、与存储无关。要证明的事：
//
//  1. **权限**：平台级门禁 —— 租户 admin（有 admin:write 但不是 owner）必须 403；
//  2. **令牌透传**：上游收到的 `X-Internal-Token` 来自 `PythonClient` 配置，而不是客户端；
//  3. **校验**：非法规格在网关就 400，且**不会**打到引擎（注入尝试不产生上游请求）；
//  4. **互斥/错误映射**：同一时刻只放一个变更请求；引擎的 400/409 原样透传，5xx 变 502。

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
)

func depsIdentityMW(next http.Handler) http.Handler { return next }

// newDepsMux 起一个假引擎 + 只挂 deps 路由的 mux（auth/rl 中间件用恒等透传，
// 这样测试直接验证 handler 层的权限与校验，不被 JWT 装配干扰）。
type depsTestRig struct {
	mux       *http.ServeMux
	mu        sync.Mutex
	requests  []string
	tokens    []string
	bodies    []string
	handler   http.HandlerFunc
	engineURL string
}

func newDepsRig(t *testing.T, handler http.HandlerFunc) *depsTestRig {
	t.Helper()
	rig := &depsTestRig{handler: handler}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		rig.mu.Lock()
		rig.requests = append(rig.requests, r.Method+" "+r.URL.RequestURI())
		rig.tokens = append(rig.tokens, r.Header.Get("X-Internal-Token"))
		rig.bodies = append(rig.bodies, string(body))
		rig.mu.Unlock()
		rig.handler(w, r)
	}))
	t.Cleanup(srv.Close)
	rig.engineURL = srv.URL

	pc := engine.NewPythonClient(srv.URL)
	pc.SetInternalToken("engine-internal-token")

	rig.mux = http.NewServeMux()
	registerDepsRoutes(rig.mux, depsIdentityMW, depsIdentityMW, pc)
	return rig
}

func (r *depsTestRig) seen() ([]string, []string, []string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]string(nil), r.requests...), append([]string(nil), r.tokens...), append([]string(nil), r.bodies...)
}

func ownerClaims() *auth.Claims {
	return &auth.Claims{UserID: "u-owner", Role: "owner", TenantID: 1}
}

// tenantAdminClaims 有 admin:read/admin:write，但角色不是 owner —— 平台级端点必须拒绝。
func tenantAdminClaims() *auth.Claims {
	return &auth.Claims{UserID: "u-admin", Role: "admin", TenantID: 2}
}

func withClaims(req *http.Request, claims *auth.Claims) *http.Request {
	return req.WithContext(auth.WithClaims(req.Context(), claims))
}

func TestDepsRoutesRejectNonPlatformOwner(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("非平台 owner 不应打到引擎：%s %s", r.Method, r.URL.Path)
	})

	cases := []struct {
		method, path, body string
	}{
		{http.MethodGet, "/v1/admin/deps/list", ""},
		{http.MethodGet, "/v1/admin/deps/search?q=requests", ""},
		{http.MethodGet, "/v1/admin/deps/jobs/abc123", ""},
		{http.MethodGet, "/v1/admin/deps/snapshots", ""},
		{http.MethodPost, "/v1/admin/deps/install", `{"spec":"requests"}`},
		{http.MethodPost, "/v1/admin/deps/uninstall", `{"name":"requests"}`},
		{http.MethodPost, "/v1/admin/deps/snapshots", `{"label":"x"}`},
		{http.MethodPost, "/v1/admin/deps/snapshots/20250101T000000000Z-abcdef/restore", `{"dry_run":true}`},
	}
	for _, c := range cases {
		t.Run(c.method+" "+c.path, func(t *testing.T) {
			req := httptest.NewRequest(c.method, c.path, strings.NewReader(c.body))
			req = withClaims(req, tenantAdminClaims())
			rec := httptest.NewRecorder()
			rig.mux.ServeHTTP(rec, req)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("租户 admin 应 403，得到 %d：%s", rec.Code, rec.Body.String())
			}
		})
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("被拒绝的请求不该到达引擎，实际：%v", reqs)
	}
}

// 未认证（claims 为 nil）同样拒绝：平台 owner 门禁不能因为"没身份"而放行。
func TestDepsRoutesRejectAnonymous(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("匿名请求不应打到引擎")
	})
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/deps/list", nil))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("匿名请求应 403，得到 %d", rec.Code)
	}
}

func TestDepsListForwardsInternalTokenAndBody(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/deps/list" {
			t.Errorf("上游路径错误：%s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"python":"3.12.0","executable":"/venv/bin/python","packages":[{"name":"fastapi","version":"0.115.0"}],"count":1}`))
	})

	req := withClaims(httptest.NewRequest(http.MethodGet, "/v1/admin/deps/list", nil), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	var body struct {
		Success bool `json:"success"`
		Data    struct {
			Packages []struct {
				Name    string `json:"name"`
				Version string `json:"version"`
			} `json:"packages"`
			Count int `json:"count"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON：%v (%s)", err, rec.Body.String())
	}
	if !body.Success || body.Data.Count != 1 || len(body.Data.Packages) != 1 {
		t.Fatalf("引擎响应未被正确透传：%s", rec.Body.String())
	}

	reqs, tokens, _ := rig.seen()
	if len(reqs) != 1 || reqs[0] != "GET /internal/deps/list" {
		t.Fatalf("上游请求不符合预期：%v", reqs)
	}
	if len(tokens) != 1 || tokens[0] != "engine-internal-token" {
		t.Fatalf("X-Internal-Token 未由网关注入（这是引擎唯一的信任凭据）：%v", tokens)
	}
}

func TestDepsSearchValidatesQuery(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"query":"requests","results":[],"count":0,"found":false}`))
	})

	for _, bad := range []string{"", " ", "requests; id", "a b", "../x", "-r"} {
		req := withClaims(httptest.NewRequest(
			http.MethodGet, "/v1/admin/deps/search?q="+url.QueryEscape(bad), nil,
		), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("q=%q 应 400，得到 %d", bad, rec.Code)
		}
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("非法查询不该打到引擎：%v", reqs)
	}

	req := withClaims(httptest.NewRequest(http.MethodGet, "/v1/admin/deps/search?q=requests", nil), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("合法查询应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 1 || reqs[0] != "GET /internal/deps/search?q=requests" {
		t.Fatalf("上游路径/查询参数不符合预期：%v", reqs)
	}
}

func TestDepsInstallRejectsInjectionBeforeEngine(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("非法规格不该打到引擎")
	})
	bad := []string{
		"requests; rm -rf /",
		"requests && curl evil",
		"pkg[extra]",
		"git+https://evil/x.git",
		"-r requirements.txt",
		".env",
		"pkg >=2",
		strings.Repeat("a", maxDepsSpecLen+1),
	}
	for _, spec := range bad {
		payload, _ := json.Marshal(map[string]string{"spec": spec})
		req := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/install", strings.NewReader(string(payload))), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("spec=%q 应 400，得到 %d：%s", spec, rec.Code, rec.Body.String())
		}
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("非法规格不该打到引擎：%v", reqs)
	}
}

func TestDepsInstallPassesSpecAndReturnsJob(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/deps/install" || r.Method != http.MethodPost {
			t.Errorf("上游请求错误：%s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"job":{"id":"job1","kind":"install","target":"requests==2.31.0","status":"queued","progress":0}}`))
	})

	req := withClaims(httptest.NewRequest(
		http.MethodPost, "/v1/admin/deps/install",
		strings.NewReader(`{"spec":"requests==2.31.0"}`),
	), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	reqs, tokens, bodies := rig.seen()
	if len(reqs) != 1 || reqs[0] != "POST /internal/deps/install" {
		t.Fatalf("上游请求不符合预期：%v", reqs)
	}
	if tokens[0] != "engine-internal-token" {
		t.Fatalf("内部令牌缺失：%v", tokens)
	}
	if strings.TrimSpace(bodies[0]) != `{"spec":"requests==2.31.0"}` {
		t.Fatalf("请求体应只含 spec，实际：%s", bodies[0])
	}
	if !strings.Contains(rec.Body.String(), `"job1"`) {
		t.Fatalf("job 未透传：%s", rec.Body.String())
	}
}

func TestDepsUninstallValidatesName(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("非法名称不该打到引擎")
	})
	for _, name := range []string{"", "requests==2", "-r x", ".env", "a/b", "pkg name"} {
		payload, _ := json.Marshal(map[string]string{"name": name})
		req := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/uninstall", strings.NewReader(string(payload))), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("name=%q 应 400，得到 %d", name, rec.Code)
		}
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("非法名称不该打到引擎：%v", reqs)
	}
}

// 引擎返回 409（已有任务在跑）必须原样透传成 409：前端据此复用同一个 job 的轮询，
// 而 502 会被读成"引擎挂了"。
func TestDepsEngineConflictIsPassedThrough(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":{"error":"another dependency task is already running","job_id":"busy1"}}`))
	})
	req := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/install", strings.NewReader(`{"spec":"requests"}`)), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("应透传 409，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "another dependency task is already running") {
		t.Fatalf("引擎的 detail 未透出：%s", rec.Body.String())
	}
}

func TestDepsEngineServerErrorBecomesBadGateway(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte("stack trace with internals"))
	})
	req := withClaims(httptest.NewRequest(http.MethodGet, "/v1/admin/deps/list", nil), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("上游 500 应映射成 502，得到 %d", rec.Code)
	}
	if strings.Contains(rec.Body.String(), "stack trace") {
		t.Fatalf("上游内部细节不该出现在响应体：%s", rec.Body.String())
	}
}

// 同一时刻只允许一个变更请求进入引擎（进程内闸）：第二个直接 409，不再打上游。
func TestDepsMutationsAreMutuallyExclusive(t *testing.T) {
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"job":{"id":"job1"}}`))
	})

	firstDone := make(chan int, 1)
	go func() {
		req := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/install", strings.NewReader(`{"spec":"requests"}`)), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		firstDone <- rec.Code
	}()

	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("第一个安装请求没有到达引擎")
	}

	second := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/install", strings.NewReader(`{"spec":"httpx"}`)), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, second)
	if rec.Code != http.StatusConflict {
		t.Fatalf("并发安装应 409，得到 %d：%s", rec.Code, rec.Body.String())
	}

	uninstall := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/uninstall", strings.NewReader(`{"name":"requests"}`)), ownerClaims())
	recU := httptest.NewRecorder()
	rig.mux.ServeHTTP(recU, uninstall)
	if recU.Code != http.StatusConflict {
		t.Fatalf("安装进行中卸载应 409，得到 %d", recU.Code)
	}

	close(release)
	if code := <-firstDone; code != http.StatusOK {
		t.Fatalf("第一个请求应成功，得到 %d", code)
	}
}

// 规格/名称正则与引擎侧同语义（此处锁住网关这一份，避免只改一侧）。
func TestDepsSpecAndNameRegex(t *testing.T) {
	valid := []string{"requests", "requests==2.31.0", "requests>=2.31", "requests~=2.0", "requests==2.*", "my-pkg_1.0", "zope.interface"}
	for _, spec := range valid {
		if !depsSpecRe.MatchString(spec) {
			t.Errorf("应接受规格 %q", spec)
		}
	}
	invalid := []string{"", "requests; id", "pkg[extra]", "git+https://x/y", "-r req.txt", ".env", "pkg >=2", "pkg/", "pkg==", "pkg\nnext"}
	for _, spec := range invalid {
		if depsSpecRe.MatchString(spec) {
			t.Errorf("应拒绝规格 %q", spec)
		}
	}
	for _, name := range []string{"requests", "Pillow", "my-pkg_1.0"} {
		if !depsNameRe.MatchString(name) {
			t.Errorf("应接受名称 %q", name)
		}
	}
	for _, name := range []string{"requests==2", "-r", ".env", "a b", "pkg/", "pkg-"} {
		if depsNameRe.MatchString(name) {
			t.Errorf("应拒绝名称 %q", name)
		}
	}
}

// 任务 id 形态约束：路径参数最终被拼进上游 URL，必须拒绝路径穿越。
//
// 注意用**编码后**的形态：未编码的 `../list` 会被 ServeMux 规范化成 307 重定向
// （那是 Go 路由层的行为，不是本 handler 的判定），所以真正的穿越尝试是
// `%2E%2E%2F` / `%2F` 这类"落在单个路径段里、解码后含斜杠"的写法。
func TestDepsJobIDRejectsPathTraversal(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("非法 job id 不该打到引擎")
	})
	for _, id := range []string{"..%2Flist", "a%2Fb", "abc%00", strings.Repeat("a", 70)} {
		req := withClaims(httptest.NewRequest(http.MethodGet, "/v1/admin/deps/jobs/"+id, nil), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound {
			t.Errorf("job id=%q 应被拒绝，得到 %d", id, rec.Code)
		}
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("非法 job id 不该打到引擎：%v", reqs)
	}
}

// ── 快照代理 ─────────────────────────────────────────────────────────────

// 读快照列表：透传引擎响应，令牌由网关注入（与 list 同口径）。
func TestDepsListSnapshotsForwardsInternalToken(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/internal/deps/snapshots" {
			t.Errorf("上游请求错误：%s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"snapshots":[{"id":"20250101T000000000Z-abcdef","package_count":2,"trigger":"manual","label":""}],"count":1}`))
	})

	req := withClaims(httptest.NewRequest(http.MethodGet, "/v1/admin/deps/snapshots", nil), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "20250101T000000000Z-abcdef") {
		t.Fatalf("快照列表未透传：%s", rec.Body.String())
	}
	reqs, tokens, _ := rig.seen()
	if len(reqs) != 1 || tokens[0] != "engine-internal-token" {
		t.Fatalf("上游请求/令牌不符合预期：%v %v", reqs, tokens)
	}
}

// 手动创建快照：label 只由网关注入体，超长在入口拒绝（引擎不必被惊动）。
func TestDepsCreateSnapshotValidatesLabel(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/internal/deps/snapshots" {
			t.Errorf("上游请求错误：%s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"snapshot":{"id":"20250101T000000000Z-abcdef","package_count":0}}`))
	})

	tooLong, _ := json.Marshal(map[string]string{"label": strings.Repeat("a", maxDepsSnapshotLabelLen+1)})
	req := withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/snapshots", strings.NewReader(string(tooLong))), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("超长 label 应 400，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("被拒绝的请求不该打到引擎：%v", reqs)
	}

	req = withClaims(httptest.NewRequest(http.MethodPost, "/v1/admin/deps/snapshots", strings.NewReader(`{"label":" before-upgrade "}`)), ownerClaims())
	rec = httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	_, _, bodies := rig.seen()
	if strings.TrimSpace(bodies[0]) != `{"label":"before-upgrade"}` {
		t.Fatalf("请求体应只含修剪后的 label，实际：%s", bodies[0])
	}
}

// 快照 id 会拼进上游 URL 的路径段：非法形态必须在网关就拒绝（不产生上游请求）。
func TestDepsRestoreSnapshotRejectsPathTraversal(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("非法 snapshot id 不该打到引擎：%s", r.URL.Path)
	})
	for _, id := range []string{
		"..%2Flist",
		"a%2Fb",
		"20250101T000000000Z-abcdef.json",
		"20250101T000000000Z",
		"00000000000000000Z-abcdef",
		strings.Repeat("a", 70),
	} {
		req := withClaims(httptest.NewRequest(
			http.MethodPost, "/v1/admin/deps/snapshots/"+id+"/restore", strings.NewReader(`{"dry_run":false}`),
		), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound {
			t.Errorf("id=%q 应被拒绝，得到 %d", id, rec.Code)
		}
	}
	if reqs, _, _ := rig.seen(); len(reqs) != 0 {
		t.Fatalf("非法 id 不该打到引擎：%v", reqs)
	}
}

// dry_run 只算差异：不占互斥闸，且请求体显式带上 true（引擎据此决定是否建任务）。
func TestDepsRestoreSnapshotDryRun(t *testing.T) {
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/deps/snapshots/20250101T000000000Z-abcdef/restore" {
			t.Errorf("上游路径错误：%s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"dry_run":true,"diff":{"install_count":1},"job":null}`))
	})

	req := withClaims(httptest.NewRequest(
		http.MethodPost, "/v1/admin/deps/snapshots/20250101T000000000Z-abcdef/restore",
		strings.NewReader(`{"dry_run":true}`),
	), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("应 200，得到 %d：%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "install_count") {
		t.Fatalf("差异摘要未透传：%s", rec.Body.String())
	}
	_, _, bodies := rig.seen()
	if strings.TrimSpace(bodies[0]) != `{"dry_run":true}` {
		t.Fatalf("dry_run 未透传：%s", bodies[0])
	}
}

// 真恢复与安装/卸载共用同一把进程内闸：一个在途时第二个直接 409，不打上游。
func TestDepsRestoreSnapshotTakesBusyGate(t *testing.T) {
	entered := make(chan struct{}, 1)
	release := make(chan struct{})
	// preview 为 1 时假引擎立刻返回（dry_run 预览不该被卡住）。rig 已经把请求体读走了，
	// 所以这里靠一个显式开关区分两类请求，而不是去解析 body。
	var preview int32
	rig := newDepsRig(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if atomic.LoadInt32(&preview) == 1 {
			_, _ = w.Write([]byte(`{"dry_run":true,"diff":{"install_count":1},"job":null}`))
			return
		}
		select {
		case entered <- struct{}{}:
		default:
		}
		<-release
		_, _ = w.Write([]byte(`{"dry_run":false,"job":{"id":"job1","kind":"restore"},"diff":{}}`))
	})

	firstDone := make(chan int, 1)
	go func() {
		req := withClaims(httptest.NewRequest(
			http.MethodPost, "/v1/admin/deps/snapshots/20250101T000000000Z-abcdef/restore",
			strings.NewReader(`{"dry_run":false}`),
		), ownerClaims())
		rec := httptest.NewRecorder()
		rig.mux.ServeHTTP(rec, req)
		firstDone <- rec.Code
	}()

	select {
	case <-entered:
	case <-time.After(3 * time.Second):
		t.Fatal("第一个恢复请求没有到达引擎")
	}

	second := withClaims(httptest.NewRequest(
		http.MethodPost, "/v1/admin/deps/snapshots/20250101T000000000Z-abcdef/restore",
		strings.NewReader(`{"dry_run":false}`),
	), ownerClaims())
	rec := httptest.NewRecorder()
	rig.mux.ServeHTTP(rec, second)
	if rec.Code != http.StatusConflict {
		t.Fatalf("并发恢复应 409，得到 %d：%s", rec.Code, rec.Body.String())
	}

	// dry_run 是只读预览，即便有变更任务在途也应当放行（不占闸）
	atomic.StoreInt32(&preview, 1)
	dry := withClaims(httptest.NewRequest(
		http.MethodPost, "/v1/admin/deps/snapshots/20250101T000000000Z-abcdef/restore",
		strings.NewReader(`{"dry_run":true}`),
	), ownerClaims())
	recDry := httptest.NewRecorder()
	rig.mux.ServeHTTP(recDry, dry)
	if recDry.Code != http.StatusOK {
		t.Fatalf("dry_run 不该被互斥闸拦下，得到 %d：%s", recDry.Code, recDry.Body.String())
	}
	close(release)
	if code := <-firstDone; code != http.StatusOK {
		t.Fatalf("第一个请求应成功，得到 %d", code)
	}
}
