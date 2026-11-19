package api

import (
	"errors"
	"strings"
	"testing"

	"github.com/athenavi/chiron/config"
)

// testPluginConfig 返回一个**隔离**的插件配置（PluginDataDir → t.TempDir()）。
//
// 必须隔离：checkOperatorAllowlist 现在会读 `{PluginDataDir}/command_allowlist.json`，
// 不隔离的话开发机/CI 上已存在的真实白名单会让断言随环境飘（本机放行的命令在 CI 上
// 可能不存在，反之亦然）。
func testPluginConfig(t *testing.T) *config.Config {
	t.Helper()
	return &config.Config{PluginDataDir: t.TempDir()}
}

// 插件命令的校验分两层：
//   - **形态**（validatePluginCommandShape）：始终生效；
//   - **放行名单**（checkOperatorAllowlist，env `PLUGIN_COMMAND_ALLOWLIST` ∪ 后台文件）：
//     **可选上界** —— 两侧都没配即不限制，任一非空则命令 basename 必须命中。
//
// 创建/更新走这两层的组合（下面 checkPluginCreateAllowed 模拟）；真正**执行**另加
// 用户级总开关 + 每插件一次性授权，见 checkPluginExecutionAllowed。
//
// 背景（2026-10-08）：此前只比 basename，于是 `../../tmp/npx`、`..\..\tmp\npx`（相对穿越）
// 与 `\\evil-host\share\npx`（UNC）都会命中白名单 —— 宿主机会执行**攻击者放置或远程共享**上的
// 同名二进制。Python 侧同一条规则见 `python-engine/app/tools/ssrf.py` 的 `_command_path_ok`。
func checkPluginCreateAllowed(command string, cfg *config.Config) error {
	if err := validatePluginCommandShape(command); err != nil {
		return err
	}
	return checkOperatorAllowlist(command, cfg)
}

func TestPluginCommandPathRule(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")
	cfg := testPluginConfig(t)

	cases := []struct {
		name    string
		command string
		wantErr string // 空串 = 应放行
	}{
		{"裸 basename", "npx", ""},
		{"POSIX 绝对路径", "/usr/local/bin/npx", ""},
		{"Windows 绝对路径", `C:\tools\npx`, ""},
		{"相对穿越", "../../tmp/npx", "absolute path"},
		{"Windows 穿越", `..\..\tmp\npx`, "absolute path"},
		{"UNC 共享", `\\evil-host\share\npx`, "absolute path"},
		{"相对路径带分隔符", "./npx", "absolute path"},
		{"非白名单 basename", "/usr/bin/bash", "not allowed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			err := checkPluginCreateAllowed(tc.command, cfg)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("%q 应放行，得到错误：%v", tc.command, err)
				}
				return
			}
			if err == nil {
				t.Fatalf("%q 应被拒绝（%s），却放行了", tc.command, tc.wantErr)
			}
			if !strings.Contains(err.Error(), tc.wantErr) {
				t.Fatalf("%q 的拒绝理由应含 %q，得到：%v", tc.command, tc.wantErr, err)
			}
		})
	}
}

// 两侧都没配置 = 可选上界不生效：**不**限制命令（授权交给用户级闸），
// 但命令形态仍须合法。
func TestOperatorAllowlistUnsetDoesNotRestrict(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	cfg := testPluginConfig(t)
	if err := checkOperatorAllowlist("npx", cfg); err != nil {
		t.Fatalf("两侧都未配置时不应限制命令，得到：%v", err)
	}
	if err := checkPluginCreateAllowed("npx", cfg); err != nil {
		t.Fatalf("未配置白名单时合法形态应放行，得到：%v", err)
	}
	// 形态非法仍拒（与白名单无关）。
	if err := checkPluginCreateAllowed("../npx", cfg); err == nil {
		t.Fatal("相对路径必须仍被拒绝（形态校验）")
	}
}

// 执行闸是 deny-by-default 的四层。这里覆盖用户级两层 + 放行名单。
func TestPluginExecutionGate(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	cfg := testPluginConfig(t)
	ready := MCPPlugin{Name: "p", Command: "npx", Approved: true}

	if err := checkPluginExecutionAllowed(ready, true, cfg); err != nil {
		t.Fatalf("总开关开启 + 已授权应放行，得到：%v", err)
	}
	if err := checkPluginExecutionAllowed(ready, false, cfg); err == nil {
		t.Fatal("用户总开关未开启时必须拒绝（deny-by-default）")
	}
	notApproved := ready
	notApproved.Approved = false
	if err := checkPluginExecutionAllowed(notApproved, true, cfg); err == nil {
		t.Fatal("插件未一次性授权时必须拒绝")
	}

	// 放行名单：配置后，名单之外的命令即使总开关/授权都具备也要拒。
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "uvx")
	if err := checkPluginExecutionAllowed(ready, true, cfg); err == nil {
		t.Fatal("命令不在放行名单内时必须拒绝")
	}
}

