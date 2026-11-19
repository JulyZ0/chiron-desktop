package api

// 本文件由 agents.go 按职责外移而来（Agent 配置解析与上下文装配，全为纯逻辑）。
// 代码逐字未改：只搬位置，不改逻辑。

import (
	"context"
	"encoding/json"
	"log/slog"
	"strconv"
	"strings"
)

func trimSpace(s string) string { return strings.TrimSpace(s) }
func itoa(n int) string         { return strconv.Itoa(n) }

// jsonOrEmptyArray 把可能为空的 JSON 写进 json 列：空值写 '[]'，
// 因为 json/jsonb 都不接受空字符串（""::json 会直接报错）。
func jsonOrEmptyArray(raw json.RawMessage) string {
	if len(raw) == 0 || string(raw) == "null" {
		return "[]"
	}
	return string(raw)
}

// agentSkillNames 解析 Agent 绑定的技能名（json 字符串数组）；
// 未绑定或格式不对时返回 nil，等价于"不筛选技能"（引擎侧沿用全部已安装技能）。
func agentSkillNames(skills json.RawMessage) []string {
	if len(skills) == 0 {
		return nil
	}
	var names []string
	if err := json.Unmarshal(skills, &names); err != nil {
		return nil
	}
	return names
}

// agentContextPayload 把 Agent 组装成透传给引擎的 context.agent。
//
// 字段名必须与 python 侧读取处一致（app/api/unified_executor.py 的 _execute_via_agent、
// app/main.py 的 workbench_context 消费）。此前只带 system_prompt / max_turns / model
// —— 也就是只带人格不带能力：Agent 自带的工具、知识库、技能全部丢失，
// 用户选了 Agent 却发现它"不会用自己的工具"。
//
// sessionPrompt 是**会话级附加提示**（`context.system_prompt`，可选）。Agent 的人格
// prompt 是基础，会话级要求拼在后面 —— 见 composeAgentSystemPrompt 的取舍。
func agentContextPayload(a *Agent, sessionPrompt string) map[string]any {
	if a == nil {
		return nil
	}
	payload := map[string]any{}
	if a.ID != "" {
		payload["id"] = a.ID
	}
	if a.Name != "" {
		payload["name"] = a.Name
	}
	if prompt := composeAgentSystemPrompt(a.SystemPrompt, sessionPrompt); prompt != "" {
		payload["system_prompt"] = prompt
	}
	if a.MaxTurns > 0 {
		payload["max_turns"] = a.MaxTurns
	}
	if model := agentModel(a.LLMConfig); model != "" {
		payload["model"] = model
	}
	// temperature 只在 Agent 里真的配了（>0）时才下发：缺省/0 表示"不覆盖"，
	// 不能把 0 当成"温度必须为 0"—— 那会把用户的采样设置悄悄改掉。
	if temp := agentTemperature(a.LLMConfig); temp > 0 {
		payload["temperature"] = temp
	}
	if tools := decodeJSONArray(a.Tools); len(tools) > 0 {
		payload["tools"] = tools
	}
	if a.KbID != "" {
		payload["kb_id"] = a.KbID
	}
	if skills := agentSkillNames(a.Skills); len(skills) > 0 {
		payload["skills"] = skills
	}
	if plugins := agentPluginNames(a.Plugins); len(plugins) > 0 {
		payload["plugins"] = plugins
	}
	return payload
}

// agentPluginNames 解析 Agent 绑定的插件名（json 字符串数组）；未绑定或格式不对时
// 返回 nil，等价于"不筛选插件"。与 agentSkillNames 同构 —— 两者都是字符串数组列。
func agentPluginNames(plugins json.RawMessage) []string {
	return agentSkillNames(plugins)
}

// decodeJSONArray 解析 json 数组列；空值或格式不对时返回 nil（表示"不传该字段"）
func decodeJSONArray(raw json.RawMessage) []any {
	if len(raw) == 0 {
		return nil
	}
	var out []any
	if err := json.Unmarshal(raw, &out); err != nil {
		return nil
	}
	return out
}

// agentModel 从 llm_config（json）里取模型名
func agentModel(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return ""
	}
	return llmString(cfg, "model", "")
}

// agentTemperature 从 llm_config 里取温度；缺失/非法/<=0 一律返回 0 = "不覆盖"。
func agentTemperature(raw json.RawMessage) float64 {
	if len(raw) == 0 {
		return 0
	}
	var cfg map[string]any
	if err := json.Unmarshal(raw, &cfg); err != nil {
		return 0
	}
	return llmFloat(cfg, "temperature", 0)
}

