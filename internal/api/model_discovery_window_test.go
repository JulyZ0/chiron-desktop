package api

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
)

// 用户报（2026-11-15）「deepseek-v4.1-flash 有 1M 上下文，但 /chat 页显示 128000」。
//
// 根因不是推断表里某个数字写错，而是**窗口的唯一来源一直是"猜"**：
// `fetchProviderModels` 只解析 `data[].id`，网关如实上报的窗口被整个丢掉，
// 于是只能靠 `inferContextWindow` 按家族给一个值。改哪条家族规则都只是治标——
// 下一个新模型照样错。这里钉住的是"真的去读网关上报的字段"。

func newModelsServer(t *testing.T, payload string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/models" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, payload)
	}))
	t.Cleanup(srv.Close)
	return srv.URL + "/v1"
}

func TestFetchProviderModelsParsesReportedWindow(t *testing.T) {
	base := newModelsServer(t, `{
	  "data": [
	    {"id": "deepseek-v4.1-flash", "limit": {"context": 1000000, "output": 384000}},
	    {"id": "big-pickle", "limit": {"context": 200000, "input": 160000, "output": 32000}},
	    {"id": "string-window", "max_input_tokens": "200000"},
	    {"id": "vllm-style", "max_model_len": 32768},
	    {"id": "nested", "top_provider": {"context_length": 128000}},
	    {"id": "top-level", "context_length": 64000},
	    {"id": "window-field", "context_window": 32000},
	    {"id": "no-window"},
	    {"id": "out-only", "max_tokens": 8192, "max_output_tokens": 4096},
	    {"id": "output-only-nested", "limit": {"output": 384000}},
	    {"context_length": 999999}
	  ]
	}`)

	models, err := fetchProviderModels(context.Background(), base, "k")
	if err != nil {
		t.Fatalf("fetchProviderModels: %v", err)
	}

	got := make(map[string]int, len(models))
	for _, m := range models {
		got[m.ID] = m.Window
	}

	want := map[string]int{
		// opencode zen 实测口径：`limit.context`。用户报的就是这个模型显示 128000。
		"deepseek-v4.1-flash": 1000000,
		// 同一条记录里 `limit.input` 是 `limit.context` 的子集，必须让 context 优先
		"big-pickle":         200000,
		"string-window":      200000, // models.dev / LiteLLM 风格，且值是字符串
		"vllm-style":         32768,  // vLLM 风格
		"nested":             128000, // OpenRouter 的 top_provider 嵌套
		"top-level":          64000,  // OpenRouter 顶层写法
		"window-field":       32000,  // 部分自建网关
		"no-window":          0,      // 没报 ⇒ 0，交给推断兜底
		"out-only":           0,      // 只有输出上限 ⇒ 绝不能当窗口
		"output-only-nested": 0,      // 同上，嵌套形式也不能认
	}
	for id, window := range want {
		if _, ok := got[id]; !ok {
			t.Errorf("缺少模型 %q", id)
			continue
		}
		if got[id] != window {
			t.Errorf("%s: window = %d, want %d", id, got[id], window)
		}
	}

	// 没有 id 的条目必须跳过：拿它建行会得到一个无法路由的模型。
	if _, ok := got[""]; ok {
		t.Errorf("无 id 的条目不该被收下：%+v", models)
	}
	if len(models) != len(want) {
		t.Errorf("模型数 = %d, want %d：%+v", len(models), len(want), models)
	}
}

// 用户报的现场的直译：opencode zen 的真实响应片段必须解析出 1000000，
// 而不是被 `limit.output`（384000）或家族表（deepseek ⇒ 128000）带偏。
func TestParseModelWindowReadsOpencodeLimitContext(t *testing.T) {
	raw := map[string]json.RawMessage{}
	line := `{
	  "id": "deepseek-v4.1-flash",
	  "name": "DeepSeek V4.1 Flash 1017",
	  "limit": {"context": 1000000, "output": 384000},
	  "cost": {"text": {"input": {"price": 0.15}, "output": {"price": 0.6}}}
	}`
	var outer map[string]json.RawMessage
	if err := json.Unmarshal([]byte(line), &outer); err != nil {
		t.Fatalf("解码失败: %v", err)
	}
	for k, v := range outer {
		raw[k] = v
	}

	if got := parseModelWindow(raw); got != 1000000 {
		t.Fatalf("limit.context 应解析为 1000000，实得 %d", got)
	}
}

// 重复 id 只留一条：provider 的 /models 可能重复返回，重复建行会在库里撞唯一约束。
func TestFetchProviderModelsDedupesIDs(t *testing.T) {
	base := newModelsServer(t, `{"data":[
	  {"id":"dup","context_length":111},
	  {"id":"dup","context_length":222}
	]}`)

	models, err := fetchProviderModels(context.Background(), base, "k")
	if err != nil {
		t.Fatalf("fetchProviderModels: %v", err)
	}
	if len(models) != 1 {
		t.Fatalf("应只留一条，实得 %d：%+v", len(models), models)
	}
	if models[0].Window != 111 {
		t.Errorf("应保留首次出现的值 111，实得 %d", models[0].Window)
	}
}

// 网关回非 200 时要报错，而不是返回空列表 —— 空列表会让调用方
// `len(models) == 0` 分支跳过同步（那是正确的），但错误信息必须能被日志看见。
func TestFetchProviderModelsReportsHTTPError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
		_, _ = io.WriteString(w, `{"error":"bad key"}`)
	}))
	t.Cleanup(srv.Close)

	if _, err := fetchProviderModels(context.Background(), srv.URL+"/v1", "k"); err == nil {
		t.Fatal("401 应返回错误")
	}
}
