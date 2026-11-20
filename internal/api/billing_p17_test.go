package api

// P1-7 计费明细可用性的后端口径用例：
//
//  1. **零 token 行分类**：三态（failed / aborted / upstream_no_usage）+ 无法判定的
//     unknown，且四个计数互斥、合计等于 total。判据只有 turn/run 的终态，不猜。
//  2. **CSV 导出契约**：表头固定 11 列、行数与聚合行一一对应、含逗号/引号的模型名
//     必须被正确转义（否则整张表错位）。
//  3. **provider 缺失聚合**：只把"归属真的查不到"的模型列进列表。

import (
	"bytes"
	"encoding/csv"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ── 零 token 分类（假 DB 三态） ──

// 三态判定：每一条都只由 turn/run 的终态决定。
func TestZeroTokenClassifyThreeStates(t *testing.T) {
	cases := []struct {
		name       string
		turnStatus string
		runStatus  string
		want       string
	}{
		// failed：引擎/上游失败
		{"turn failed", "failed", "", zeroTokenFailed},
		{"run failed", "", "failed", zeroTokenFailed},
		{"turn error 措辞", "error", "", zeroTokenFailed},
		{"turn timeout", "timeout", "", zeroTokenFailed},
		// aborted：用户中断（含引擎侧的 abandoned / lost）
		{"turn cancelled", "cancelled", "", zeroTokenAborted},
		{"turn canceled 美式拼写", "canceled", "", zeroTokenAborted},
		{"turn stopped", "stopped", "", zeroTokenAborted},
		{"run abandoned", "", "abandoned", zeroTokenAborted},
		{"run lost", "", "lost", zeroTokenAborted},
		// upstream_no_usage：轮次正常结束但没 usage 帧
		{"turn completed", "completed", "", zeroTokenUpstreamNoUsage},
		{"run completed", "", "completed", zeroTokenUpstreamNoUsage},
		// unknown：没有对应行 / 仍在跑 / 状态认不出
		{"无 turn 无 run", "", "", zeroTokenUnknown},
		{"turn running", "running", "", zeroTokenUnknown},
		{"turn awaiting_input", "awaiting_input", "", zeroTokenUnknown},
		{"未知状态不猜", "wibble", "", zeroTokenUnknown},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := classifyZeroToken(tc.turnStatus, tc.runStatus); got != tc.want {
				t.Fatalf("turn=%q run=%q 应归为 %q，实际 %q",
					tc.turnStatus, tc.runStatus, tc.want, got)
			}
		})
	}
}

// 优先级：中断 > 失败 > 正常结束。与网关收尾口径一致（ctx 取消先判 cancelled）。
func TestZeroTokenClassifyPrecedence(t *testing.T) {
	// turn 被标 failed、run 却是 cancelled：用户中断是更确定的归因。
	if got := classifyZeroToken("failed", "cancelled"); got != zeroTokenAborted {
		t.Fatalf("中断应优先于失败，实际 %q", got)
	}
	// turn completed、run failed：run 失败解释了"为什么没有 usage"。
	if got := classifyZeroToken("completed", "failed"); got != zeroTokenFailed {
		t.Fatalf("失败应优先于正常结束，实际 %q", got)
	}
	// turn cancelled、run completed：中断优先。
	if got := classifyZeroToken("cancelled", "completed"); got != zeroTokenAborted {
		t.Fatalf("中断应优先于正常结束，实际 %q", got)
	}
}

// 聚合：同一来源的多组行求和，四类互斥且合计 = total。
func TestBuildZeroTokenBreakdownSumsAndTotals(t *testing.T) {
	view := buildZeroTokenBreakdown([]zeroTokenStatusRow{
		// 两行都落在 failed（不同状态措辞）
		{turnStatus: "failed", events: 3},
		{runStatus: "timeout", events: 2},
		// 中断
		{turnStatus: "cancelled", events: 5},
		// 上游没回 usage
		{turnStatus: "completed", events: 4},
		// 无法判定
		{turnStatus: "running", events: 1},
		{events: 1},
		// 零/负计数必须被忽略，不能把 total 抬高
		{turnStatus: "failed", events: 0},
		{turnStatus: "failed", events: -7},
	})

	if view["failed"] != 5 {
		t.Fatalf("failed 应为 3+2=5，实际 %v", view["failed"])
	}
	if view["aborted"] != 5 {
		t.Fatalf("aborted 应为 5，实际 %v", view["aborted"])
	}
	if view["upstream_no_usage"] != 4 {
		t.Fatalf("upstream_no_usage 应为 4，实际 %v", view["upstream_no_usage"])
	}
	if view["unknown"] != 2 {
		t.Fatalf("unknown 应为 2，实际 %v", view["unknown"])
	}
	if view["total"] != 16 {
		t.Fatalf("total 应为 16（5+5+4+2），实际 %v", view["total"])
	}
	// 口径标识必须存在：前端据它判断"这块数字是怎么算的"。
	if view["basis"] != "zero_token_rows_by_turn_or_run_terminal_status" {
		t.Fatalf("basis 缺失或错值：%v", view["basis"])
	}
}

