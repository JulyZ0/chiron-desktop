package engine

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// 快速失败修复的回归测试：
// 服务不可用时（连接拒绝 / 引擎假死不响应），客户端必须在秒级失败并返回错误，
// 而不是干等到 http.Client 60s 总超时且在同一地址上重试 4 次（曾导致前端
// "思考中" 空等 50s+ 才收到 "Service temporarily unavailable"）。

// TestRunSSE_ConnectionRefused_FailsFast：Python 引擎不可达（端口无监听，连接被拒）
// 时应立即返回错误，不做长时间重试。
func TestRunSSE_ConnectionRefused_FailsFast(t *testing.T) {
	// 占用一个端口后立刻释放：请求时该地址无监听 → ECONNREFUSED
	ls, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen: %v", err)
	}
	addr := ls.Addr().String()
	ls.Close()

	client := NewPythonClient("http://" + addr)

	start := time.Now()
	_, err = client.RunSSE(t.Context(), "/v1/agent/submit", map[string]any{"content": "hi"})
	elapsed := time.Since(start)

	if err == nil {
		t.Fatalf("expected error for unreachable python engine, got nil")
	}
	// 连接拒绝毫秒级失败；给 CI 留足余量。修复前此处会等满 60s 超时并重试。
	if elapsed > 3*time.Second {
		t.Fatalf("connection refused should fail fast, took %v", elapsed)
	}
	t.Logf("connection refused failed fast in %v: %v", elapsed, err)
}

// TestRunSSE_NormalStream_Unaffected：引擎正常时 SSE 流式输出照常工作，
// 新增的拨号/响应头超时不得误伤正常请求。
func TestRunSSE_NormalStream_Unaffected(t *testing.T) {
	events := []map[string]any{
		{"type": "text", "content": "hello"},
		{"type": "done"},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		if f, ok := w.(http.Flusher); ok {
			f.Flush() // 立即发送响应头（Python 端 StreamingResponse 行为）
		}
		for _, ev := range events {
			b, _ := json.Marshal(ev)
			fmt.Fprintf(w, "data: %s\n\n", b)
			if f, ok := w.(http.Flusher); ok {
				f.Flush()
			}
		}
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)

	ch, err := client.RunSSE(t.Context(), "/v1/agent/submit", map[string]any{"content": "hi"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	var got []PythonEvent
	for ev := range ch {
		got = append(got, ev)
	}
	if len(got) != len(events) {
		t.Fatalf("expected %d events, got %d: %+v", len(events), len(got), got)
	}
	if got[0].Type != "text" || got[0].Content != "hello" {
		t.Fatalf("unexpected first event: %+v", got[0])
	}
}

// TestRunSSE_HTTP503_StillRetries：HTTP 5xx/429 的重试语义必须保留
// （本次修复仅对"网络层不可达"快速失败，不改变服务端 5xx 的瞬时抖动容忍）。
func TestRunSSE_HTTP503_StillRetries(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if calls.Add(1) <= 2 {
			http.Error(w, "boom", http.StatusServiceUnavailable)
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		fmt.Fprintf(w, "data: %s\n\n", `{"type":"text","content":"ok"}`)
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)

	ch, err := client.RunSSE(t.Context(), "/v1/agent/submit", map[string]any{"content": "hi"})
	if err != nil {
		t.Fatalf("expected 503 retried to success, got error: %v", err)
	}
	got := make([]PythonEvent, 0)
	for ev := range ch {
		got = append(got, ev)
	}
	if calls.Load() < 3 {
		t.Fatalf("expected >=3 attempts (2x 503 + 1x success), got %d", calls.Load())
	}
	if len(got) != 1 || got[0].Content != "ok" {
		t.Fatalf("unexpected events after retry: %+v", got)
	}
}

// 回归测试（API Key 保存 400 根因）：Python 引擎（FastAPI）只为 /healthz 注册 GET，
// HEAD 返回 405。IsConnected 曾用 HEAD 探测 → 引擎存活却被判不可达，
// 导致管理端 API Key 保存恒 400 "python engine not available"。现改用 GET，须判连通。
func TestIsConnected_GETOnlyHealthz(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" {
			http.NotFound(w, r)
			return
		}
		if r.Method != http.MethodGet {
			// 模拟 FastAPI GET-only 路由：HEAD 等非 GET 方法返回 405
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	client := NewPythonClient(srv.URL)
	if !client.IsConnected() {
		t.Fatalf("IsConnected should be true when GET /healthz returns 200 (engine is alive)")
	}

	// HealthCheck 使用同一探测路径，应判定 healthy
	h := client.HealthCheck(t.Context())
	if healthy, _ := h["healthy"].(bool); !healthy {
		t.Fatalf("HealthCheck should report healthy, got: %+v", h)
	}
}

// TestPythonEvent_ForwardsFieldsItDeclares 守住"网关不吞字段"。
//
// 这条链是四段序列化：引擎帧 → `PythonEvent` → `broadcast.Event` → 浏览器。
// 网关转发时是 `Data: evt`（本结构体），**未声明的字段在重新序列化那一刻就消失**了
// —— 只改引擎没有用，必须两边都声明。历史故障全是这一种形态：
//
//   - `revision` 缺 ⇒ 前端 `shouldApply` 收到 undefined，乱序防护退化成"永远接受"，
//     重连补发时清单面板倒退一格然后永远差一格；
//   - `options` 缺 ⇒ `ask_user` 的候选项永远是空列表，用户只能自己手打；
//   - `budget`/`context`/`cache` 缺 ⇒ 预算面板、上下文占用、缓存诊断全空着；
//   - `duration_ms` 缺 ⇒ `mergeTurnStats` 的 `> 0` 守卫直接跳过，本轮耗时恒不显示。
//
// 所以断言不是"解出来是对的"，而是"**再序列化一次**之后它还在" —— 那才是
// 前端真正收到的形状。
func TestPythonEvent_ForwardsFieldsItDeclares(t *testing.T) {
	frame := `{"type":"todo_updated","content":"[]","revision":7,"duration_ms":1234,` +
		`"options":["A","B"],"budget":{"tokens":{"used":10,"limit":100}},` +
		`"context":{"used":5,"limit":50},"cache":{"prefix_changed":["system"]},` +
		`"input_tokens":11,"output_tokens":22,"cached_tokens":33,"model":"m"}`

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		fmt.Fprintf(w, "data: %s\n\n", frame)
	}))
	defer srv.Close()

	ch, err := NewPythonClient(srv.URL).RunSSE(t.Context(), "/v1/agent/submit", map[string]any{"content": "hi"})
	if err != nil {
		t.Fatalf("RunSSE: %v", err)
	}
	var events []PythonEvent
	for ev := range ch {
		events = append(events, ev)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}

	// 关键一步：按网关的真实做法重新序列化（`Data: evt`），再解码回来看字段在不在。
	raw, err := json.Marshal(events[0])
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var forwarded map[string]any
	if err := json.Unmarshal(raw, &forwarded); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	for _, key := range []string{
		"revision", "duration_ms", "options", "budget", "context", "cache",
		"input_tokens", "output_tokens", "cached_tokens", "model",
	} {
		if _, ok := forwarded[key]; !ok {
			t.Errorf("field %q disappeared at the gateway (engine sent it, frontend never sees it)", key)
		}
	}

	if got := forwarded["revision"]; got != float64(7) {
		t.Errorf("revision: got %v, want 7", got)
	}
	if got := forwarded["duration_ms"]; got != float64(1234) {
		t.Errorf("duration_ms: got %v, want 1234", got)
	}
	if opts, ok := forwarded["options"].([]any); !ok || len(opts) != 2 {
		t.Errorf("options: got %v, want 2 entries", forwarded["options"])
	}
}

