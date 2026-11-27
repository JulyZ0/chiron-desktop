// Package market 提供「资源市场」的目录模型、内置目录与查询。
//
// 市场把四类可安装资源统一成一种条目形态（agent / skill / knowledge / mcp），
// 让网关用一套分页、筛选、安装流水线覆盖它们 —— 而不是四条几乎相同的
// 端点 + 四套前端分支。
//
// **内置目录随二进制发布**：`catalog/*.json` 用 `//go:embed` 打进可执行文件，
// 装包因此不需要新增 electron-builder 的 extraResources 规则，也不依赖
// 仓库根目录 `market/` 是否被打进镜像。外部源不可达时市场页仍有内容，
// 这是桌面版离线可用的底线。
//
// **条目以「源」限定**：`(Source, ID)` 才是主键。内置源固定为 `builtin`；
// 外部源引入后，同名条目在不同源互不干扰，安装记录也能追到来源。
package market

import (
	"embed"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"
)

// Kind 是市场条目的类型。
type Kind string

const (
	KindAgent     Kind = "agent"
	KindSkill     Kind = "skill"
	KindKnowledge Kind = "knowledge"
	KindMCP       Kind = "mcp"
)

// Kinds 是全部合法类型，顺序即市场页 Tab 的顺序。
var Kinds = []Kind{KindAgent, KindSkill, KindKnowledge, KindMCP}

// SourceBuiltin 是随包发布的内置目录的固定源 id。
//
// 它是关键字而非常规配置值：前端据此判断"这条永远可安装、不需要联网"，
// 安装流水线据此跳过下载与哈希校验。
const SourceBuiltin = "builtin"

// ParseKind 把外部输入转成 Kind，非法值返回错误（不静默归零）。
func ParseKind(s string) (Kind, error) {
	for _, k := range Kinds {
		if string(k) == s {
			return k, nil
		}
	}
	return "", fmt.Errorf("unknown kind %q: must be one of %s", s, strings.Join(kindStrings(), ", "))
}

func kindStrings() []string {
	out := make([]string, 0, len(Kinds))
	for _, k := range Kinds {
		out = append(out, string(k))
	}
	return out
}

// Requires 描述条目安装前可读的前提，前端直接渲染成提示。
//
// **只声明键名，绝不内嵌密钥**：市场条目一旦带凭据进仓库，就等于把密钥
// 写进版本历史。需要凭据的条目在这里列名，安装时由用户在表单里填值。
type Requires struct {
	// Env 是需要用户提供的环境变量键名（如 GITHUB_TOKEN）。
	Env []string `json:"env,omitempty"`
	// Model 表示该条目需要可解析的模型才能运行（agent 类）。
	Model bool `json:"model,omitempty"`
	// Approval 表示装完仍需用户在插件页授权才能真正执行（mcp 类）。
	Approval bool `json:"approval,omitempty"`
	// FS 声明需要访问的文件范围（相对项目根的通配符；空 = 未声明）。
	FS []string `json:"fs,omitempty"`
	// Net 声明需要联网的主机（域名/IP；空 = 未声明）。
	Net []string `json:"net,omitempty"`
	// Shell 声明是否需要启动子进程/执行命令。
	Shell bool `json:"shell,omitempty"`
}

