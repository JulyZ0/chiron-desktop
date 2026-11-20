package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

// 计费补齐（P1-9）的口径用例：
//
//  1. **缺价显示"未知"而不是 0**：`cost_cents IS NULL` ⇒ 派生 `price_missing`，
//     金额列仍回"已定价部分之和"，两者靠 `priced_calls` / `unpriced_calls` 分辨。
//  2. **auto 链路（TaskRouter）的用量**：引擎回包带 `usage` 时网关必须记账；
//     没带时必须只记调用、并在事件上标注 `usage_missing`（不许编 0 token 冒充）。
//  3. **价格覆盖率**：只把"有 token 消耗且查不到单价"的模型算作缺口。

func TestBillingBreakdownDerivesPriceMissing(t *testing.T) {
	rows := buildBreakdownRows([]usageBucketRow{
		// 全部有价：不该标记缺价
		{bucket: "gpt-4o", events: 2, inputTokens: 100, outputTokens: 20, costCents: 7, pricedCalls: 2},
		// 全部无价：金额必须显示 "—"（前端据 price_missing 决定），不能是 0
		{bucket: "mystery-model", events: 3, inputTokens: 900, outputTokens: 100, unpricedCalls: 3},
		// 部分无价：金额是"已定价部分"，标记缺价让界面说明它不完整
		{bucket: "mixed", events: 4, inputTokens: 50, outputTokens: 5, costCents: 3, pricedCalls: 1, unpricedCalls: 3},
	})

	if len(rows) != 3 {
		t.Fatalf("应保留 3 行，实际 %d", len(rows))
	}
	if rows[0]["price_missing"] != false || rows[0]["unpriced_calls"] != 0 {
		t.Fatalf("有价行不该标记缺价：%v", rows[0])
	}
	if rows[1]["price_missing"] != true || rows[1]["unpriced_calls"] != 3 {
		t.Fatalf("无价行必须标记 price_missing，实际 %v", rows[1])
	}
	// 缺价行的金额仍是 0（占位），但缺价标记让前端显示 "—" 而不是 0
	if rows[1]["cost_cents"] != 0 {
		t.Fatalf("缺价行的 cost_cents 应是已定价部分（0），实际 %v", rows[1]["cost_cents"])
	}
	if rows[2]["price_missing"] != true || rows[2]["priced_calls"] != 1 || rows[2]["cost_cents"] != 3 {
		t.Fatalf("部分缺价行必须同时给出已定价金额与缺价标记，实际 %v", rows[2])
	}
}

func TestBillingDailyViewCountsUnpricedCalls(t *testing.T) {
	today := time.Date(2026, 11, 20, 8, 0, 0, 0, time.UTC)
	rows := []usageDayRow{
		{day: "2026-11-19", events: 3, costCents: 5, inputTokens: 300, outputTokens: 30, unpricedCalls: 1},
		// 纯历史零 token 行：不是缺价（没有金额可言），不能混进 unpriced
		{day: "2026-11-20", events: 2, zeroTokenEvents: 2},
	}

	view := buildDailyView(rows, 3, today)

	if view["total_unpriced_calls"] != 1 {
		t.Fatalf("缺价调用数应为 1，实际 %v", view["total_unpriced_calls"])
	}
	if view["price_missing"] != true {
		t.Fatalf("存在缺价行时汇总必须标记 price_missing，实际 %v", view["price_missing"])
	}
	daily := view["daily"].([]map[string]any)
	// 每一根柱子都要带这个键：缺键会让前端读到 undefined 而不是 false
	for _, point := range daily {
		if _, ok := point["unpriced_calls"]; !ok {
			t.Fatalf("每天的序列点必须带 unpriced_calls：%v", point)
		}
	}
}

func TestBillingPriceCoverageAggregates(t *testing.T) {
	rows := []coverageModelRow{
		{model: "unpriced-big", calls: 9, tokenCalls: 9, inputTokens: 900, outputTokens: 90, lastSeen: "2026-11-19"},
		{model: "gpt-4o", calls: 4, tokenCalls: 4, inputTokens: 400, outputTokens: 40, lastSeen: "2026-11-20"},
		// 零 token 的历史行：不计入覆盖率分母（它本来就没有金额）
		{model: "legacy-no-tokens", calls: 7, tokenCalls: 0, lastSeen: "2026-08-01"},
	}
	priced := map[string]bool{"gpt-4o": true}

	view := buildPriceCoverage(rows, func(model string) bool { return priced[model] }, 90)

	if view["total_models"] != 2 || view["priced_models"] != 1 || view["unpriced_models"] != 1 {
		t.Fatalf("覆盖率分母只算有 token 的模型：应为 2/1/1，实际 %v/%v/%v",
			view["total_models"], view["priced_models"], view["unpriced_models"])
	}
	coverage, _ := view["coverage"].(float64)
	if coverage != 0.5 {
		t.Fatalf("覆盖率应为 0.5，实际 %v", coverage)
	}
	unpriced := view["unpriced"].([]coverageView)
	if len(unpriced) != 1 || unpriced[0].Model != "unpriced-big" {
		t.Fatalf("未定价列表应只含 unpriced-big，实际 %+v", unpriced)
	}
	if unpriced[0].Calls != 9 || unpriced[0].LastSeen != "2026-11-19" {
		t.Fatalf("未定价条目必须带调用数与最近出现日期（决定补价优先级），实际 %+v", unpriced[0])
	}
	// 补价入口：前端据此跳 /billing/prices，后端给路径避免在视图里写死字符串
	if view["prices_entry_url"] != "/billing/prices" {
		t.Fatalf("应给出补价入口路径，实际 %v", view["prices_entry_url"])
	}
}