// ── 后台可编辑白名单（2026-11-18）─────────────────────────────────
//
// 背景：装完市场插件、点「测试连接」却报 command not allowed 时，用户只能去改
// 部署 env 并重启网关 + 引擎。现在后台把 basename 写进
// `{PLUGIN_DATA_DIR}/command_allowlist.json`，网关与引擎读同一份，改动即时生效。

// 单独靠后台文件就能放行命令（env 可以为空）—— 这是"后台可随时加白"的最小闭环。
func TestPluginAllowlistFileEnablesCommand(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "")
	cfg := testPluginConfig(t)

	if _, err := savePluginAllowlistFile(cfg, []string{"npx"}); err != nil {
		t.Fatalf("写入后台白名单失败：%v", err)
	}
	if err := checkOperatorAllowlist("npx", cfg); err != nil {
		t.Fatalf("后台加了白名单后应放行 npx，得到：%v", err)
	}
	// 未加白的命令仍被拒。
	if err := checkOperatorAllowlist("curl", cfg); err == nil {
		t.Fatal("不在后台白名单里的命令必须仍被拒绝")
	}
}

// env 与后台文件是**并集**：任一侧放行的命令都能用，后台加白不会把 env 里的挤掉。
func TestPluginAllowlistEnvAndFileAreMerged(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "uvx")
	cfg := testPluginConfig(t)
	if _, err := savePluginAllowlistFile(cfg, []string{"npx"}); err != nil {
		t.Fatalf("写入后台白名单失败：%v", err)
	}

	for _, cmd := range []string{"uvx", "npx"} {
		if err := checkOperatorAllowlist(cmd, cfg); err != nil {
			t.Fatalf("%s 应被 env ∪ 文件放行，得到：%v", cmd, err)
		}
	}
	got := effectivePluginCommandList(cfg)
	if strings.Join(got, ",") != "npx,uvx" {
		t.Fatalf("并集回填应有序且去重，得到 %v", got)
	}
}

// 被名单挡下时错误必须**结构化**地带出命令名 —— 前端要靠它弹「加入白名单」。
func TestPluginAllowlistErrorCarriesCommand(t *testing.T) {
	t.Setenv("PLUGIN_COMMAND_ALLOWLIST", "uvx")
	cfg := testPluginConfig(t)

	err := checkOperatorAllowlist("npx", cfg)
	var allowErr *pluginAllowlistError
	if !errors.As(err, &allowErr) {
		t.Fatalf("名单拒绝必须是 *pluginAllowlistError，得到 %T: %v", err, err)
	}
	if allowErr.Command != "npx" {
		t.Fatalf("错误里应带命令名 npx，得到 %q", allowErr.Command)
	}
	if !strings.Contains(err.Error(), "allowlist") {
		t.Fatalf("文案应指向白名单，得到：%v", err)
	}
}

// 后台只能加**裸可执行名**：带路径分隔符的条目等于开了一条任意路径放行通道。
func TestSavePluginAllowlistRejectsPaths(t *testing.T) {
	cfg := testPluginConfig(t)
	for _, bad := range []string{"../npx", "/usr/bin/npx", `C:\tools\npx`, "   ", "np\nx"} {
		if _, err := savePluginAllowlistFile(cfg, []string{bad}); err == nil {
			t.Fatalf("%q 应被拒绝，却写进了白名单", bad)
		}
	}
}

// 空白/重复条目要归一化：写进去什么、读回来就是去重排序后的样子。
func TestSavePluginAllowlistNormalizes(t *testing.T) {
	cfg := testPluginConfig(t)
	got, err := savePluginAllowlistFile(cfg, []string{" npx ", "npx", "uvx"})
	if err != nil {
		t.Fatalf("写入失败：%v", err)
	}
	if strings.Join(got, ",") != "npx,uvx" {
		t.Fatalf("应去重排序为 npx,uvx，得到 %v", got)
	}
	if again := loadPluginAllowlistFile(cfg); strings.Join(again, ",") != "npx,uvx" {
		t.Fatalf("落盘后读回应一致，得到 %v", again)
	}
}
