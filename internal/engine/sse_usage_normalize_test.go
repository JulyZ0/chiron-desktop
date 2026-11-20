package engine

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
)

// SSE 帧 → PythonEvent 的**用量字段折叠**。
//
// 缺陷形态（用户可见）：计费页「近 90 天消耗全为 0」，而按会话看却有 N 次调用 ——
// `usage_events` 里有行，`input/output/cached` 全是 0。
//
// 根因之一就在这里：引擎发的是扁平 `input_tokens`，但兼容层 / 第三方网关按 OpenAI
// 契约发 `prompt_tokens` / `completion_tokens`，流式最后一帧还可能是独立的
// `usage` 对象。这些名字此前在 PythonEvent 里**没有声明**，`json.Unmarshal` 于是
// 静默丢弃它们 —— 不报错、不告警，只是所有用量变成 0。

// runSSE 起一个假引擎，吐出给定帧，收齐后返回事件。
func sseEvents(t *testing.T, frames ...string) []PythonEvent {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		for _, f := range frames {
			_, _ = fmt.Fprint(w, f+"\n\n")
		}
	}))
	t.Cleanup(srv.Close)

	ch, err := NewPythonClient(srv.URL).RunSSE(context.Background(), "/v1/agent/submit", map[string]any{})
	if err != nil {
		t.Fatalf("RunSSE: %v", err)
	}
	var out []PythonEvent
	for ev := range ch {
		out = append(out, ev)
	}
	return out
}

func TestRunSSENormalizesFlatEngineTokens(t *testing.T) {
	events := sseEvents(t, `data: {"type":"done","model":"gpt-4o","input_tokens":120,"output_tokens":30,"cached_tokens":40}`)
	if len(events) != 1 {
		t.Fatalf("应收到 1 条事件，实际 %d", len(events))
	}
	ev := events[0]
	if ev.InputTokens != 120 || ev.OutputTokens != 30 || ev.CachedTokens != 40 {
		t.Fatalf("扁平字段应原样保留，实际 %d/%d/%d", ev.InputTokens, ev.OutputTokens, ev.CachedTokens)
	}
	if ev.Model != "gpt-4o" {
		t.Fatalf("model 应保留（价格查询键），实际 %q", ev.Model)
	}
}

func TestRunSSENormalizesOpenAIShapedTokens(t *testing.T) {
	events := sseEvents(t, `data: {"type":"done","model":"deepseek-chat","prompt_tokens":900,"completion_tokens":120}`)
	ev := events[0]
	if ev.InputTokens != 900 || ev.OutputTokens != 120 {
		t.Fatalf("prompt_tokens/completion_tokens 必须折叠成 input/output，实际 %d/%d",
			ev.InputTokens, ev.OutputTokens)
	}
}

func TestRunSSENormalizesNestedUsageObject(t *testing.T) {
	events := sseEvents(t, `data: {"type":"done","usage":{"prompt_tokens":700,"completion_tokens":80,"cached_tokens":100}}`)
	ev := events[0]
	if ev.InputTokens != 700 || ev.OutputTokens != 80 || ev.CachedTokens != 100 {
		t.Fatalf("嵌套 usage 必须被折叠，实际 %d/%d/%d", ev.InputTokens, ev.OutputTokens, ev.CachedTokens)
	}
}

func TestRunSSENormalizesCachedTokensFromDetails(t *testing.T) {
	events := sseEvents(t, `data: {"type":"done","usage":{"prompt_tokens":500,"completion_tokens":50,"prompt_tokens_details":{"cached_tokens":384}}}`)
	ev := events[0]
	if ev.CachedTokens != 384 {
		t.Fatalf("缓存命中应从 prompt_tokens_details.cached_tokens 取到，实际 %d", ev.CachedTokens)
	}
}

// 幂等 + 不覆盖：同一帧里两套数字同时存在时，**规范字段优先**。
func TestNormalizeUsagePrefersCanonicalFields(t *testing.T) {
	ev := PythonEvent{
		InputTokens:      10,
		OutputTokens:     2,
		PromptTokens:     999,
		CompletionTokens: 999,
		Usage:            map[string]any{"prompt_tokens": float64(999)},
	}
	ev.NormalizeUsage()
	if ev.InputTokens != 10 || ev.OutputTokens != 2 {
		t.Fatalf("已有规范字段时不该被别名覆盖，实际 %d/%d", ev.InputTokens, ev.OutputTokens)
	}
	// 再折一次结果不变（幂等）
	before, _ := json.Marshal(ev)
	ev.NormalizeUsage()
	after, _ := json.Marshal(ev)
	if string(before) != string(after) {
		t.Fatalf("NormalizeUsage 必须幂等")
	}
}

// 没有任何用量字段的帧不该被凭空补出 token。
func TestNormalizeUsageLeavesEmptyFramesAlone(t *testing.T) {
	events := sseEvents(t, `data: {"type":"text","content":"hi"}`)
	ev := events[0]
	if ev.InputTokens != 0 || ev.OutputTokens != 0 || ev.CachedTokens != 0 {
		t.Fatalf("无用量字段时不该编造 token，实际 %d/%d/%d", ev.InputTokens, ev.OutputTokens, ev.CachedTokens)
	}
}
