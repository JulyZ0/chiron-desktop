package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/market"
)

// ── 测试脚手架：假 installer 注入 + 假目录 ──
//
// 安装 handler 的落点依赖 DB / 文件系统 / Python 引擎，三者都不该出现在单测里。
// 这里只替换**编排层注入的落点**，HTTP 适配、参数校验、错误映射全部走真实代码路径。

// stubInstaller 记录调用并可注入失败。
type stubInstaller struct {
	installs   int
	uninstalls int
	lastItem   market.Item
	lastEnv    map[string]string
	installErr error
	uninstErrr error
}

func (s *stubInstaller) Install(_ context.Context, item market.Item, env map[string]string) (map[string]any, error) {
	if s.installErr != nil {
		return nil, s.installErr
	}
	s.installs++
	s.lastItem = item
	s.lastEnv = env
	return map[string]any{"id": "local-" + item.ID, "name": item.Name}, nil
}

func (s *stubInstaller) Uninstall(_ context.Context, item market.Item, ref map[string]any) error {
	if s.uninstErrr != nil {
		return s.uninstErrr
	}
	s.uninstalls++
	return nil
}

// testInstallEnv 是一套测试用的 handler + 各 kind 的假落点。
type testInstallEnv struct {
	h     *MarketInstallHandler
	store *memoryInstallStore
	insts map[market.Kind]*stubInstaller
}

// newTestInstallEnv 构造 handler：目录用内置（真实 entry 才能走完 Find），
// 落点全部换成 stub，store 用进程内实现。
func newTestInstallEnv(t *testing.T, allowRemoteMCP bool) *testInstallEnv {
	t.Helper()
	insts := map[market.Kind]*stubInstaller{}
	mapped := map[market.Kind]market.Installer{}
	for _, k := range market.Kinds {
		s := &stubInstaller{}
		insts[k] = s
		mapped[k] = s
	}
	store := newMemoryInstallStore()
	return &testInstallEnv{
		h: &MarketInstallHandler{
			svc:     market.NewService(nil),
			install: market.NewInstallService(store, mapped, market.InstallOptions{AllowRemoteMCP: allowRemoteMCP}),
		},
		store: store,
		insts: insts,
	}
}

// doInstall 发一次安装请求（带登录态）。
func (e *testInstallEnv) doInstall(body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodPost, "/v1/market/install", strings.NewReader(body))
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()
	e.h.Install(rec, req)
	return rec
}

// doUninstall 发一次卸载请求。
func (e *testInstallEnv) doUninstall(source, kind, id string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodDelete, "/v1/market/install/"+source+"/"+kind+"/"+id, nil)
	req.SetPathValue("source", source)
	req.SetPathValue("kind", kind)
	req.SetPathValue("id", id)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()
	e.h.Uninstall(rec, req)
	return rec
}

// doList 发一次已安装列表请求。
func (e *testInstallEnv) doList(query string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(http.MethodGet, "/v1/market/install"+query, nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()
	e.h.List(rec, req)
	return rec
}

// decodeInstallData 解出成功响应的 data 段。
func decodeInstallData(t *testing.T, rec *httptest.ResponseRecorder, out any) {
	t.Helper()
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Success bool            `json:"success"`
		Data    json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析响应失败: %v; body=%s", err, rec.Body.String())
	}
	if !resp.Success {
		t.Fatalf("success = false; body=%s", rec.Body.String())
	}
	if err := json.Unmarshal(resp.Data, out); err != nil {
		t.Fatalf("解析 data 失败: %v; body=%s", err, rec.Body.String())
	}
}

// ── 成功路径 ──

// 内置 agent：装完 installed=true、enabled=true、requires_approval=false，
// 响应字段与前端契约一一对应。
func TestMarketInstall_BuiltinAgentSuccess(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doInstall(`{"source":"builtin","kind":"agent","id":"agent:code-reviewer"}`)

	var got installResponse
	decodeInstallData(t, rec, &got)
	if !got.Installed || got.AlreadyInstalled {
		t.Errorf("installed=%v already=%v，首次安装应为 installed=true/未重复", got.Installed, got.AlreadyInstalled)
	}
	if got.Source != "builtin" || got.Kind != market.KindAgent || got.ID != "agent:code-reviewer" {
		t.Errorf("响应身份字段不符: %+v", got)
	}
	if !got.Enabled {
		t.Error("内置条目装完应 enabled")
	}
	if got.RequiresApproval {
		t.Error("内置 agent 不应 requires_approval")
	}
	if got.Ref["id"] != "local-agent:code-reviewer" {
		t.Errorf("ref 未透出落点引用: %v", got.Ref)
	}
	if env.insts[market.KindAgent].installs != 1 {
		t.Error("落点未被调用一次")
	}
}