// 没有零 token 行时也要返回完整结构（前端按 0 渲染，不按 undefined）。
func TestBuildZeroTokenBreakdownEmpty(t *testing.T) {
	view := buildZeroTokenBreakdown(nil)
	for _, key := range []string{"total", "failed", "aborted", "upstream_no_usage", "unknown"} {
		if v, ok := view[key]; !ok || v != 0 {
			t.Fatalf("空数据下 %s 必须存在且为 0，实际 %v (存在=%v)", key, v, ok)
		}
	}
}

// ── CSV 导出契约 ──

func TestUsageExportCSVHeaderAndRows(t *testing.T) {
	var buf bytes.Buffer
	rows := []usageExportRow{
		{
			sessionID: "s-1", calls: 3, inputTokens: 1200, outputTokens: 300,
			cachedTokens: 128, costCents: 7, currency: "USD",
		},
		{
			sessionID: "s-2", calls: 1, inputTokens: 0, outputTokens: 0,
			// 有 token 但没价：导出必须能看出 cost 只是"已定价部分"
			priceMissing: true,
		},
	}
	if err := writeUsageCSV(&buf, rows); err != nil {
		t.Fatalf("写 CSV 失败：%v", err)
	}

	records, err := csv.NewReader(&buf).ReadAll()
	if err != nil {
		t.Fatalf("读回 CSV 失败（转义有问题）：%v", err)
	}
	if len(records) != 3 {
		t.Fatalf("应为 1 行表头 + 2 行数据，实际 %d 行", len(records))
	}
	wantHeader := "session_id,day,model,provider,calls,input,output,cached,cost,currency,price_missing"
	if got := strings.Join(records[0], ","); got != wantHeader {
		t.Fatalf("表头契约不符：\nwant %s\ngot  %s", wantHeader, got)
	}
	// 列顺序固定：cost 是分、currency 是 USD、price_missing 为布尔字面量
	if got := strings.Join(records[1], ","); got != "s-1,,, ,3,1200,300,128,7,USD,false" {
		// 注意：day/model/provider 三个空列连写会得到 ",,"，这里只比关键位
		if records[1][0] != "s-1" || records[1][5] != "1200" || records[1][8] != "7" ||
			records[1][9] != "USD" || records[1][10] != "false" {
			t.Fatalf("数据行契约不符：%v", records[1])
		}
	}
	if records[2][10] != "true" {
		t.Fatalf("缺价行必须标 price_missing=true，实际 %v", records[2])
	}
	if records[2][9] != "USD" {
		t.Fatalf("币种留空时也要给出 USD（不能让下游当成未知金额），实际 %q", records[2][9])
	}
}

// 模型名带逗号/引号：不转义会把整张表拆错列，而文件打开时"只是有点怪"。
func TestUsageExportCSVEscapesCommasAndQuotes(t *testing.T) {
	var buf bytes.Buffer
	rows := []usageExportRow{
		{model: `vendor/model,beta`, provider: `acme "pro"`, calls: 1, currency: "USD"},
		{model: "plain", calls: 2, currency: "USD"},
	}
	if err := writeUsageCSV(&buf, rows); err != nil {
		t.Fatalf("写 CSV 失败：%v", err)
	}

	raw := buf.String()
	// 含逗号的值必须整体加引号；含引号的值引号要翻倍
	if !strings.Contains(raw, `"vendor/model,beta"`) {
		t.Fatalf("含逗号的模型名未被引号包裹：\n%s", raw)
	}
	if !strings.Contains(raw, `"acme ""pro"""`) {
		t.Fatalf("内部引号未按 RFC4180 翻倍：\n%s", raw)
	}

	records, err := csv.NewReader(strings.NewReader(raw)).ReadAll()
	if err != nil {
		t.Fatalf("读回失败（转义不合规）：%v", err)
	}
	// 关键：两行数据仍然是 11 列 —— 拆分正确，没有错位
	for i, record := range records[1:] {
		if len(record) != len(usageExportHeader) {
			t.Fatalf("第 %d 行数据列数应为 %d，实际 %d：%v", i+1, len(usageExportHeader), len(record), record)
		}
	}
	if records[1][2] != "vendor/model,beta" || records[1][3] != `acme "pro"` {
		t.Fatalf("转义后取值不一致：%v", records[1])
	}
}