func TestBillingPriceCoverageEmptyIsNotZeroPercent(t *testing.T) {
	view := buildPriceCoverage(nil, func(string) bool { return false }, 90)

	if view["total_models"] != 0 {
		t.Fatalf("没有模型时应为 0，实际 %v", view["total_models"])
	}
	// 没有可定价的模型时 coverage=0 是数学事实，但 unpriced 必须为空 ——
	// 前端据此显示"没有需要定价的调用"，而不是"覆盖率 0%"的告警。
	if list, _ := view["unpriced"].([]coverageView); len(list) != 0 {
		t.Fatalf("无调用时未定价列表必须为空，实际 %+v", list)
	}
}

// auto 链路（TaskRouter）的回包形状：usage 在顶层（引擎 submit_task 的返回）。
func TestUsageUnifiedAutoPathRecordsUsage(t *testing.T) {
	capture := captureUsageEvents(t)
	body := map[string]any{
		"session_id": "us-auto-1",
		"llm_config": map[string]any{"model": "deepseek-chat"},
		"mode":       "auto",
	}
	// 引擎 TaskRouter 的 UsageAccumulator 产出的 block（含 calls 字段，网关忽略）
	resp := map[string]any{
		"success": true,
		"task_id": "task_auto_1",
		"usage": map[string]any{
			"model":         "deepseek-chat",
			"provider":      "deepseek",
			"input_tokens":  float64(1500),
			"output_tokens": float64(220),
			"cached_tokens": float64(64),
			"calls":         float64(2),
		},
		"usage_missing": false,
		"metadata":      map[string]any{"usage_missing": false},
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-auto", TenantID: 2}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, body, resp)

	rows := capture.snapshot()
	if len(rows) != 1 {
		t.Fatalf("auto 链路的一次回合必须写 1 行，实际 %d", len(rows))
	}
	r := rows[0]
	if r.InputTokens != 1500 || r.OutputTokens != 220 || r.CachedTokens != 64 {
		t.Fatalf("auto 链路的 token 必须落到行上，实际 %+v", r)
	}
	if r.Model != "deepseek-chat" || r.Provider != "deepseek" {
		t.Fatalf("模型/厂商必须落到行上（价格查询键），实际 %q / %q", r.Model, r.Provider)
	}
	if r.UsageMissing {
		t.Fatalf("带 usage 的回合不该标 usage_missing：%+v", r)
	}
}

// 无 usage（纯工具回合 / 老引擎）：只记调用，并在事件上标注 usage_missing。
func TestUsageUnifiedMarksUsageMissing(t *testing.T) {
	capture := captureUsageEvents(t)
	body := map[string]any{
		"session_id": "us-auto-2",
		"llm_config": map[string]any{"model": "gpt-4o-mini"},
		"mode":       "auto",
	}
	resp := map[string]any{
		"success":       true,
		"task_id":       "task_auto_2",
		"usage":         nil,
		"usage_missing": true,
		"metadata":      map[string]any{"usage_missing": true},
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-auto2", TenantID: 2}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, body, resp)

	rows := capture.snapshot()
	if len(rows) != 1 {
		t.Fatalf("无用量也必须记一次调用（调用次数是真实事实），实际 %d 行", len(rows))
	}
	if !rows[0].UsageMissing {
		t.Fatalf("事件必须标注 usage_missing:true，实际 %+v", rows[0])
	}
	if rows[0].InputTokens != 0 || rows[0].OutputTokens != 0 {
		t.Fatalf("不许给无明细的调用编 token，实际 %+v", rows[0])
	}
	if rows[0].Model != "gpt-4o-mini" {
		t.Fatalf("模型名应回落到请求里的 llm_config.model，实际 %q", rows[0].Model)
	}
}

// metadata 里的 usage_missing 也要认（引擎两处都放，老网关只读一处）。
func TestUsageUnifiedReadsMetadataUsageMissing(t *testing.T) {
	capture := captureUsageEvents(t)
	body := map[string]any{"session_id": "us-auto-3", "llm_config": map[string]any{"model": "qwen-plus"}}
	resp := map[string]any{
		"success":  true,
		"metadata": map[string]any{"usage_missing": true},
	}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/submit", nil)
	claims := &auth.Claims{UserID: "u-auto3", TenantID: 2}
	req = req.WithContext(auth.WithClaims(req.Context(), claims))

	recordUnifiedUsageFromResponse(req, claims, body, resp)

	rows := capture.snapshot()
	if len(rows) != 1 || !rows[0].UsageMissing {
		t.Fatalf("metadata.usage_missing 必须被识别，实际 %+v", rows)
	}
}
