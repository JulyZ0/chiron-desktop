package api

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/session"
)

// 端到端记账：**带 usage 的 SSE 响应 → `usage_events` 的写入行**。
//
// ## 这条用例钉的是什么
//
// 用户看到的形态：计费页「近 90 天消耗全为 0」，而按会话看却有 N 次调用 ——
// 也就是"行存在，token 与花费都是 0"。这条链路跨四段（引擎 SSE → PythonEvent →
// 事件归因 → INSERT），任何一段把数字丢掉，表现都一模一样、都不会报错。
//
// 所以这里用一个**假引擎**发四种真实出现过的帧形状，断言最后写进去的行：
//
//  1. 扁平 `input_tokens/output_tokens/cached_tokens`（引擎自己的形状）；
//  2. OpenAI 形状 `prompt_tokens/completion_tokens`（兼容层）；
//  3. 嵌套 `usage: {prompt_tokens, ...}`（流式最后一帧的口径）；
//  4. 只有 `usage` 事件、`done` 完全不带 token（取消 / 截断时的形态）。
//
// 断言不只看"有没有 token"，还看 `model`：那是 `model_prices` 的唯一查询键，
// 丢了它 cost 只能是 NULL —— "token 有、花费 0"正是这么来的。

// usageCapture 捕获 RecordUsageEvent 实际要写的行（替换 usageEventWrite）。
type usageCapture struct {
	mu   sync.Mutex
	rows []UsageEvent
}

func (c *usageCapture) write(_ context.Context, ev UsageEvent) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.rows = append(c.rows, ev)
}

func (c *usageCapture) snapshot() []UsageEvent {
	c.mu.Lock()
	defer c.mu.Unlock()
	out := make([]UsageEvent, len(c.rows))
	copy(out, c.rows)
	return out
}

// captureUsageEvents 把落库动作换成内存捕获（用例结束自动还原）。
func captureUsageEvents(t *testing.T) *usageCapture {
	t.Helper()
	cap := &usageCapture{}
	prev := usageEventWrite
	usageEventWrite = cap.write
	t.Cleanup(func() { usageEventWrite = prev })
	return cap
}

func newFakeEngineSubmitHandler(t *testing.T, frames []string) *SubmitHandler {
	t.Helper()
	// 先发一段正文：Goal FSM 的判定层把"没有任何回答"判为 Incomplete 并**自动续跑**
	// 到轮数上限（每轮都会写一行用量，断言就变成 20 倍）。这与记账无关，是续跑
	// 语义的既有行为，所以在这里给每条用例补一个"已作答"的前缀，让它们只测记账。
	all := append([]string{`data: {"type":"text","content":"ok"}`}, frames...)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		for _, f := range all {
			_, _ = fmt.Fprint(w, f+"\n\n")
		}
	}))
	t.Cleanup(srv.Close)
	hub := broadcast.NewHub(nil)
	t.Cleanup(func() { hub.Close() })
	return NewSubmitHandler(
		engine.NewPythonClient(srv.URL),
		session.NewManager(nil, nil),
		hub,
		nil,
	)
}

// sumUsage 把捕获到的行按 (model, input, output, cached) 汇总成可断言的字面量。
func sumUsage(rows []UsageEvent) (input, output, cached int) {
	for _, r := range rows {
		input += r.InputTokens
		output += r.OutputTokens
		cached += r.CachedTokens
	}
	return input, output, cached
}

// 引擎原生形状：`usage`（按次增量）+ `done`（整轮累计）。
func TestSubmitWritesUsageRowsFromEngineNativeEvents(t *testing.T) {
	cap := captureUsageEvents(t)
	h := newFakeEngineSubmitHandler(t, []string{
		`data: {"type":"text","content":"hi"}`,
		`data: {"type":"usage","model":"gpt-4o","input_tokens":120,"output_tokens":30,"cached_tokens":40}`,
		`data: {"type":"done","model":"gpt-4o","input_tokens":200,"output_tokens":50,"cached_tokens":60}`,
	})

	h.HandleSubmit(context.Background(), "u1", "s-usage-1", "hello",
		map[string]interface{}{}, nil, nil)

	rows := cap.snapshot()
	if len(rows) == 0 {
		t.Fatal("带 usage 的回合必须写出用量行；一行都没有说明链路在某一段把用量吞掉了")
	}
	in, out, cached := sumUsage(rows)
	// done 是整轮累计，是唯一入账来源；usage 只是兜底，不能重复计。
	if in != 200 || out != 50 || cached != 60 {
		t.Fatalf("整轮用量应为 200/50/60（done 的累计），实际 %d/%d/%d", in, out, cached)
	}
	for _, r := range rows {
		if r.SessionID != "s-usage-1" {
			t.Fatalf("行的会话归属错了：%q", r.SessionID)
		}
		// model 是价格查询键：空 ⇒ cost_cents 只能是 NULL ⇒ 花费恒 0。
		if strings.TrimSpace(r.Model) == "" {
			t.Fatalf("model 不能为空（它是 model_prices 的查询键），行：%+v", r)
		}
	}
}

