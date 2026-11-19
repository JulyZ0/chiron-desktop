package api

import (
	"encoding/json"
	"net/http"
	"strings"
)

// admin_plugin_allowlist.go —— 后台「插件命令白名单」的读写端点。
//
//	GET /v1/admin/plugin-allowlist   读**生效中**的名单（env ∪ 后台文件）
//	PUT /v1/admin/plugin-allowlist   覆盖写后台文件（改动即时生效，无需重启）
//
// ## 为什么需要这两个端点
//
// `PLUGIN_COMMAND_ALLOWLIST` 是部署期环境变量，进程只读一次。用户装完市场插件、
// 点「测试连接」被拒时，唯一的出路是改 env 并**同时重启网关与引擎** —— 这在
// 真实使用中几乎等于"这个插件永远用不了"。白名单本身是"哪个命令能跑"的运维上界，
// 不是部署拓扑，完全可以随时改。
//
// ## 两侧一致
//
// 落盘位置 `{PLUGIN_DATA_DIR}/command_allowlist.json`，引擎读同一份文件
// （python-engine/app/tools/ssrf.py 的 plugin_allowlist_file_entries）。
// 写端点只改**文件**部分：env 里的条目属于部署配置，页面只能展示（标记 from_env），
// 不能通过 API 删掉 —— 否则会出现"页面显示已移除、实际仍然放行"的错觉。
//
// ## 权限
//
// 白名单直接决定**宿主机可以执行哪些命令**，是平台级安全边界：
// 读用 platformReadMW、写用 platformWriteMW（见 routes_admin.go 的口径）。

// AdminPluginAllowlist 是白名单端点的响应体。
type AdminPluginAllowlist struct {
	// Commands 是**生效中**的全部命令（env ∪ 文件，去重排序）。
	Commands []string `json:"commands"`
	// FileCommands 是后台文件里的那部分（页面编辑框回填它，不是并集）。
	FileCommands []string `json:"file_commands"`
	// EnvCommands 来自部署环境变量。页面须把它们显示为只读 —— 改它们要改部署配置。
	EnvCommands []string `json:"env_commands"`
	// CommandsEnabled 回显"是否配置了任何来源"：两侧都空 = 网关不设上界。
	Configured bool `json:"configured"`
}

// GetPluginAllowlist 返回生效中的插件命令白名单（GET /v1/admin/plugin-allowlist）。
func (h *AdminHandler) GetPluginAllowlist(w http.ResponseWriter, r *http.Request) {
	envList := envPluginCommandList()
	fileList := loadPluginAllowlistFile(h.cfg)
	if fileList == nil {
		fileList = []string{}
	}
	OK(w, AdminPluginAllowlist{
		Commands:     effectivePluginCommandList(h.cfg),
		FileCommands: fileList,
		EnvCommands:  envList,
		Configured:   len(envList) > 0 || len(fileList) > 0,
	})
}

// UpdatePluginAllowlist 覆盖写后台白名单（PUT /v1/admin/plugin-allowlist）。
//
// body：`{"commands": ["npx", "uvx"]}` —— 语义是**整体替换文件内容**（不是增量）。
// 用整体替换而不是 add/remove 两个动作：页面就是一份可编辑清单，增量接口在并发编辑
// 下还要额外定义合并语义，收益不明显。
//
// 条目只接受**裸可执行名**（见 normalizePluginCommandName）：带路径分隔符的条目
// 会把"哪个名字能跑"与"从哪跑"混为一谈，也容易让这个接口变成任意路径放行通道。
func (h *AdminHandler) UpdatePluginAllowlist(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Commands []string `json:"commands"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&body); err != nil {
		BadRequest(w, "invalid json body")
		return
	}

	saved, err := savePluginAllowlistFile(h.cfg, body.Commands)
	if err != nil {
		// 条目非法是用户输入问题，不是服务端故障 —— 用 400 让前端把原因显示出来。
		BadRequest(w, "invalid allowlist entry: "+err.Error())
		return
	}
	if saved == nil {
		saved = []string{}
	}

	envList := envPluginCommandList()
	merged := effectivePluginCommandList(h.cfg)
	OK(w, AdminPluginAllowlist{
		Commands:     merged,
		FileCommands: saved,
		EnvCommands:  envList,
		Configured:   len(envList) > 0 || len(saved) > 0,
	})
}

// envPluginCommandList 返回**仅**来自部署环境变量的条目（有序）。
//
// 与 allowedPluginCommands 的区别：后者是给判定用的集合（未配置返回 nil），
// 这里要的是"页面能展示的清单"，未配置时返回空切片而不是 nil。
func envPluginCommandList() []string {
	set := allowedPluginCommands()
	if set == nil {
		return []string{}
	}
	out := make([]string, 0, len(set))
	for name := range set {
		if strings.TrimSpace(name) == "" {
			continue
		}
		out = append(out, name)
	}
	return dedupeSortCommands(out)
}
