package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// ── H24：插件授权必须绑定"具体执行参数"，且执行路径都要校验 ────────────────────
//
// 缺陷形态：
//   · Go：只有 `command` 变化会重置 `approved` —— 改 `args`/`env`/`read_only` 不需要重新授权；
//   · Python：`active_servers()` 只看 `status == "active"`，"用户没同意过"这条闸门
//     在引擎侧根本不存在（照样起进程、连网、注册工具）。

func pluginFixture(over func(p *MCPPlugin)) MCPPlugin {
	p := MCPPlugin{
		Name:    "demo",
		Command: "node",
		Args:    []string{"server.js", "--read-only"},
		Env:     map[string]string{"TOKEN": "a", "ENDPOINT": "https://x"},
		Status:  "active",
	}
	if over != nil {
		over(&p)
	}
	return p
}

func approvedFixture(t *testing.T) MCPPlugin {
	t.Helper()
	p := pluginFixture(nil)
	p.Approved = true
	p.ApprovalDigest = pluginApprovalDigest(p)
	return p
}

func TestPluginApprovedForAcceptsBoundApproval(t *testing.T) {
	if !pluginApprovedFor(approvedFixture(t)) {
		t.Fatal("已绑定同一配置的授权应通过")
	}
}

func TestPluginApprovedForRejectsLegacyApprovalWithoutDigest(t *testing.T) {
	p := pluginFixture(nil)
	p.Approved = true // 老数据：有 approved、没有 digest
	if pluginApprovedFor(p) {
		t.Fatal("没有摘要的授权判为未授权（不知道当初批准了什么）")
	}
}

func TestPluginApprovedForRejectsEveryExecutionInputChange(t *testing.T) {
	base := approvedFixture(t)

	cases := []struct {
		name   string
		mutate func(p *MCPPlugin)
	}{
		{"换命令", func(p *MCPPlugin) { p.Command = "bash" }},
		{"换参数", func(p *MCPPlugin) { p.Args = []string{"server.js", "--exec"} }},
		{"加参数", func(p *MCPPlugin) { p.Args = append(p.Args, "--danger") }},
		{"换环境变量", func(p *MCPPlugin) { p.Env = map[string]string{"TOKEN": "b"} }},
		{"翻转 read_only", func(p *MCPPlugin) { p.ReadOnly = true }},
	}
	for _, c := range cases {
		p := base
		c.mutate(&p)
		if pluginApprovedFor(p) {
			t.Fatalf("%s 之后旧授权仍被认作有效", c.name)
		}
	}
}

func TestPluginApprovalIgnoresCosmeticFields(t *testing.T) {
	base := approvedFixture(t)
	p := base
	p.Description = "改个说明"
	p.Version = "9.9.9"
	p.Status = "active"
	if !pluginApprovedFor(p) {
		t.Fatal("改描述/版本不该逼用户重新授权")
	}
}

func TestPluginApprovalDigestIsStable(t *testing.T) {
	a := pluginApprovalDigest(pluginFixture(nil))
	b := pluginApprovalDigest(pluginFixture(nil))
	if a != b {
		t.Fatalf("同一配置两次摘要不同：%s vs %s", a, b)
	}
	if len(a) != 32 {
		t.Fatalf("摘要长度应为 32 hex，得到 %d", len(a))
	}
}

// ── Python 侧：未授权时不得把配置交出去 ───────────────────────────────────
func TestPythonStoreRequiresBothSwitches(t *testing.T) {
	src := readRepoFile(t, "python-engine/app/plugins/store.py")
	for _, marker := range []string{
		"def commands_enabled",
		"if not self.commands_enabled(user_id):",
		"if not s.approved:",
	} {
		if !strings.Contains(src, marker) {
			t.Fatalf("store.py 缺少判据：%s", marker)
		}
	}
	if strings.Contains(src, `return [s for s in self.load(user_id) if s.status == "active"]`) {
		t.Fatal("active_servers 不得只看 status —— 那正是「未授权也能执行」的来源")
	}
}

// ── 授权时写摘要、撤销时清空 ───────────────────────────────────────────────
func TestApproveEndpointBindsAndClearsDigest(t *testing.T) {
	src := readRepoFile(t, "internal/api/plugin_handler.go")
	if !strings.Contains(src, "pf.MCPServers[i].ApprovalDigest = pluginApprovalDigest(") {
		t.Fatal("授权时必须写入绑定摘要")
	}
	if !strings.Contains(src, "pluginApprovedFor(*plugin)") {
		t.Fatal("/test 必须按摘要核对授权")
	}
}

func TestPluginUpdateResetsApprovalForArgsEnvReadOnly(t *testing.T) {
	src := readRepoFile(t, "internal/api/plugin_handler.go")
	if !strings.Contains(src, "approvalInputChanged") {
		t.Fatal("更新路径必须比较\"执行输入\"是否变化")
	}
	if !strings.Contains(src, "plugins[i].Approved = false") {
		t.Fatal("执行输入变化后必须重置授权")
	}
}

// readRepoFile 读仓库内文件（相对 repo root）。
func readRepoFile(t *testing.T, rel string) string {
	t.Helper()
	raw, err := readFileFromRepoRoot(rel)
	if err != nil {
		t.Fatalf("读取 %s: %v", rel, err)
	}
	return raw
}

// 用一次假请求确认 Forbidden 的形态（不依赖 PG/插件文件）。
func TestForbiddenShapeForUnapprovedPlugin(t *testing.T) {
	rec := httptest.NewRecorder()
	Forbidden(rec, "plugin is not approved for its current configuration")
	if rec.Code != http.StatusForbidden {
		t.Fatalf("want 403, got %d", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是 JSON: %v", err)
	}
	if body["success"] != false {
		t.Fatalf("失败响应应带 success=false：%v", body)
	}
}
