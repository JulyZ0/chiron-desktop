package api

// H75 的 HTTP 层回归：**装完 inert，显式启用才可用，撤销立刻停用**。
//
// 覆盖三类落点的"执行面"字段：
//   - agent：agents.enabled（安装写 false）；
//   - mcp：plugins.json 的 status（安装写 inactive）与 approved（启用前必须已由用户授权）；
//   - 记录：market_installations.enabled 与落点同步翻转。
//
// 这些字段决定引擎会不会注入/执行/连接，所以断言必须落在**落点文件**上，
// 而不是只看接口返回值。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/market"
)

// okVerifier 是"校验一定通过"的信任链替身（H5 的行为由 market_trust_api_test.go 覆盖）。
type okVerifier struct{}

func (okVerifier) Verify(_ context.Context, item market.Item) (market.Item, market.Verification, error) {
	return item, market.Verification{Verified: true, ManifestSHA256: "deadbeef", KeyID: "test-key"}, nil
}

// enableEnv 是启用/停用链路的最小环境：真实 PluginHandler（写临时目录）+ 进程内安装记录。
type enableEnv struct {
	h       *MarketInstallHandler
	store   *memoryInstallStore
	plugins *PluginHandler
	dir     string
}

func newEnableEnv(t *testing.T) *enableEnv {
	t.Helper()
	dir := t.TempDir()
	cfg := &config.Config{PluginDataDir: dir}
	plugins := &PluginHandler{cfg: cfg, dataDir: dir}
	store := newMemoryInstallStore()
	installers := map[market.Kind]market.Installer{
		market.KindMCP:       mcpMarketInstaller{plugins: plugins},
		market.KindKnowledge: knowledgeMarketInstaller{py: nil},
	}
	opts := market.InstallOptions{
		AllowRemoteMCP: true,
		Verifier:       okVerifier{},
		EnableCheck:    newPluginEnableCheck(plugins, cfg),
	}
	return &enableEnv{
		h: &MarketInstallHandler{
			svc:     market.NewService(nil),
			install: market.NewInstallService(store, installers, opts),
			plugins: plugins,
		},
		store:   store,
		plugins: plugins,
		dir:     dir,
	}
}

// externalMCPItem 是外部源的一条 mcp 条目（装完应被 quarantine）。
func externalMCPItem() market.Item {
	return market.Item{
		Kind: market.KindMCP, ID: "mcp:remote-fs", Name: "remote-fs", Source: "team",
		ManifestURL: "https://market.test/mcp/remote-fs.json",
		SHA256:      "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
		Payload:     []byte(`{"command":"npx","args":["-y","remote-fs-mcp"],"read_only":true}`),
	}
}

// readPlugins 读回该用户的 plugins.json（落点事实）。
func (e *enableEnv) readPlugins(t *testing.T) pluginsFile {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(e.dir, "u1", "plugins.json"))
	if err != nil {
		t.Fatalf("read plugins.json: %v", err)
	}
	var pf pluginsFile
	if err := json.Unmarshal(raw, &pf); err != nil {
		t.Fatalf("decode plugins.json: %v", err)
	}
	return pf
}

// writePlugins 直接改落点文件（模拟用户在插件页的授权操作）。
func (e *enableEnv) writePlugins(t *testing.T, pf pluginsFile) {
	t.Helper()
	if err := e.plugins.savePluginsFile("u1", pf); err != nil {
		t.Fatalf("save plugins.json: %v", err)
	}
}

// doToggle 发一次 enable/disable 请求。
func (e *enableEnv) doToggle(t *testing.T, source, kind, id string, enable bool) *httptest.ResponseRecorder {
	t.Helper()
	action := "disable"
	if enable {
		action = "enable"
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/market/install/"+source+"/"+kind+"/"+id+"/"+action, nil)
	req.SetPathValue("source", source)
	req.SetPathValue("kind", kind)
	req.SetPathValue("id", id)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()
	if enable {
		e.h.Enable(rec, req)
	} else {
		e.h.Disable(rec, req)
	}
	return rec
}

// installExternalMCP 走编排层安装那条外部 mcp 条目（返回安装结果）。
func (e *enableEnv) installExternalMCP(t *testing.T) market.Result {
	t.Helper()
	ctx := auth.WithClaims(context.Background(), &auth.Claims{UserID: "u1", TenantID: 1})
	res, err := e.h.install.Install(ctx, "1", "u1", externalMCPItem(), nil)
	if err != nil {
		t.Fatalf("安装外部 mcp 失败: %v", err)
	}
	return res
}

// 市场装出来的 agent 行必须是 **enabled=false**：装完不进注入链，等显式启用。
func TestAgentInstallArgs_IsInertByDefault(t *testing.T) {
	claims := &auth.Claims{UserID: "u1", TenantID: 1}
	args := agentInstallArgs(claims, "agent-1", market.Item{Name: "a", Source: "team"}, agentPayload{})
	enabled, ok := args[len(args)-1].(bool)
	if !ok {
		t.Fatalf("最后一个参数应为 enabled 布尔值，got %T", args[len(args)-1])
	}
	if enabled {
		t.Error("市场安装的 agent 必须 enabled=false（H75：装完 inert）")
	}
}

// 外部 mcp 安装后：记录 enabled=false，插件 status=inactive 且未授权（不连接）。
func TestMCPInstall_IsQuarantined(t *testing.T) {
	env := newEnableEnv(t)
	res := env.installExternalMCP(t)

	if res.Enabled {
		t.Error("外部 mcp 安装记录必须 enabled=false")
	}
	pf := env.readPlugins(t)
	if len(pf.MCPServers) != 1 {
		t.Fatalf("plugins = %d，want 1", len(pf.MCPServers))
	}
	entry := pf.MCPServers[0]
	if entry.Status != pluginStatusInactive {
		t.Errorf("status = %q，want %q（引擎只连 active，装完不许连）", entry.Status, pluginStatusInactive)
	}
	if entry.Approved {
		t.Error("装完不得自带授权")
	}
}

// 未授权就启用 → 403，且插件仍是 inactive（拒绝发生在落点动作之前）。
func TestEnableMCP_RequiresPluginApproval(t *testing.T) {
	env := newEnableEnv(t)
	env.installExternalMCP(t)

	rec := env.doToggle(t, "team", "mcp", "mcp:remote-fs", true)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d，want 403; body=%s", rec.Code, rec.Body.String())
	}
	if entry := env.readPlugins(t).MCPServers[0]; entry.Status != pluginStatusInactive {
		t.Errorf("被拒绝后 status = %q，必须仍是 inactive", entry.Status)
	}
	recs, err := env.store.List(context.Background(), "1", "u1", "")
	if err != nil {
		t.Fatal(err)
	}
	if len(recs) != 1 || recs[0].Enabled {
		t.Errorf("被拒绝后记录必须仍 disabled: %+v", recs)
	}
}