// env 必须透传到落点（安装表单里填的凭据）。
func TestMarketInstall_PassesEnv(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doInstall(`{"source":"builtin","kind":"agent","id":"agent:doc-writer","env":{"GITHUB_TOKEN":"abc"}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d; body=%s", rec.Code, rec.Body.String())
	}
	if env.insts[market.KindAgent].lastEnv["GITHUB_TOKEN"] != "abc" {
		t.Errorf("env 未透传: %v", env.insts[market.KindAgent].lastEnv)
	}
}

// 同一请求发两次：第二次 200 + already_installed=true，落点只调用一次。
func TestMarketInstall_IdempotentRepeated(t *testing.T) {
	env := newTestInstallEnv(t, false)
	body := `{"source":"builtin","kind":"skill","id":"skill:brainstorming"}`

	first := env.doInstall(body)
	if first.Code != http.StatusOK {
		t.Fatalf("首次安装 status = %d; body=%s", first.Code, first.Body.String())
	}

	rec := env.doInstall(body)
	var got installResponse
	decodeInstallData(t, rec, &got)
	if !got.AlreadyInstalled {
		t.Error("重复安装必须返回 already_installed=true（幂等），而不是报错")
	}
	if env.insts[market.KindSkill].installs != 1 {
		t.Errorf("落点被调用 %d 次，want 1", env.insts[market.KindSkill].installs)
	}
}

// ── 状态码契约 ──

// 缺字段 → 400。
func TestMarketInstall_MissingFieldsIs400(t *testing.T) {
	env := newTestInstallEnv(t, false)
	for _, body := range []string{
		`{"kind":"agent","id":"agent:code-reviewer"}`,
		`{"source":"builtin","id":"agent:code-reviewer"}`,
		`{"source":"builtin","kind":"agent"}`,
		`{`,
	} {
		if rec := env.doInstall(body); rec.Code != http.StatusBadRequest {
			t.Errorf("body=%s status = %d, want 400", body, rec.Code)
		}
	}
}

// kind 非法 → 400（不是静默当成某一类）。
func TestMarketInstall_InvalidKindIs400(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doInstall(`{"source":"builtin","kind":"widget","id":"agent:code-reviewer"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%s", rec.Code, rec.Body.String())
	}
}

// kind 与条目真实类型不符 → 400（否则会把 agent 条目按 skill 装到错误落点）。
func TestMarketInstall_KindMismatchIs400(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doInstall(`{"source":"builtin","kind":"skill","id":"agent:code-reviewer"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%s", rec.Code, rec.Body.String())
	}
}

// 目录里没有该条目 → 404（不接受调用方自带 payload）。
func TestMarketInstall_UnknownItemIs404(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doInstall(`{"source":"builtin","kind":"agent","id":"agent:nope"}`)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body=%s", rec.Code, rec.Body.String())
	}
}

// 外部源条目在 MARKET_ALLOW_REMOTE_MCP=false 时 → 403，且原因写清楚。
// 这里直接用编排层构造一条"外部 mcp"条目（目录里没有外部源，但策略闸在落点之前）。
func TestMarketInstall_ExternalMCPForbiddenIs403(t *testing.T) {
	env := newTestInstallEnv(t, false)
	_, err := env.h.install.Install(context.Background(), "1", "u1", market.Item{
		Kind: market.KindMCP, ID: "mcp:evil", Name: "evil", Source: "team",
		Payload: []byte(`{"command":"curl"}`),
	}, nil)
	if !errors.Is(err, market.ErrRemoteMCPForbidden) {
		t.Fatalf("err = %v，want ErrRemoteMCPForbidden", err)
	}

	rec := httptest.NewRecorder()
	writeInstallError(rec, err)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(strings.ToLower(rec.Body.String()), "market_allow_remote_mcp") {
		t.Errorf("403 响应应说明开关名，便于用户知道怎么打开: %s", rec.Body.String())
	}
}

// 落点失败 → 502（不是 500：网关自己没坏）。
func TestMarketInstall_UpstreamFailureIs502(t *testing.T) {
	env := newTestInstallEnv(t, false)
	env.insts[market.KindKnowledge].installErr = fmt.Errorf("%w: engine unreachable", errInstallUpstream)
	rec := env.doInstall(`{"source":"builtin","kind":"knowledge","id":"knowledge:blank"}`)
	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502; body=%s", rec.Code, rec.Body.String())
	}
}

// 本部署缺该 kind 的落点（无 DB 装 agent、无引擎装 skill）→ 501，不伪造成功。
func TestMarketInstall_MissingCapabilityIs501(t *testing.T) {
	env := newTestInstallEnv(t, false)
	env.insts[market.KindSkill].installErr = fmt.Errorf("%w: python engine unavailable", errInstallNotImplemented)
	rec := env.doInstall(`{"source":"builtin","kind":"skill","id":"skill:brainstorming"}`)
	if rec.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501; body=%s", rec.Code, rec.Body.String())
	}
}

// 引擎明确回 400（如"请先选择知识库 provider"）→ 原样 400，而不是含糊的 502：
// 这类消息里有用户能照做的下一步。
func TestMarketInstall_UpstreamBadRequestStays400(t *testing.T) {
	env := newTestInstallEnv(t, false)
	env.insts[market.KindKnowledge].installErr = &upstreamError{
		status: http.StatusBadRequest,
		detail: "create knowledge base: 请先在知识库设置中选择 provider",
	}
	rec := env.doInstall(`{"source":"builtin","kind":"knowledge","id":"knowledge:blank"}`)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "provider") {
		t.Errorf("可操作原因被吞掉了: %s", rec.Body.String())
	}
}

// 未登录 → 401（安装是用户级写操作，不允许匿名）。
func TestMarketInstall_RequiresAuth(t *testing.T) {
	env := newTestInstallEnv(t, false)
	req := httptest.NewRequest(http.MethodPost, "/v1/market/install",
		strings.NewReader(`{"source":"builtin","kind":"agent","id":"agent:code-reviewer"}`))
	rec := httptest.NewRecorder()
	env.h.Install(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("status = %d, want 401", rec.Code)
	}
}

// ── 卸载 ──

// 卸载已安装条目 → 200 uninstalled=true；再卸一次仍 200（幂等）。
func TestMarketUninstall_Idempotent(t *testing.T) {
	env := newTestInstallEnv(t, false)
	if rec := env.doInstall(`{"source":"builtin","kind":"agent","id":"agent:code-reviewer"}`); rec.Code != http.StatusOK {
		t.Fatalf("安装失败: %s", rec.Body.String())
	}

	for i := 0; i < 2; i++ {
		rec := env.doUninstall("builtin", "agent", "agent:code-reviewer")
		var got map[string]bool
		decodeInstallData(t, rec, &got)
		if !got["uninstalled"] {
			t.Errorf("第 %d 次卸载未返回 uninstalled=true: %s", i+1, rec.Body.String())
		}
	}
	if env.insts[market.KindAgent].uninstalls != 1 {
		t.Errorf("落点卸载 %d 次，want 1（第二次没有记录，不该再调落点）",
			env.insts[market.KindAgent].uninstalls)
	}
}

// 卸载目录里已消失的条目仍成功：外部源下线后用户要有权卸掉旧东西。
func TestMarketUninstall_UnknownItemStillSucceeds(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doUninstall("team", "skill", "skill:gone")
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200（幂等卸载）; body=%s", rec.Code, rec.Body.String())
	}
}

// 卸载参数里的 kind 非法 → 400。
func TestMarketUninstall_InvalidKindIs400(t *testing.T) {
	env := newTestInstallEnv(t, false)
	if rec := env.doUninstall("builtin", "widget", "agent:code-reviewer"); rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// ── 列表 ──

// GET /v1/market/install?kind= 返回 { items: [...] }，字段含 installed_at。
func TestMarketInstallList_ReturnsItems(t *testing.T) {
	env := newTestInstallEnv(t, false)
	if rec := env.doInstall(`{"source":"builtin","kind":"agent","id":"agent:code-reviewer"}`); rec.Code != http.StatusOK {
		t.Fatalf("安装失败: %s", rec.Body.String())
	}
	if rec := env.doInstall(`{"source":"builtin","kind":"skill","id":"skill:brainstorming"}`); rec.Code != http.StatusOK {
		t.Fatalf("安装失败: %s", rec.Body.String())
	}

	var got struct {
		Items []market.InstalledRecord `json:"items"`
	}
	decodeInstallData(t, env.doList(""), &got)
	if len(got.Items) != 2 {
		t.Fatalf("items = %d, want 2; %+v", len(got.Items), got.Items)
	}
	first := got.Items[0]
	if first.Source == "" || first.Kind == "" || first.MarketID == "" {
		t.Errorf("列表项缺少身份字段: %+v", first)
	}
	if first.CreatedAt.IsZero() {
		t.Error("installed_at 为空")
	}

	var onlySkills struct {
		Items []market.InstalledRecord `json:"items"`
	}
	decodeInstallData(t, env.doList("?kind=skill"), &onlySkills)
	if len(onlySkills.Items) != 1 || onlySkills.Items[0].Kind != market.KindSkill {
		t.Errorf("kind 过滤失效: %+v", onlySkills.Items)
	}
}

// 列表的 kind 非法 → 400（与目录查询同一口径）。
func TestMarketInstallList_InvalidKindIs400(t *testing.T) {
	env := newTestInstallEnv(t, false)
	if rec := env.doList("?kind=widget"); rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// 空列表返回 `items: []` 而不是 null（前端直接遍历，null 会炸）。
func TestMarketInstallList_EmptyIsArray(t *testing.T) {
	env := newTestInstallEnv(t, false)
	rec := env.doList("")
	if !strings.Contains(rec.Body.String(), `"items":[]`) {
		t.Fatalf("空列表应序列化成 []: %s", rec.Body.String())
	}
}

// ── 存储实现 ──

// 进程内 store 必须按 (tenant, user) 隔离：同租户另一个用户看不到别人装的条目。
func TestMemoryInstallStore_IsolatesUsers(t *testing.T) {
	s := newMemoryInstallStore()
	rec := market.InstalledRecord{Source: "builtin", Kind: market.KindAgent, MarketID: "agent:x"}
	if err := s.Upsert(context.Background(), "1", "u1", rec); err != nil {
		t.Fatal(err)
	}

	got, ok, err := s.Get(context.Background(), "1", "u2", rec)
	if err != nil {
		t.Fatal(err)
	}
	if ok {
		t.Errorf("跨用户命中了安装记录: %+v", got)
	}

	list, err := s.List(context.Background(), "1", "u2", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 0 {
		t.Errorf("跨用户列表串了: %+v", list)
	}
}

// ── 落点实现的静态检查（不联网、不连库的回退分支） ──
//
// 这几条断言守的是"能力缺失时明确失败"：没有引擎/没有插件存储时必须报错，
// 绝不能返回一个假的 ref。
func TestInstallers_FailClosedWithoutUpstream(t *testing.T) {
	ctx := auth.WithClaims(context.Background(), &auth.Claims{UserID: "u1", TenantID: 1})
	cases := []struct {
		name string
		inst market.Installer
		item market.Item
	}{
		{"skill without engine", skillMarketInstaller{py: nil}, market.Item{
			Kind: market.KindSkill, ID: "skill:x", Name: "x", Source: "builtin", Payload: []byte(`{}`)}},
		{"knowledge without engine", knowledgeMarketInstaller{py: nil}, market.Item{
			Kind: market.KindKnowledge, ID: "knowledge:x", Name: "x", Source: "builtin", Payload: []byte(`{}`)}},
		{"mcp without plugin store", mcpMarketInstaller{plugins: nil}, market.Item{
			Kind: market.KindMCP, ID: "mcp:x", Name: "x", Source: "builtin", Payload: []byte(`{"command":"npx"}`)}},
	}
	for _, c := range cases {
		if _, err := c.inst.Install(ctx, c.item, nil); err == nil {
			t.Errorf("%s: 上游缺失时必须返回错误，不能伪造成功", c.name)
		}
	}
}

// classifyUpstream 把引擎错误串还原成状态码：4xx 原样透出，其余归 502。
func TestClassifyUpstream(t *testing.T) {
	cases := []struct {
		err    error
		status int
		isUp   bool
	}{
		{fmt.Errorf("python POST /v1/kb returned 400: {\"detail\":\"选择 provider\"}"), 400, true},
		{fmt.Errorf("python POST /v1/kb returned 422: bad"), 422, true},
		{fmt.Errorf("dial tcp: connection refused"), 0, false},
	}
	for _, c := range cases {
		err := classifyUpstream("op", c.err)
		var up *upstreamError
		if c.isUp {
			if !errors.As(err, &up) || up.status != c.status {
				t.Errorf("err=%v 应还原成 %d", c.err, c.status)
			}
			continue
		}
		if errors.As(err, &up) {
			t.Errorf("err=%v 不该被当成上游状态码", c.err)
		}
		if !errors.Is(err, errInstallUpstream) {
			t.Errorf("err=%v 应归为 errInstallUpstream（→502）", c.err)
		}
	}
}

// 保证测试文件用到的 io 不报 unused（route 层断言用得到它时再说）。
var _ = io.Discard