// Item 是市场里的一条资源。
type Item struct {
	Kind        Kind            `json:"kind"`
	ID          string          `json:"id"`
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	Version     string          `json:"version,omitempty"`
	Author      string          `json:"author,omitempty"`
	Tags        []string        `json:"tags,omitempty"`
	Homepage    string          `json:"homepage,omitempty"`
	Payload     json.RawMessage `json:"payload,omitempty"`
	Requires    Requires        `json:"requires,omitempty"`

	// Source 是条目所属的源 id（内置 `builtin` 或外部源）。由加载层填写，
	// 不来自条目 JSON —— 避免条目自称属于别的源。
	Source string `json:"source"`
	// Signed 表示条目是否带发布方签名。内置条目为 false 且前端不显示
	// 「未签名」警示（它与二进制同源发布，签名没有额外信息量）。
	Signed bool `json:"signed"`
	// Stale 表示该条目来自一次失败抓取后保留的上次成功快照。
	Stale bool `json:"stale,omitempty"`

	// ManifestURL 是外部条目的**内容来源**：安装时由网关抓取并校验。
	// 内置条目为空（内容就在 Payload 里）。
	//
	// 为什么外部条目必须有它：目录里的 payload 只是展示信息，用"目录自称的内容"
	// 落库等于信任一个随时可变、且没有完整性校验的数据源。
	ManifestURL string `json:"manifest_url,omitempty"`
	// SHA256 是 manifest 原始字节的哈希（完整性校验；不匹配即拒绝安装）。
	SHA256 string `json:"sha256,omitempty"`
	// Signature 是发布方签名（可选）。为 nil 表示未签名。
	Signature *Signature `json:"signature,omitempty"`
	// MinEngine 是最低引擎版本要求（空表示不限制）。
	MinEngine string `json:"min_engine,omitempty"`
}

// itemDef 是内置目录 JSON 的线上形态：没有 source/signed，这两个由加载层补。
//
// 外部源的条目也解析成这个结构（多出 manifest_url / sha256 / signature /
// min_engine 四个可选字段），使两种来源共享同一套字段校验。
type itemDef struct {
	Kind        Kind            `json:"kind"`
	ID          string          `json:"id"`
	Name        string          `json:"name"`
	Description string          `json:"description,omitempty"`
	Version     string          `json:"version,omitempty"`
	Author      string          `json:"author,omitempty"`
	Tags        []string        `json:"tags,omitempty"`
	Homepage    string          `json:"homepage,omitempty"`
	Payload     json.RawMessage `json:"payload,omitempty"`
	Requires    Requires        `json:"requires,omitempty"`

	ManifestURL string     `json:"manifest_url,omitempty"`
	SHA256      string     `json:"sha256,omitempty"`
	Signature   *Signature `json:"signature,omitempty"`
	MinEngine   string     `json:"min_engine,omitempty"`
}

//go:embed catalog/*.json
var builtinFS embed.FS

// builtinFiles 把类型映射到内置目录文件。文件名与 kind 必须一致：
// 加载时按文件名决定期望的 kind，条目写错类型会被丢弃（见 loadBuiltin）。
var builtinFiles = map[Kind]string{
	KindAgent:     "catalog/agents.json",
	KindSkill:     "catalog/skills.json",
	KindKnowledge: "catalog/knowledge.json",
	KindMCP:       "catalog/mcp.json",
}

// builtinCache 是解析一次后的内置条目（进程内不可变）。
var builtinCache = loadBuiltin()

// loadBuiltin 解析随包发布的目录。
//
// **坏条目只跳过自己**：一条 JSON 写错（缺 id、kind 与文件不符、payload 非法）
// 不应该让整个市场空掉。每条被丢弃的条目都留一条 warning —— 静默丢数据
// 会让「市场里少了一条」变成无从排查的现象。
func loadBuiltin() []Item {
	out := make([]Item, 0, 16)
	for kind, name := range builtinFiles {
		data, err := builtinFS.ReadFile(name)
		if err != nil {
			slog.Warn("market: read builtin catalog failed", "file", name, "error", err)
			continue
		}
		var defs []itemDef
		if err := json.Unmarshal(data, &defs); err != nil {
			slog.Warn("market: parse builtin catalog failed", "file", name, "error", err)
			continue
		}
		for _, d := range defs {
			if msg := validateDef(kind, d); msg != "" {
				slog.Warn("market: skip invalid builtin item",
					"file", name, "id", d.ID, "reason", msg)
				continue
			}
			out = append(out, Item{
				Kind:        d.Kind,
				ID:          d.ID,
				Name:        d.Name,
				Description: d.Description,
				Version:     d.Version,
				Author:      d.Author,
				Tags:        d.Tags,
				Homepage:    d.Homepage,
				Payload:     d.Payload,
				Requires:    d.Requires,
				Source:      SourceBuiltin,
				Signed:      false,
			})
		}
	}
	sortItems(out)
	return out
}