// 用户级总开关未开也不能启用（两道闸都要过）。
func TestEnableMCP_RequiresCommandsEnabled(t *testing.T) {
	env := newEnableEnv(t)
	env.installExternalMCP(t)

	pf := env.readPlugins(t)
	pf.MCPServers[0].Approved = true // 逐条授权已给
	pf.CommandsEnabled = false       // 但总开关没开
	env.writePlugins(t, pf)

	if rec := env.doToggle(t, "team", "mcp", "mcp:remote-fs", true); rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d，want 403; body=%s", rec.Code, rec.Body.String())
	}
}

// 授权齐备后启用 → 200，插件 status=active（可连接），记录翻成 enabled。
func TestEnableMCP_AfterApprovalBecomesActive(t *testing.T) {
	env := newEnableEnv(t)
	env.installExternalMCP(t)

	pf := env.readPlugins(t)
	pf.MCPServers[0].Approved = true
	pf.CommandsEnabled = true
	env.writePlugins(t, pf)

	rec := env.doToggle(t, "team", "mcp", "mcp:remote-fs", true)
	var got installResponse
	decodeInstallData(t, rec, &got)
	if !got.Enabled {
		t.Error("启用后响应必须 enabled=true")
	}
	if entry := env.readPlugins(t).MCPServers[0]; entry.Status != pluginStatusActive {
		t.Errorf("启用后 status = %q，want %q", entry.Status, pluginStatusActive)
	}
	if entry := env.readPlugins(t).MCPServers[0]; !entry.Approved {
		t.Error("启用不该撤销用户已有的逐条授权")
	}
}

// 撤销（disable）**立刻停用**：status=inactive 且旧授权作废。
func TestDisableMCP_RevokesImmediately(t *testing.T) {
	env := newEnableEnv(t)
	env.installExternalMCP(t)

	pf := env.readPlugins(t)
	pf.MCPServers[0].Approved = true
	pf.CommandsEnabled = true
	pf.MCPServers[0].Status = pluginStatusActive
	env.writePlugins(t, pf)
	// 记录侧同步成已启用（模拟此前启用过）。
	recs, err := env.store.List(context.Background(), "1", "u1", "")
	if err != nil || len(recs) != 1 {
		t.Fatalf("list installs: %v %d", err, len(recs))
	}
	recs[0].Enabled = true
	if err := env.store.Upsert(context.Background(), "1", "u1", recs[0]); err != nil {
		t.Fatal(err)
	}

	response := env.doToggle(t, "team", "mcp", "mcp:remote-fs", false)
	var got installResponse
	decodeInstallData(t, response, &got)
	if got.Enabled {
		t.Error("停用后响应必须 enabled=false")
	}
	entry := env.readPlugins(t).MCPServers[0]
	if entry.Status != pluginStatusInactive {
		t.Errorf("停用后 status = %q，want %q（立刻不可连接）", entry.Status, pluginStatusInactive)
	}
	if entry.Approved {
		t.Error("停用必须同时撤回旧授权（否则下次打开总开关就能跑）")
	}
}

// 没装过的条目启用 → 404（不是 200 的假成功）。
func TestEnableMarketItem_NotInstalledIs404(t *testing.T) {
	env := newEnableEnv(t)
	if rec := env.doToggle(t, "team", "mcp", "mcp:nope", true); rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d，want 404; body=%s", rec.Code, rec.Body.String())
	}
}

// kind 非法 → 400（与安装/卸载同一口径）。
func TestEnableMarketItem_InvalidKindIs400(t *testing.T) {
	env := newEnableEnv(t)
	if rec := env.doToggle(t, "team", "widget", "mcp:x", true); rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d，want 400", rec.Code)
	}
}