// ── Agent 人格 + 会话级附加提示的拼装 ──
//
// 事故形态：Agent 的 system_prompt 到了引擎会**整体替换**默认人格（main.py 的
// `task.system_prompt = agent_conf["system_prompt"]`）。于是"会话里再加一句要求"
// 要么被 Agent 覆盖掉，要么（若有人直接拼接）无限增长把上下文挤爆。
//
// 这里定两条规则：
//  1. 明确分隔标记 —— 模型能分清"这是我的人格"与"这是本次会话的临时要求"；
//  2. 总长度上限 —— Agent 部分与附加部分各有预算，超出截断并**注明**已截断，
//     让模型（和看日志的人）知道信息不全，而不是静默丢半句。
const (
	// maxAgentBasePromptChars 是 Agent 人格 prompt 的字符预算（约 4k tokens）。
	maxAgentBasePromptChars = 8000
	// maxSessionPromptChars 是会话级附加提示的预算（约 2k tokens）。
	maxSessionPromptChars = 4000
	// sessionPromptSeparator 是两段之间的**显式**分隔：带标题，模型不会把附加要求
	// 误读成 Agent 人格的一部分。
	sessionPromptSeparator = "\n\n---\n\n## 本次会话的附加要求（优先于上面的通用人格）\n\n"
	truncationNote         = "\n\n[已截断：内容超出长度上限]"
)

// sessionSystemPrompt 取会话级附加提示。
//
// 来源是 `context.system_prompt`（可选字段）：客户端想给**本次会话**加要求时放这里，
// 而不是去改 Agent 记录（那会影响其它会话）。非字符串/空串一律视为未设置。
func sessionSystemPrompt(wbCtx map[string]any) string {
	if wbCtx == nil {
		return ""
	}
	if s, ok := wbCtx["system_prompt"].(string); ok {
		return strings.TrimSpace(s)
	}
	return ""
}

// truncateChars 按**字符（rune）**截断：按字节截会把中文切成乱码。
func truncateChars(s string, limit int) (string, bool) {
	if limit <= 0 {
		return "", s != ""
	}
	runes := []rune(s)
	if len(runes) <= limit {
		return s, false
	}
	return string(runes[:limit]) + truncationNote, true
}

// composeAgentSystemPrompt 把 Agent 人格与会话级附加提示拼成最终 system prompt。
//
// 纯函数：不查库、不读配置，便于直接单测。返回空串表示"这次没有 Agent 相关上下文"
// （调用方据此不下发 system_prompt，引擎回落到自己的默认人格）。
func composeAgentSystemPrompt(agentPrompt, sessionPrompt string) string {
	base := strings.TrimSpace(agentPrompt)
	extra := strings.TrimSpace(sessionPrompt)
	if base == "" && extra == "" {
		return ""
	}

	base, _ = truncateChars(base, maxAgentBasePromptChars)
	extra, _ = truncateChars(extra, maxSessionPromptChars)

	switch {
	case base == "":
		return extra
	case extra == "":
		return base
	default:
		return base + sessionPromptSeparator + extra
	}
}

// resolveAgentContext 在"只带 agent_id、没带 agent 配置"时补全 context.agent。
//
// 幂等：已有完整 agent 对象时只补会话级附加提示（不查库、不覆盖客户端给的字段）。
// 查不到 Agent（不存在/非本人/非共享）时保持原样并记日志 —— 让请求继续走，只是
// "这次没带 Agent"，与改动前的行为一致；不因为一个坏 agent_id 让整条对话失败。
func resolveAgentContext(ctx context.Context, wbCtx map[string]any, tenantID int16, userID string) {
	if wbCtx == nil {
		return
	}
	sessionPrompt := sessionSystemPrompt(wbCtx)
	if existing, ok := wbCtx["agent"].(map[string]any); ok && len(existing) > 0 {
		// 客户端已给全量配置：Agent 部分以它为准，只把会话级附加提示按同一口径
		// 拼上（分隔标记 + 长度上限）。没有附加提示时逐字不动。
		if sessionPrompt != "" {
			base, _ := existing["system_prompt"].(string)
			if composed := composeAgentSystemPrompt(base, sessionPrompt); composed != "" {
				existing["system_prompt"] = composed
			}
		}
		return
	}
	agentID, _ := wbCtx["agent_id"].(string)
	if strings.TrimSpace(agentID) == "" {
		return
	}
	agent, err := loadAgent(ctx, tenantID, userID, agentID)
	if err != nil {
		slog.Warn("workbench: agent context fallback failed", "agent_id", agentID, "error", err)
		return
	}
	if payload := agentContextPayload(agent, sessionPrompt); payload != nil {
		wbCtx["agent"] = payload
		applyAgentBindings(wbCtx, agent)
		// 多选的其余 Agent → "可委派的专家"。一个对话只能有一个人格（首选的 Agent），
		// 但可以有多个可请教的专家 —— 这正是"多选 Agent"的语义。
		if experts := loadExpertPayloads(ctx, wbCtx, tenantID, userID); len(experts) > 0 {
			payload["experts"] = experts
		}
	}
}

