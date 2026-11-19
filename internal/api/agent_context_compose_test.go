package api

import (
	"strings"
	"testing"
	"unicode/utf8"
)

// ── Agent 人格 + 会话级附加提示的拼装（agents.go / agent_context.go）──
//
// 守的是"配了 Agent 却像没配"的另一半：Agent 的 system_prompt 到了引擎会整体替换
// 默认人格，于是"本次会话再加一句要求"没有容身之处；而如果把两段无脑首尾相接，
// 又会随会话变长无限膨胀。这里钉住分隔标记与长度上限。

func TestComposeAgentSystemPrompt_AgentOnly(t *testing.T) {
	got := composeAgentSystemPrompt("  你是严格的代码评审者  ", "")
	if got != "你是严格的代码评审者" {
		t.Fatalf("只有 Agent 人格时应原样返回（去首尾空白）: %q", got)
	}
}

func TestComposeAgentSystemPrompt_SessionOnly(t *testing.T) {
	got := composeAgentSystemPrompt("", "  这次请用英文回答  ")
	if got != "这次请用英文回答" {
		t.Fatalf("只有会话级提示时应单独生效: %q", got)
	}
}

func TestComposeAgentSystemPrompt_BothHaveExplicitSeparator(t *testing.T) {
	got := composeAgentSystemPrompt("人格", "附加要求")
	if !strings.Contains(got, "人格") || !strings.Contains(got, "附加要求") {
		t.Fatalf("两段都要保留: %q", got)
	}
	if !strings.Contains(got, sessionPromptSeparator) {
		t.Fatalf("两段之间必须有明确的标题分隔: %q", got)
	}
	// 人格在前、附加要求在后：后者是"本次会话"的临时约束，位置越靠后越贴近本轮请求。
	if strings.Index(got, "人格") > strings.Index(got, "附加要求") {
		t.Fatalf("人格应在附加要求之前: %q", got)
	}
}

func TestComposeAgentSystemPrompt_EmptyWhenNothing(t *testing.T) {
	if got := composeAgentSystemPrompt("   ", "\n\t"); got != "" {
		t.Fatalf("两段都空时应返回空串（引擎据此回落默认人格）: %q", got)
	}
}

func TestComposeAgentSystemPrompt_TruncatesAgentPrompt(t *testing.T) {
	long := strings.Repeat("人", maxAgentBasePromptChars+500)
	got := composeAgentSystemPrompt(long, "")
	if !strings.Contains(got, strings.TrimSpace(truncationNote)) {
		t.Fatalf("超长 Agent 人格必须注明已截断")
	}
	// 截断按 rune 计：不能把中文切成乱码，总长度也不能超过预算+说明。
	body := strings.TrimSuffix(got, truncationNote)
	if n := utf8.RuneCountInString(body); n > maxAgentBasePromptChars {
		t.Fatalf("截断后仍超预算: %d runes", n)
	}
	if !utf8.ValidString(got) {
		t.Fatal("截断把多字节字符切坏了")
	}
}

func TestComposeAgentSystemPrompt_TruncatesSessionPrompt(t *testing.T) {
	long := strings.Repeat("x", maxSessionPromptChars+100)
	got := composeAgentSystemPrompt("人格", long)
	extra := got[strings.Index(got, sessionPromptSeparator)+len(sessionPromptSeparator):]
	if !strings.Contains(extra, strings.TrimSpace(truncationNote)) {
		t.Fatalf("超长会话附加提示必须注明已截断")
	}
	if n := len(strings.TrimSuffix(extra, truncationNote)); n > maxSessionPromptChars {
		t.Fatalf("会话附加提示截断后仍超预算: %d", n)
	}
}

// temperature 只在 Agent 真的配了（>0）时才下发 —— 0 的含义是"不覆盖"，
// 不能变成"温度必须为 0"，那会悄悄改掉用户的采样设置。
func TestAgentContextPayload_OverridesOnlyWhenNonEmpty(t *testing.T) {
	bare := agentContextPayload(&Agent{ID: "a1"}, "")
	if bare == nil {
		t.Fatal("payload 不应为 nil")
	}
	for _, key := range []string{"system_prompt", "model", "temperature", "max_turns"} {
		if _, ok := bare[key]; ok {
			t.Errorf("字段为空时不该下发 %q: %v", key, bare[key])
		}
	}

	full := agentContextPayload(&Agent{
		ID:           "a1",
		SystemPrompt: "人格",
		MaxTurns:     7,
		LLMConfig:    rawJSON(`{"model":"deepseek-chat","temperature":0.3}`),
	}, "本次请简短")
	if full["model"] != "deepseek-chat" {
		t.Errorf("model 未下发: %v", full["model"])
	}
	if full["temperature"] != 0.3 {
		t.Errorf("temperature 未下发: %v", full["temperature"])
	}
	if full["max_turns"] != 7 {
		t.Errorf("max_turns 未下发: %v", full["max_turns"])
	}
	prompt, _ := full["system_prompt"].(string)
	if !strings.Contains(prompt, "人格") || !strings.Contains(prompt, "本次请简短") {
		t.Errorf("system_prompt 未把人格与会话级提示拼起来: %q", prompt)
	}
}

// 0 温度 = 不覆盖（而不是覆盖成 0）。
func TestAgentContextPayload_ZeroTemperatureIsNotOverride(t *testing.T) {
	payload := agentContextPayload(&Agent{ID: "a1", LLMConfig: rawJSON(`{"temperature":0}`)}, "")
	if _, ok := payload["temperature"]; ok {
		t.Fatalf("temperature=0 不该下发: %v", payload["temperature"])
	}
}

func TestSessionSystemPrompt(t *testing.T) {
	if got := sessionSystemPrompt(map[string]any{"system_prompt": "  附加  "}); got != "附加" {
		t.Fatalf("want %q, got %q", "附加", got)
	}
	if got := sessionSystemPrompt(map[string]any{"system_prompt": 42}); got != "" {
		t.Fatalf("非字符串应视为未设置, got %q", got)
	}
	if got := sessionSystemPrompt(nil); got != "" {
		t.Fatalf("nil 上下文应返回空串, got %q", got)
	}
}

// 客户端已给全量 agent 配置时：不查库，只把会话级附加提示按同一口径拼上。
func TestResolveAgentContext_MergesSessionPromptIntoClientAgent(t *testing.T) {
	wb := map[string]any{
		"system_prompt": "本次只用只读命令",
		"agent":         map[string]any{"id": "a1", "system_prompt": "人格"},
	}
	resolveAgentContext(t.Context(), wb, 1, "u1")

	agent := wb["agent"].(map[string]any)
	prompt, _ := agent["system_prompt"].(string)
	if !strings.Contains(prompt, "人格") || !strings.Contains(prompt, "本次只用只读命令") {
		t.Fatalf("会话级提示未合并进客户端给的 agent: %q", prompt)
	}
	if !strings.Contains(prompt, sessionPromptSeparator) {
		t.Fatalf("合并必须带明确分隔: %q", prompt)
	}
}

// 没有会话级提示时，客户端给的配置**逐字不动**（幂等）。
func TestResolveAgentContext_ClientAgentUntouchedWithoutSessionPrompt(t *testing.T) {
	agent := map[string]any{"id": "a1", "system_prompt": "人格"}
	wb := map[string]any{"agent": agent}
	resolveAgentContext(t.Context(), wb, 1, "u1")
	if agent["system_prompt"] != "人格" {
		t.Fatalf("无附加提示时不该改 system_prompt: %v", agent["system_prompt"])
	}
}