// validateDef 校验一条内置条目，返回空串表示通过。
func validateDef(fileKind Kind, d itemDef) string {
	if d.Kind != fileKind {
		return fmt.Sprintf("kind %q does not match catalog file (%s)", d.Kind, fileKind)
	}
	if strings.TrimSpace(d.ID) == "" {
		return "id is required"
	}
	if !strings.HasPrefix(d.ID, string(d.Kind)+":") {
		return fmt.Sprintf("id %q must be prefixed with %q", d.ID, string(d.Kind)+":")
	}
	if strings.TrimSpace(d.Name) == "" {
		return "name is required"
	}
	if len(d.Payload) == 0 || string(d.Payload) == "null" {
		return "payload is required"
	}
	// payload 必须是 JSON 对象：数组/标量会让安装流水线拿不到字段。
	var probe map[string]any
	if err := json.Unmarshal(d.Payload, &probe); err != nil {
		return "payload must be a JSON object"
	}
	return ""
}

// sortItems 按 kind 顺序、再按名称稳定排序，使分页结果可预期。
func sortItems(items []Item) {
	rank := make(map[Kind]int, len(Kinds))
	for i, k := range Kinds {
		rank[k] = i
	}
	sort.SliceStable(items, func(i, j int) bool {
		if rank[items[i].Kind] != rank[items[j].Kind] {
			return rank[items[i].Kind] < rank[items[j].Kind]
		}
		if items[i].Name != items[j].Name {
			return items[i].Name < items[j].Name
		}
		return items[i].ID < items[j].ID
	})
}

// BuiltinItems 返回内置目录的副本（调用方修改不会污染缓存）。
func BuiltinItems() []Item {
	out := make([]Item, len(builtinCache))
	copy(out, builtinCache)
	return out
}

// Query 是目录查询条件。零值表示不筛选。
type Query struct {
	// Kind 为空表示全部类型。
	Kind Kind
	// Q 对 name / description / id 做不区分大小写的子串匹配。
	Q string
	// Tag 要求条目 tags 里含该值（不区分大小写）。
	Tag string
	// Source 为空表示全部源。
	Source string
}

// Filter 按条件筛出条目。它不修改入参切片。
func Filter(items []Item, q Query) []Item {
	needle := strings.ToLower(strings.TrimSpace(q.Q))
	tag := strings.ToLower(strings.TrimSpace(q.Tag))
	out := make([]Item, 0, len(items))
	for _, it := range items {
		if q.Kind != "" && it.Kind != q.Kind {
			continue
		}
		if q.Source != "" && it.Source != q.Source {
			continue
		}
		if tag != "" && !hasTag(it.Tags, tag) {
			continue
		}
		if needle != "" && !matchesText(it, needle) {
			continue
		}
		out = append(out, it)
	}
	return out
}

func hasTag(tags []string, want string) bool {
	for _, t := range tags {
		if strings.ToLower(t) == want {
			return true
		}
	}
	return false
}

func matchesText(it Item, needle string) bool {
	for _, field := range []string{it.ID, it.Name, it.Description} {
		if strings.Contains(strings.ToLower(field), needle) {
			return true
		}
	}
	return false
}

// Find 在给定条目集合里按 `(source, id)` 查找。
//
// source 为空时会在全部源中查找 —— 调用方要么传 `"builtin"`，要么接受
// 跨源命中的歧义。安装流水线必须传具体 source，避免装错来源的同名条目。
func Find(items []Item, source, id string) (Item, bool) {
	for _, it := range items {
		if it.ID != id {
			continue
		}
		if source != "" && it.Source != source {
			continue
		}
		return it, true
	}
	return Item{}, false
}
