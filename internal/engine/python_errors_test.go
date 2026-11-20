package engine

// python_errors_test.go —— 错误分类与"探针不致熔断"的不变量。
//
// 三个事实必须锁住，否则 deps 管理页的文案又会退化成一言 "unreachable"：
//
//  1. 客户端整体超时必须被识别成 **Timeout**（不是"连不上"）；
//  2. 5xx 重试耗尽后要带上游响应体（引擎的原因），而不是丢弃成 "server error 502"；
//  3. `WithoutCooldown` 的只读调用**不能**把地址打进冷却 —— 后台刷新一次依赖列表
//     不该让正在进行的对话在接下来 5s 里被轮转到一个被冷却的地址。

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestGetJSONClientTimeoutIsClassifiedAsTimeout(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		time.Sleep(200 * time.Millisecond)
		_, _ = w.Write([]byte(`{}`))
	}))
	defer srv.Close()

	c := NewPythonClientWithTimeout(40*time.Millisecond, srv.URL)
	var out map[string]any
	err := c.GetJSON(t.Context(), "/internal/deps/list", &out)

	if err == nil {
		t.Fatal("期望超时错误")
	}
	var te *TransportError
	if !errors.As(err, &te) {
		t.Fatalf("超时应是 TransportError，得到 %T: %v", err, err)
	}
	if !IsTimeout(err) {
		t.Fatalf("IsTimeout 应为 true：%v", err)
	}
	if got := FailureKindOf(err); got != FailureTimeout {
		t.Fatalf("FailureKindOf = %v，期望 FailureTimeout", got)
	}
}

// 5xx 重试耗尽：错误里必须带引擎自己给的 body（否则上层只能猜）。
func TestServerErrorKeepsUpstreamBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = w.Write([]byte(`{"detail":{"error":"pip list timed out after 30s"}}`))
	}))
	defer srv.Close()

	c := NewPythonClient(srv.URL)
	var out map[string]any
	err := c.GetJSON(t.Context(), "/internal/deps/list", &out)

	var up *UpstreamError
	if !errors.As(err, &up) {
		t.Fatalf("应是 UpstreamError，得到 %T: %v", err, err)
	}
	if up.Status != http.StatusBadGateway {
		t.Fatalf("状态码 = %d，期望 502", up.Status)
	}
	if got := FailureKindOf(err); got != FailureUpstream {
		t.Fatalf("FailureKindOf = %v，期望 FailureUpstream", got)
	}
	var payload map[string]any
	if err := json.Unmarshal([]byte(up.Body), &payload); err != nil {
		t.Fatalf("上游 body 未保留：%q", up.Body)
	}
}

// WithoutRetry：只发一次请求（重试只会把 pip 的慢/失败放大 4 倍）。
func TestWithoutRetrySendsOnce(t *testing.T) {
	var hits int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits++
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := NewPythonClient(srv.URL)
	var out map[string]any
	_ = c.GetJSON(t.Context(), "/internal/deps/list", &out, WithoutRetry(), WithoutCooldown())

	if hits != 1 {
		t.Fatalf("上游被打了 %d 次，期望 1 次", hits)
	}
}

// WithoutCooldown：只读探针失败后，地址**不得**进入冷却（否则拖累对话流量）。
func TestProbeFailureDoesNotTripAddressCooldown(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := NewPythonClient(srv.URL)
	var out map[string]any
	_ = c.GetJSON(t.Context(), "/internal/deps/list", &out, WithoutRetry(), WithoutCooldown())

	entries := c.snapshot()
	if len(entries) != 1 {
		t.Fatalf("地址表长度 = %d，期望 1", len(entries))
	}
	if entries[0].cooldownUntil != 0 {
		t.Fatalf("只读探针不该让地址冷却：cooldownUntil=%d（now=%d）",
			entries[0].cooldownUntil, time.Now().Unix())
	}

	// 对照组：同一地址走普通路径（会更新熔断）时必须被冷却 —— 否则上面那条断言
	// 可能因为 markFailure 根本没工作而"假通过"。
	_ = c.GetJSON(t.Context(), "/internal/deps/list", &out, WithoutRetry())
	if got := c.snapshot()[0].cooldownUntil; got == 0 {
		t.Fatal("对照组：普通调用的失败必须让地址进入冷却")
	}
}

// 连接失败 → FailureUnreachable（只有这一类才配说 "unreachable"）。
func TestConnectionRefusedIsUnreachable(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	dead := srv.URL
	srv.Close()

	c := NewPythonClientWithTimeout(2*time.Second, dead)
	var out map[string]any
	err := c.GetJSON(t.Context(), "/internal/deps/list", &out)

	if err == nil {
		t.Fatal("期望连接失败")
	}
	if got := FailureKindOf(err); got != FailureUnreachable {
		t.Fatalf("FailureKindOf = %v（%v），期望 FailureUnreachable", got, err)
	}
}
