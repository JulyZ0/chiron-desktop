package api

// 插件命令白名单的**后台可编辑**部分。
//
// 背景：env 的 `PLUGIN_COMMAND_ALLOWLIST` 只在进程启动时读一次，改它必须重启网关
// **和**引擎；而"市场装了插件、点测试连接却被告知命令未放行"需要当场解决。
// 于是后台把放行的 basename 写进 `{PLUGIN_DATA_DIR}/command_allowlist.json`，
// 网关与引擎读**同一份文件**（引擎本就与网关共享 PLUGIN_DATA_DIR，见
// python-engine/app/plugins/store.py），不需要新通道、也不引入第二份事实源。
//
// 生效口径（两侧一致）：**env ∪ 文件**。
//   - 两者都为空 ⇒ 网关侧视为"运维未设上界"（不额外限制），引擎侧仍 fail-closed
//     拒绝非内置插件 —— 这条差异是**有意的**：网关只做校验/测试，真正拉起进程的是
//     引擎，安全默认必须落在执行方。
//   - 任一非空 ⇒ 命令 basename 必须命中，否则拒绝执行并提示去后台加白。

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/athenavi/chiron/config"
)

// pluginAllowlistFileName 与引擎侧 app/tools/ssrf.py 的默认文件名**必须一致**。
const pluginAllowlistFileName = "command_allowlist.json"

// pluginAllowlistDoc 是落盘结构。用对象而非裸数组，方便日后加字段而不破坏兼容。
type pluginAllowlistDoc struct {
	Commands []string `json:"commands"`
}

// pluginAllowlistPath 返回白名单文件路径（与 plugins.json 同一父目录）。
func pluginAllowlistPath(cfg *config.Config) string {
	dir := ""
	if cfg != nil {
		dir = strings.TrimSpace(cfg.PluginDataDir)
	}
	if dir == "" {
		dir = filepath.Join(".", "data", "plugins")
	}
	return filepath.Join(dir, pluginAllowlistFileName)
}

// normalizePluginCommandName 校验并归一化一条白名单条目。
//
// 只接受**裸 basename**：白名单管的是"哪个名字能跑"，带分隔符的条目会与
// `pluginCommandPathAllowed` 的"从哪跑"约束混淆，也容易让后台输入框变成一条
// 隐蔽的任意路径放行通道。
func normalizePluginCommandName(raw string) (string, error) {
	name := strings.TrimSpace(raw)
	if name == "" {
		return "", errors.New("command name is empty")
	}
	if strings.ContainsAny(name, `/\`) || name == "." || name == ".." {
		return "", fmt.Errorf("command %q must be a bare executable name (no path separators)", raw)
	}
	if strings.ContainsAny(name, "\x00\r\n") {
		return "", fmt.Errorf("command %q contains control characters", raw)
	}
	return name, nil
}

// loadPluginAllowlistFile 读后台配置的白名单条目（不含 env）。
// 文件不存在/损坏时返回空切片：上游按"未配置"处理，不阻断任何请求。
func loadPluginAllowlistFile(cfg *config.Config) []string {
	data, err := os.ReadFile(pluginAllowlistPath(cfg))
	if err != nil {
		return nil
	}
	var doc pluginAllowlistDoc
	if err := json.Unmarshal(data, &doc); err != nil {
		return nil
	}
	out := make([]string, 0, len(doc.Commands))
	for _, c := range doc.Commands {
		if name, err := normalizePluginCommandName(c); err == nil {
			out = append(out, name)
		}
	}
	return dedupeSortCommands(out)
}

// savePluginAllowlistFile 覆盖写入后台白名单（原子替换，避免读到半截 JSON）。
func savePluginAllowlistFile(cfg *config.Config, commands []string) ([]string, error) {
	normalized := make([]string, 0, len(commands))
	for _, c := range commands {
		name, err := normalizePluginCommandName(c)
		if err != nil {
			return nil, err
		}
		normalized = append(normalized, name)
	}
	normalized = dedupeSortCommands(normalized)

	path := pluginAllowlistPath(cfg)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return nil, err
	}
	data, err := json.MarshalIndent(pluginAllowlistDoc{Commands: normalized}, "", "  ")
	if err != nil {
		return nil, err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o644); err != nil {
		return nil, err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return nil, err
	}
	return normalized, nil
}

// effectivePluginCommands 返回**生效中**的白名单 = env ∪ 后台文件。
//
// 返回 nil 表示"两侧都没配"：网关侧据此按"运维未设上界"放行（真正的执行闸在引擎侧，
// 那里 fail-closed）。调用方不得把 nil 与"空集合"混同 —— 空集合会拒绝一切命令。
func effectivePluginCommands(cfg *config.Config) map[string]bool {
	allowed := allowedPluginCommands() // env，未配置返回 nil
	file := loadPluginAllowlistFile(cfg)
	if allowed == nil && len(file) == 0 {
		return nil
	}
	if allowed == nil {
		allowed = make(map[string]bool, len(file))
	}
	for _, name := range file {
		allowed[name] = true
	}
	return allowed
}

// effectivePluginCommandList 返回并集的有序列表（供后台页面回填）。
func effectivePluginCommandList(cfg *config.Config) []string {
	set := effectivePluginCommands(cfg)
	if set == nil {
		return []string{}
	}
	out := make([]string, 0, len(set))
	for name := range set {
		out = append(out, name)
	}
	return dedupeSortCommands(out)
}

func dedupeSortCommands(in []string) []string {
	seen := make(map[string]bool, len(in))
	out := make([]string, 0, len(in))
	for _, c := range in {
		if c == "" || seen[c] {
			continue
		}
		seen[c] = true
		out = append(out, c)
	}
	sort.Strings(out)
	return out
}
