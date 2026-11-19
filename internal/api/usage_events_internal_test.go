package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

// TestIngestUsageEventsRejectsMissingIdentity 固定住"归属不来自请求体"的第一道门。
//
// 缺陷形态（这条通道存在之前的现实）：接管 run 由引擎自己起，事件流不经过网关的
// SSE 消费段，`usage_events` 里永远没有它的行 —— 计费页对巡检花的钱恒为 0。
// 补通道时最容易走的歪路是"让引擎在请求体里带 tenant_id"，那等于把"这笔账算谁的"
// 交给调用方。所以这里钉住：没有 session_id / turn_id 直接 400，归属只按会话行反查。
func TestIngestUsageEventsRejectsMissingIdentity(t *testing.T) {
	for name, body := range map[string]string{
		"no session": `{"turn_id":"takeover_ab","entries":[{"model":"m","input_tokens":1}]}`,
		"no turn":    `{"session_id":"s-1","entries":[{"model":"m","input_tokens":1}]}`,
	} {
		req := httptest.NewRequest(http.MethodPost, "/v1/internal/usage-events", strings.NewReader(body))
		rec := httptest.NewRecorder()
		IngestUsageEvents(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s: 期望 400，得到 %d：%s", name, rec.Code, rec.Body.String())
		}
	}
}

// TestIngestUsageEventsEmptyEntriesIsSuccess 钉住"巡检了但无事可做"是成功而非错误。
//
// 零用量 run 是**合法事实**。把它判成 400 会让引擎侧的上报循环把每一拍"无事可做"
// 都记成失败，进而干扰接管自身的失败计数与熔断判定。
func TestIngestUsageEventsEmptyEntriesIsSuccess(t *testing.T) {
	body := `{"session_id":"s-1","turn_id":"takeover_ab","entries":[]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/usage-events", strings.NewReader(body))
	rec := httptest.NewRecorder()
	IngestUsageEvents(rec, req)
	if rec.Code == http.StatusBadRequest {
		t.Fatalf("零用量上报被判 400：%s", rec.Body.String())
	}
}

// TestIngestUsageEventsRejectsOversizedBatch 钉住写入放大上限。
//
// 上限存在是因为这个端点用的是内部令牌 —— 一旦某个调用点写错（例如把整条事件的
// token 列表当 entries 传），没有上限就是一次几十万行的插入。
func TestIngestUsageEventsRejectsOversizedBatch(t *testing.T) {
	var sb strings.Builder
	sb.WriteString(`{"session_id":"s-1","turn_id":"t-1","entries":[`)
	for i := 0; i < ingestUsageEntriesMax+1; i++ {
		if i > 0 {
			sb.WriteByte(',')
		}
		sb.WriteString(`{"model":"m","input_tokens":1}`)
	}
	sb.WriteString(`]}`)
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/usage-events", strings.NewReader(sb.String()))
	rec := httptest.NewRecorder()
	IngestUsageEvents(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("超量上报未被拦住：%d %s", rec.Code, rec.Body.String())
	}
}

// TestIngestUsageEventsUnavailableWithoutDB 钉住"库不可用是 503 而不是静默成功"。
//
// 静默成功会让引擎侧以为自己报上去了，于是这一拍的用量永远消失且无人知晓；
// 503 让上报方至少能记一条告警。
func TestIngestUsageEventsUnavailableWithoutDB(t *testing.T) {
	if db.GlobalDBManager != nil {
		t.Skip("本用例只在无数据库连接的单元测试环境有意义")
	}
	body := `{"session_id":"s-1","turn_id":"takeover_ab","entries":[{"model":"m","input_tokens":1}]}`
	req := httptest.NewRequest(http.MethodPost, "/v1/internal/usage-events", strings.NewReader(body))
	rec := httptest.NewRecorder()
	IngestUsageEvents(rec, req)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("库不可用时期望 503，得到 %d：%s", rec.Code, rec.Body.String())
	}
}