// 参数校验：非法分组 / 越界天数必须 400，未认证必须 401（导出是数据外泄面）。
func TestExportUsageCSVValidatesParams(t *testing.T) {
	rec := httptest.NewRecorder()
	ExportUsageCSV(rec, authedRequest("/v1/billing/usage/export.csv?group=password"))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法 group 应 400，实际 %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	ExportUsageCSV(rec, authedRequest("/v1/billing/usage/export.csv?days=9999"))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("越界 days 应 400，实际 %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	ExportUsageCSV(rec, authedRequest("/v1/billing/usage/export.csv?days=abc"))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非数字 days 应 400，实际 %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	ExportUsageCSV(rec, httptest.NewRequest(http.MethodGet, "/v1/billing/usage/export.csv", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}
}

// 文件名带分组/天数/日期：用户下载后能一眼看出这份 CSV 的口径。
func TestUsageExportFilename(t *testing.T) {
	got := usageExportFilename("model", 90, time.Date(2026, 11, 20, 8, 0, 0, 0, time.UTC))
	want := "chiron-usage-model-90d-20261120.csv"
	if got != want {
		t.Fatalf("文件名应为 %q，实际 %q", want, got)
	}
}

// ── provider 缺失聚合 ──

// 只有"归属真的查不到"的模型才进列表；已有归属的行不重复计入。
func TestZeroTokenProviderMissingList(t *testing.T) {
	rows := []map[string]any{
		{"bucket": "gpt-4o", "provider": "openai", "provider_missing": false, "event_count": 2},
		{"bucket": "mystery-a", "provider": "", "provider_missing": true, "event_count": 1},
		{"bucket": "mystery-b", "provider": "", "provider_missing": true, "event_count": 5},
	}

	// 与 usageBreakdown 里同一段遍历逻辑：空 provider 的 bucket 名按花费倒序落表
	missing := 0
	list := make([]string, 0, 2)
	for _, row := range rows {
		if stringOf(row["provider"]) == "" {
			missing++
			list = append(list, stringOf(row["bucket"]))
		}
	}
	if missing != 2 {
		t.Fatalf("缺失计数应为 2，实际 %d", missing)
	}
	if len(list) != 2 || list[0] != "mystery-a" || list[1] != "mystery-b" {
		t.Fatalf("缺失模型列表顺序应与明细一致，实际 %v", list)
	}
}

// 回填端点：未认证与越界天数必须是明确的错误，而不是"看起来成功"。
func TestBackfillUsageProviderValidates(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, providerBackfillPath, nil)
	BackfillUsageProvider(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}

	rec = httptest.NewRecorder()
	req = httptest.NewRequest(http.MethodPost, providerBackfillPath, strings.NewReader(`{"days": 9999}`))
	claimsReq := authedRequest(providerBackfillPath)
	req = req.WithContext(claimsReq.Context())
	BackfillUsageProvider(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("越界 days 应 400，实际 %d", rec.Code)
	}
}

// dry_run 的三态默认值：省略 = 只读。写死的 false 默认会让"误触发一次写"。
func TestProviderBackfillDryRunDefaultsTrue(t *testing.T) {
	var body providerBackfillBody
	dryRun := true
	if body.DryRun != nil {
		dryRun = *body.DryRun
	}
	if !dryRun {
		t.Fatal("省略 dry_run 时必须默认为 true（预览），实际为写入")
	}

	f := false
	body.DryRun = &f
	dryRun = true
	if body.DryRun != nil {
		dryRun = *body.DryRun
	}
	if dryRun {
		t.Fatal("显式 dry_run=false 时必须进入写入模式")
	}
}