// OpenAI 形状的帧（兼容层 / 第三方网关）：`prompt_tokens` / `completion_tokens`。
//
// 这正是"有行、token 全 0"最可能的成因：网关的 PythonEvent 此前只声明了
// `input_tokens`，别的名字一律被 json.Unmarshal 静默丢弃。
func TestSubmitRecordsUsageFromOpenAIShapedFrames(t *testing.T) {
	cap := captureUsageEvents(t)
	h := newFakeEngineSubmitHandler(t, []string{
		`data: {"type":"done","model":"deepseek-chat","prompt_tokens":900,"completion_tokens":120,"usage":{"prompt_tokens_details":{"cached_tokens":512}}}`,
	})

	h.HandleSubmit(context.Background(), "u1", "s-usage-2", "hello",
		map[string]interface{}{}, nil, nil)

	rows := cap.snapshot()
	if len(rows) != 1 {
		t.Fatalf("应写 1 行，实际 %d 行：%+v", len(rows), rows)
	}
	r := rows[0]
	if r.InputTokens != 900 || r.OutputTokens != 120 {
		t.Fatalf("OpenAI 形状的 token 必须被折叠成规范字段，实际 %d/%d", r.InputTokens, r.OutputTokens)
	}
	if r.CachedTokens != 512 {
		t.Fatalf("缓存命中应从 prompt_tokens_details.cached_tokens 取到 512，实际 %d", r.CachedTokens)
	}
	if r.Model != "deepseek-chat" {
		t.Fatalf("模型名必须落到行上（价格查询键），实际 %q", r.Model)
	}
}

// 嵌套 `usage` 对象（流式最后一帧只有 usage，没有平铺字段）。
func TestSubmitRecordsUsageFromNestedUsageObject(t *testing.T) {
	cap := captureUsageEvents(t)
	h := newFakeEngineSubmitHandler(t, []string{
		`data: {"type":"done","model":"qwen-plus","usage":{"prompt_tokens":700,"completion_tokens":80,"cached_tokens":100}}`,
	})

	h.HandleSubmit(context.Background(), "u1", "s-usage-3", "hello",
		map[string]interface{}{}, nil, nil)

	rows := cap.snapshot()
	if len(rows) != 1 {
		t.Fatalf("应写 1 行，实际 %d 行", len(rows))
	}
	if rows[0].InputTokens != 700 || rows[0].OutputTokens != 80 || rows[0].CachedTokens != 100 {
		t.Fatalf("嵌套 usage 必须被折叠，实际 %+v", rows[0])
	}
}

// `done` 完全不带 token，只有逐次 `usage`（取消 / 截断 / 旧引擎）：兜底必须生效。
//
// 此前这条路径会写出"零 token 行"，用户看到的就是「9 次调用 / 0 tokens」。
func TestSubmitFallsBackToPerCallUsageWhenDoneHasNoTokens(t *testing.T) {
	cap := captureUsageEvents(t)
	h := newFakeEngineSubmitHandler(t, []string{
		`data: {"type":"usage","model":"gpt-4o-mini","input_tokens":300,"output_tokens":60}`,
		`data: {"type":"usage","model":"gpt-4o-mini","input_tokens":200,"output_tokens":40}`,
		`data: {"type":"done","model":"gpt-4o-mini"}`,
	})

	h.HandleSubmit(context.Background(), "u1", "s-usage-4", "hello",
		map[string]interface{}{}, nil, nil)

	rows := cap.snapshot()
	if len(rows) != 1 {
		t.Fatalf("同模型应合并成 1 行，实际 %d 行：%+v", len(rows), rows)
	}
	if rows[0].InputTokens != 500 || rows[0].OutputTokens != 100 {
		t.Fatalf("done 没带 token 时必须回落到增量之和（500/100），实际 %d/%d",
			rows[0].InputTokens, rows[0].OutputTokens)
	}
}

// `usage` 事件没带 model 时，必须用本轮归属模型兜底 —— 否则列写 NULL、价格查不到。
func TestSubmitAttributesModelWhenUsageEventOmitsIt(t *testing.T) {
	cap := captureUsageEvents(t)
	h := newFakeEngineSubmitHandler(t, []string{
		`data: {"type":"usage","input_tokens":100,"output_tokens":20}`,
		`data: {"type":"done","model":"gpt-4o","input_tokens":100,"output_tokens":20}`,
	})

	h.HandleSubmit(context.Background(), "u1", "s-usage-5", "hello",
		map[string]interface{}{}, nil, nil)

	rows := cap.snapshot()
	if len(rows) != 1 {
		t.Fatalf("应写 1 行，实际 %d 行：%+v", len(rows), rows)
	}
	if rows[0].Model != "gpt-4o" {
		t.Fatalf("usage 事件缺 model 时应回落到本轮模型 gpt-4o，实际 %q", rows[0].Model)
	}
}

// 真正零用量的回合仍要留一行（调用次数真实），但不得伪造 token。
func TestSubmitKeepsAZeroTokenRowForEmptyTurns(t *testing.T) {
	cap := captureUsageEvents(t)
	h := newFakeEngineSubmitHandler(t, []string{
		`data: {"type":"text","content":"nothing to do"}`,
		`data: {"type":"done","model":"gpt-4o"}`,
	})

	h.HandleSubmit(context.Background(), "u1", "s-usage-6", "hello",
		map[string]interface{}{}, nil, nil)

	rows := cap.snapshot()
	if len(rows) != 1 {
		t.Fatalf("零用量回合也应留 1 行（调用次数要真实），实际 %d 行", len(rows))
	}
	if rows[0].InputTokens != 0 || rows[0].OutputTokens != 0 {
		t.Fatalf("不许给零用量回合编 token，实际 %+v", rows[0])
	}
}
