package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/engine"
)

// gateway_breaker_test.go —— `/v1/admin/gateway/breakers` 与 `reset-breaker`。
//
// 这两个端点补的是**路径**而不是能力：引擎侧熔断与重置早已实现，但网关从未转发，
// 而前端只认网关 ⇒ 运维实际上调不到。所以这里钉住的是三件事：
//   1. chat 与 embed 是两张独立表，读数不能只取一半；
//   2. 引擎不可达时回 502，**不能**回空列表（两者在排障页上结论相反）；
//   3. 重置是转发，不是网关本地记账（熔断表在引擎进程里，本地"已恢复"只会与真实判定错位）。

// breakerEngine 起一个假引擎，返回 /info 与 reset-breaker 的固定响应。
func breakerEngine(t *testing.T, info string, resetStatus int) (*AdminHandler, *[]string) {
	t.Helper()
	var calls []string

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls = append(calls, r.Method+" "+r.URL.Path)
		switch r.URL.Path {
		case "/info":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(info))
		case "/v1/gateway/reset-breaker":
			if resetStatus >= 400 {
				w.WriteHeader(resetStatus)
				_, _ = w.Write([]byte(`{"status":"error","message":"provider not found"}`))
				return
			}
			var body map[string]string
			_ = json.NewDecoder(r.Body).Decode(&body)
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"status":"ok","provider":"` + body["provider"] + `"}`))
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	t.Cleanup(srv.Close)

	h := NewAdminHandler(nil, nil, nil, nil, engine.NewPythonClient(srv.URL))
	return h, &calls
}

// infoWith 造一份 /info 响应：chat 与 embed 给不同的状态。
func infoWith(chat, embed string) string {
	return `{"version":"test","gateway":{"providers":{
		"openai":{"circuit_state":"` + chat + `","embed_circuit_state":"` + embed + `","avg_latency_ms":123.456},
		"deepseek":{"circuit_state":"closed","embed_circuit_state":"closed","avg_latency_ms":0}
	}}}`
}

func TestGatewayBreakersSeparatesChatAndEmbed(t *testing.T) {
	// chat 正常、embed 熔断 —— 只读 circuit_state 的话这行会显示"健康"，
	// 而 embedding 实际已被判死，这正是"聊天能回、知识库检索恒空"的现场。
	h, _ := breakerEngine(t, infoWith("closed", "open"), 0)

	rec := httptest.NewRecorder()
	h.GetGatewayBreakers(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/gateway/breakers", nil))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}

	var resp struct {
		Data struct {
			Breakers []GatewayBreaker `json:"breakers"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("decode: %v; body=%s", err, rec.Body.String())
	}

	if len(resp.Data.Breakers) != 2 {
		t.Fatalf("breakers = %d, want 2; body=%s", len(resp.Data.Breakers), rec.Body.String())
	}
	// 排序后第一个必是 deepseek（字典序），第二个 openai —— 顺序稳定，前端行序才不会每次刷新都变。
	if resp.Data.Breakers[0].Provider != "deepseek" || resp.Data.Breakers[1].Provider != "openai" {
		t.Fatalf("providers not sorted: %+v", resp.Data.Breakers)
	}

	openai := resp.Data.Breakers[1]
	if openai.Chat != "closed" {
		t.Errorf("chat = %q, want closed", openai.Chat)
	}
	if openai.Embed != "open" {
		t.Errorf("embed = %q, want open（embed 熔断必须与 chat 分开可见）", openai.Embed)
	}
	if openai.AvgLatencyMs != 123.46 {
		t.Errorf("avg_latency_ms = %v, want 123.46（保留两位）", openai.AvgLatencyMs)
	}
}

func TestGatewayBreakersEngineUnreachableIs502(t *testing.T) {
	// 指向一个已关闭的端口：fetchPyEngineInfo 拿不到 → 必须 502。
	// 回空列表会被读成"没有 provider 熔断"，而实际是"读不到"。
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	dead := srv.URL
	srv.Close()
	h := NewAdminHandler(nil, nil, nil, nil, engine.NewPythonClient(dead))

	rec := httptest.NewRecorder()
	h.GetGatewayBreakers(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/gateway/breakers", nil))

	if rec.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502（不能是 200 + 空列表）; body=%s", rec.Code, rec.Body.String())
	}
}

func TestGatewayBreakersWithoutEngineIs500(t *testing.T) {
	h := NewAdminHandler(nil, nil, nil, nil, nil)
	rec := httptest.NewRecorder()
	h.GetGatewayBreakers(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/gateway/breakers", nil))

	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500", rec.Code)
	}
}

func TestResetGatewayBreakerForwardsToEngine(t *testing.T) {
	h, calls := breakerEngine(t, infoWith("open", "open"), 0)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/gateway/reset-breaker",
		strings.NewReader(`{"provider":"openai"}`))
	h.ResetGatewayBreaker(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}

	forwarded := false
	for _, c := range *calls {
		if c == "POST /v1/gateway/reset-breaker" {
			forwarded = true
		}
	}
	if !forwarded {
		t.Fatalf("未转发到引擎：calls = %v", *calls)
	}
}

func TestResetGatewayBreakerUnknownProviderIs404(t *testing.T) {
	// 引擎对未知 provider 回 404。必须透传成 404：502 会被读成"引擎挂了"，
	// 而实际只是名字打错，处置完全不同。
	h, _ := breakerEngine(t, infoWith("closed", "closed"), http.StatusNotFound)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/v1/admin/gateway/reset-breaker",
		strings.NewReader(`{"provider":"nope"}`))
	h.ResetGatewayBreaker(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404; body=%s", rec.Code, rec.Body.String())
	}
}

func TestResetGatewayBreakerRequiresProvider(t *testing.T) {
	h, calls := breakerEngine(t, infoWith("closed", "closed"), 0)

	for _, body := range []string{`{}`, `{"provider":""}`, `{"provider":"   "}`} {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "/v1/admin/gateway/reset-breaker", strings.NewReader(body))
		h.ResetGatewayBreaker(rec, req)

		if rec.Code != http.StatusBadRequest {
			t.Errorf("body=%s: status = %d, want 400", body, rec.Code)
		}
	}
	// 空 provider 不该打到引擎：否则会变成"重置了一个叫空字符串的 provider"。
	if len(*calls) != 0 {
		t.Errorf("空 provider 仍请求了引擎：calls = %v", *calls)
	}
}