// applyAgentBindings 把 Agent 自带的绑定提升到 workbench_context 顶层。
//
// 引擎读的是**顶层** kb_id / skill_names / plugin_names：
//   - kb_id、skill_names 见 python-engine/app/agent/prompt_engine.py
//   - plugin_names 见 python-engine/app/agent/runtime.py 的 _restrict_tools_to_plugins
//
// 只放在 context["agent"] 里引擎读不到，等于"绑了没生效"。用户本次显式选的值优先，
// Agent 上带的只是默认值（所以非空就不覆盖）。
//
// 从 resolveAgentContext 里抽出来是为了能直接单测：原来内联在那一层，而那一层要先
// 查库（loadAgent），测它就得先起一个带完整 schema 的 PostgreSQL。
func applyAgentBindings(wbCtx map[string]any, agent *Agent) {
	if wbCtx == nil || agent == nil {
		return
	}
	if kb, _ := wbCtx["kb_id"].(string); strings.TrimSpace(kb) == "" && agent.KbID != "" {
		wbCtx["kb_id"] = agent.KbID
	}
	if _, hasSkills := wbCtx["skill_names"]; !hasSkills {
		if skills := agentSkillNames(agent.Skills); len(skills) > 0 {
			wbCtx["skill_names"] = skills
		}
	}
	if _, hasPlugins := wbCtx["plugin_names"]; !hasPlugins {
		if plugins := agentPluginNames(agent.Plugins); len(plugins) > 0 {
			wbCtx["plugin_names"] = plugins
		}
	}
}

// loadExpertPayloads 取 agent_ids 里除首个之外的 Agent（主 Agent 之外的"可委派专家"）。
//
// 单个查不到就跳过：不该因为列表里一个坏 id 让其余专家一起丢失。专家只带
// 展示与人格所需字段，不带工具集 —— 委派出去的 child 复用主对话的工具预算。
func loadExpertPayloads(ctx context.Context, wbCtx map[string]any, tenantID int16, userID string) []map[string]any {
	ids := contextIDList(wbCtx, "agent")
	if len(ids) < 2 {
		return nil
	}
	experts := make([]map[string]any, 0, len(ids)-1)
	for _, id := range ids[1:] {
		agent, err := loadAgent(ctx, tenantID, userID, id)
		if err != nil || agent == nil {
			slog.Warn("workbench: expert agent not loadable", "agent_id", id, "error", err)
			continue
		}
		expert := map[string]any{"id": agent.ID}
		if agent.Name != "" {
			expert["name"] = agent.Name
		}
		if agent.Description != "" {
			expert["description"] = agent.Description
		}
		if agent.SystemPrompt != "" {
			expert["system_prompt"] = agent.SystemPrompt
		}
		experts = append(experts, expert)
	}
	return experts
}

// contextIDList 读多值 <name>_ids，缺失时回退单值 <name>_id。
// 与前端 buildContextQuery、python 侧 context_ids 同一口径（复数优先、单数兼容），
// 否则同一个 URL 在三条链路上会解析出不同结果。
//
// 数组同时接受 []any（请求体经 DecodeJSON 后的常态）与 []string（Go 侧构造
// context 时的手写形态）：只认前者会让后者静默退化成"没传多值"。
func contextIDList(wbCtx map[string]any, name string) []string {
	if raw, ok := wbCtx[name+"_ids"]; ok {
		out := make([]string, 0, 4)
		switch v := raw.(type) {
		case []any:
			for _, item := range v {
				if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
					out = append(out, strings.TrimSpace(s))
				}
			}
		case []string:
			for _, s := range v {
				if strings.TrimSpace(s) != "" {
					out = append(out, strings.TrimSpace(s))
				}
			}
		}
		if len(out) > 0 {
			return out
		}
	}
	if s, ok := wbCtx[name+"_id"].(string); ok && strings.TrimSpace(s) != "" {
		return []string{strings.TrimSpace(s)}
	}
	return nil
}
func joinComma(items []string) string { return strings.Join(items, ", ") }
func llmString(m map[string]any, key, fallback string) string {
	if v, ok := m[key].(string); ok && v != "" {
		return v
	}
	return fallback
}
func llmInt(m map[string]any, key string, fallback int) int {
	if v, ok := m[key].(float64); ok && v > 0 {
		return int(v)
	}
	return fallback
}
func llmFloat(m map[string]any, key string, fallback float64) float64 {
	if v, ok := m[key].(float64); ok && v > 0 {
		return v
	}
	return fallback
}
func boolOf(v any) bool {
	b, _ := v.(bool)
	return b
}