// TestNewPythonClientWithTimeout_WiresSyncOnly 固定住"`PYTHON_ENGINE_TIMEOUT` 真的接上了"。
//
// 缺陷形态：该配置项此前**全仓零读取点**（只在 config.go 定义与赋值），而同步客户端
// 把超时硬编码成 60s。`/v1/agents/dispatch`、`/v1/context/condense`、
// `/v1/checkpoints/restore` 这类"完整编排后才返回 JSON"的端点只要超过 60s 就被判失败
// —— 长任务在这里静默断掉，错误只是一句 `context deadline exceeded`，看不出是被
// 客户端超时掐的。
//
// 两条判据必须同时成立：
//  1. 同步客户端拿到传入的超时（否则配置仍是死的）；
//  2. **流式客户端不能**设整体超时 —— 那会把长回答硬截断在中途（见 streamClient 的注释）。
func TestNewPythonClientWithTimeout_WiresSyncOnly(t *testing.T) {
	client := NewPythonClientWithTimeout(5*time.Minute, "http://127.0.0.1:9999")

	if got := client.client.Timeout; got != 5*time.Minute {
		t.Fatalf("同步客户端超时 = %v，期望 5m（PYTHON_ENGINE_TIMEOUT 没接上）", got)
	}
	if got := client.streamClient.Timeout; got != 0 {
		t.Fatalf("流式客户端超时 = %v，必须是 0（设了整体超时会硬截断长回答）", got)
	}
}

// TestNewPythonClientWithTimeout_FallsBackOnNonPositive 配成 0/负数时回落到默认值。
//
// 配 0 的意图通常不是"立刻失败"；直接照用会让每个请求在发出瞬间就超时，
// 表现为"引擎永远不可用"，而日志里只是一片 context deadline exceeded。
func TestNewPythonClientWithTimeout_FallsBackOnNonPositive(t *testing.T) {
	for _, v := range []time.Duration{0, -time.Second} {
		client := NewPythonClientWithTimeout(v, "http://127.0.0.1:9999")
		if got := client.client.Timeout; got != defaultSyncTimeout {
			t.Fatalf("timeout=%v 时同步超时 = %v，期望回落到 %v", v, got, defaultSyncTimeout)
		}
	}
}

// TestNewPythonClient_KeepsLightweightDefault 轻量入口（单测/脚本）保持 60s 默认。
//
// 它们的共同点是不该等一个挂死的引擎等上 5 分钟：默认值调大会让本地验证与
// 冒烟脚本的失败反馈慢到不可接受。真实部署走 NewPythonClientWithTimeout。
func TestNewPythonClient_KeepsLightweightDefault(t *testing.T) {
	client := NewPythonClient("http://127.0.0.1:9999")
	if got := client.client.Timeout; got != defaultSyncTimeout {
		t.Fatalf("默认同步超时 = %v，期望 %v", got, defaultSyncTimeout)
	}
}
