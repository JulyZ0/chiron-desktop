package api

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

// 计费页聚合的**口径**（不是取数实现）。
//
// 用户看到的形态：近 90 天消耗全为 0。这里钉住两件事：
//
//  1. 按天视图**必须同时给出 token**。此前它只 SUM `cost_cents`，于是"算不出花费"
//     （模型名缺失 ⇒ 无单价 ⇒ 列 NULL）时整页只有一串 0，而 token 其实好好地躺在表里
//     —— 页面无法自证"用量确实被记下来了"。
//  2. `zero_token_events` 要能被算出来：升级前的历史行**没有 token 明细**，
//     这部分只能标注、不能回填（回填等于编数据）。

func TestBuildDailyViewSumsTokensAndCost(t *testing.T) {
	today := time.Date(2026, 11, 20, 8, 0, 0, 0, time.UTC)
	rows := []usageDayRow{
		{day: "2026-11-18", events: 2, costCents: 7, inputTokens: 1000, outputTokens: 200, cachedTokens: 300},
		{day: "2026-11-20", events: 1, costCents: 0, inputTokens: 50, outputTokens: 10},
	}

	view := buildDailyView(rows, 3, today)

	daily := view["daily"].([]map[string]any)
	if len(daily) != 3 {
		t.Fatalf("应补成 3 天，实际 %d", len(daily))
	}
	if view["total_input"] != 1050 || view["total_output"] != 210 || view["total_cached"] != 300 {
		t.Fatalf("token 合计应为 1050/210/300，实际 %v/%v/%v",
			view["total_input"], view["total_output"], view["total_cached"])
	}
	if view["total_spent"] != 7 {
		t.Fatalf("区间花费应为 7 分，实际 %v", view["total_spent"])
	}
	if view["total_calls"] != 3 {
		t.Fatalf("记账行数应为 3，实际 %v", view["total_calls"])
	}
	// 每一天都要带 token 键：缺键会让前端读到 undefined 而不是 0
	for _, point := range daily {
		if _, ok := point["input_tokens"]; !ok {
			t.Fatalf("每天的序列点必须带 input_tokens：%v", point)
		}
	}
	// 空白天补零，且不能混进合计
	if daily[1]["input_tokens"] != 0 || daily[1]["credits_spent"] != 0 {
		t.Fatalf("空白天应补零，实际 %v", daily[1])
	}
	// 调用次数口径必须随响应一起下发，否则"9 次调用"这类疑问只能靠猜
	if view["count_basis"] != "usage_rows" {
		t.Fatalf("count_basis 应说明记账行口径，实际 %v", view["count_basis"])
	}
}

func TestBuildDailyViewCountsLegacyZeroTokenRows(t *testing.T) {
	today := time.Date(2026, 11, 20, 0, 0, 0, 0, time.UTC)
	rows := []usageDayRow{
		// 升级前的历史行：有调用、无 token 明细
		{day: "2026-11-19", events: 5, costCents: 0, zeroTokenEvents: 5},
	}

	view := buildDailyView(rows, 2, today)

	if view["zero_token_events"] != 5 {
		t.Fatalf("应统计出 5 条无 token 明细的历史行，实际 %v", view["zero_token_events"])
	}
}

// 统一链路（`/v1/chat/submit`）的回包记账：此前**一行都不写**。
func TestRecordUnifiedUsageFromResponseWritesRow(t *testing.T) {
	capture := captureUsageEvents(t)

	body := map[string]any{"session_id": "us-1", "llm_config": map[string]any{"model": "gpt-4o"}}
	resp := map[string]any{
		"success":  true,
		"task_id":  "task-9",
		"trace_id": "trace-9",
		"usage": map[string]any{
			"model":         "gpt-4o",
			"input_tokens":  float64(1200),
			"output_tokens": float64(300),
			"cached_tokens": float64(128),
		},
	}

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-9", TenantID: 3}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, body, resp)

	rows := capture.snapshot()
	if len(rows) != 1 {
		t.Fatalf("统一链路的一次调用必须写 1 行，实际 %d 行", len(rows))
	}
	r := rows[0]
	if r.SessionID != "us-1" || r.TurnID != "task-9" {
		t.Fatalf("会话/回合归属错了：%q / %q", r.SessionID, r.TurnID)
	}
	if r.UserID != "u-9" || r.TenantID != 3 {
		t.Fatalf("归属必须取自 claims（不可信回包），实际 %q / %d", r.UserID, r.TenantID)
	}
	if r.InputTokens != 1200 || r.OutputTokens != 300 || r.CachedTokens != 128 {
		t.Fatalf("token 必须落到行上，实际 %+v", r)
	}
	if r.Model != "gpt-4o" {
		t.Fatalf("model 必须落到行上（价格查询键），实际 %q", r.Model)
	}
}

// 引擎把 usage 放在 `metadata.usage` 下（另一种真实形状）时同样要认。
func TestRecordUnifiedUsageReadsMetadataUsage(t *testing.T) {
	capture := captureUsageEvents(t)
	resp := map[string]any{
		"success": true,
		"metadata": map[string]any{
			"usage": map[string]any{"model": "qwen-plus", "input_tokens": float64(80), "output_tokens": float64(8)},
		},
	}
	body := map[string]any{"session_id": "us-2"}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-2", TenantID: 1}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, body, resp)

	rows := capture.snapshot()
	if len(rows) != 1 || rows[0].InputTokens != 80 || rows[0].OutputTokens != 8 {
		t.Fatalf("metadata.usage 形状必须被识别，实际 %+v", rows)
	}
}

// 完全没有用量字段（老引擎）：仍然记一次调用，但 token 必须是 0 —— 不许编。
func TestRecordUnifiedUsageKeepsCallWithoutTokens(t *testing.T) {
	capture := captureUsageEvents(t)
	resp := map[string]any{"success": true, "task_id": "task-x"}
	body := map[string]any{"session_id": "us-3", "llm_config": map[string]any{"model": "gpt-4o-mini"}}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-3", TenantID: 1}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, body, resp)

	rows := capture.snapshot()
	if len(rows) != 1 {
		t.Fatalf("没有 usage 字段也代表发生过一次调用，应写 1 行，实际 %d 行", len(rows))
	}
	if rows[0].InputTokens != 0 || rows[0].OutputTokens != 0 {
		t.Fatalf("不许给无明细的调用编 token，实际 %+v", rows[0])
	}
	if rows[0].Model != "gpt-4o-mini" {
		t.Fatalf("模型名应回落到请求里的 llm_config.model，实际 %q", rows[0].Model)
	}
}

// 连模型名都没有、也没有用量：不写"全空行"污染统计基数。
func TestRecordUnifiedUsageSkipsNothingAtAll(t *testing.T) {
	capture := captureUsageEvents(t)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-4", TenantID: 1}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, map[string]any{}, map[string]any{"success": true})

	if rows := capture.snapshot(); len(rows) != 0 {
		t.Fatalf("无模型、无用量时不该写行，实际 %+v", rows)
	}
}

// 只有 cached 命中的调用是真事实，不能被 RecordUsageEvent 的空行守卫丢掉。
func TestRecordUsageEventKeepsCachedOnlyRows(t *testing.T) {
	capture := captureUsageEvents(t)
	RecordUsageEvent(context.Background(), UsageEvent{SessionID: "s", CachedTokens: 512})
	if rows := capture.snapshot(); len(rows) != 1 || rows[0].CachedTokens != 512 {
		t.Fatalf("只有缓存命中的行必须落库，实际 %+v", rows)
	}
}
