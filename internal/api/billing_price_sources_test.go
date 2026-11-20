package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

// 价格源健康（P0-4）的口径用例。
//
// 要钉住的三件事：
//  1. **三态可分**：`failed`（源不可达）与 `empty`（源活着但对不上模型名）是两种
//     完全不同的故障，混成一个"没数据"会让排查方向从"换源"变成"查网络"。
//  2. **失败原因可见但脱敏**：pricetoken 的 401/超时必须出现在面板上，且 200 字符截断
//     （长文本会把表格撑坏，也把凭证留在界面里）。
//  3. **stale 口径**：只有**全部源**都超过 2 小时没更新，才判定同步循环停了；
//     单个源过期可能只是它被移出目录。

func sampleSourceRows(now time.Time) []priceSourceRow {
	return []priceSourceRow{
		{
			sourceKey: "pricetoken", parser: "pricetoken", urlHost: "pricetoken.ai",
			status: "failed", rows: 0, durationMS: 812,
			// 写侧已脱敏，这里是"老行/手工写入"的超长文本再截一次
			error:     "ReadTimeout: https://pricetoken.ai/api/v1/text?api_key=*** timed out",
			checkedAt: now.Add(-5 * time.Minute),
		},
		{
			sourceKey: "portkey:openai", parser: "portkey", provider: "openai",
			urlHost: "configs.portkey.ai", status: "ok", rows: 42, durationMS: 120,
			checkedAt: now.Add(-3 * time.Minute),
		},
		{
			sourceKey: "llmrates", parser: "llmrates", urlHost: "www.llmrates.ai",
			status: "empty", rows: 0, durationMS: 90,
			checkedAt: now.Add(-4 * time.Minute),
		},
	}
}

func TestBuildPriceSourceReportSplitsStates(t *testing.T) {
	now := time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)
	report := buildPriceSourceReport(sampleSourceRows(now), now)

	sources, ok := report["sources"].([]priceSourceView)
	if !ok || len(sources) != 3 {
		t.Fatalf("应有 3 个源，实际 %#v", report["sources"])
	}
	// 排序按 source_key，便于界面稳定（portkey 的 4 个文件不会跳位）
	if sources[0].Source != "llmrates" || sources[1].Source != "portkey:openai" || sources[2].Source != "pricetoken" {
		t.Fatalf("源顺序应按 source_key 排序，实际 %q/%q/%q",
			sources[0].Source, sources[1].Source, sources[2].Source)
	}

	summary := report["summary"].(map[string]any)
	if summary["total"] != 3 || summary["ok"] != 1 || summary["empty"] != 1 || summary["failed"] != 1 {
		t.Fatalf("三态计数错误：%#v", summary)
	}
	if summary["rows_total"] != 42 {
		t.Fatalf("只有 ok 源贡献条数，应为 42，实际 %v", summary["rows_total"])
	}
	// last_run_at 是最大值（3 分钟前那个），不是第一行
	if summary["last_run_at"] != now.Add(-3*time.Minute).Format(time.RFC3339) {
		t.Fatalf("last_run_at 应取最近一次，实际 %v", summary["last_run_at"])
	}
	if summary["stale"] != false {
		t.Fatalf("10 分钟内刚跑过，不该判 stale：%#v", summary)
	}

	// 失败原因必须可见 —— 这正是"pricetoken 失败原因可见"的验收点
	failed := sources[2]
	if failed.Status != "failed" || failed.Error == "" || failed.DurationMS != 812 {
		t.Fatalf("失败源必须带原因与耗时：%#v", failed)
	}
	// empty 不能被算成 failed（否则会有人去查网络，实际是模型名对不上）
	if sources[0].Status != "empty" || sources[0].Error != "" {
		t.Fatalf("empty 源应无错误文本：%#v", sources[0])
	}
}

func TestBuildPriceSourceReportTruncatesError(t *testing.T) {
	now := time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)
	report := buildPriceSourceReport([]priceSourceRow{{
		sourceKey: "pricetoken", status: "failed",
		error:     repeatChar('x', 500),
		checkedAt: now,
	}}, now)

	sources := report["sources"].([]priceSourceView)
	if len(sources[0].Error) != priceSourceErrorMaxChars {
		t.Fatalf("错误文本应截到 %d 字符，实际 %d", priceSourceErrorMaxChars, len(sources[0].Error))
	}
}

func TestBuildPriceSourceReportFlagsStaleOnlyWhenAllOld(t *testing.T) {
	now := time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)
	// 一个源刚更新、另一个 3 小时前：整体不该判 stale（循环还在跑）
	mixed := buildPriceSourceReport([]priceSourceRow{
		{sourceKey: "a", status: "ok", checkedAt: now.Add(-time.Hour * 3)},
		{sourceKey: "b", status: "ok", checkedAt: now.Add(-time.Minute)},
	}, now)
	if mixed["summary"].(map[string]any)["stale"] != false {
		t.Fatalf("还有源在更新时不该判整体 stale：%#v", mixed["summary"])
	}
	// 全部超过 2 小时：同步循环停了
	allOld := buildPriceSourceReport([]priceSourceRow{
		{sourceKey: "a", status: "ok", checkedAt: now.Add(-time.Hour * 3)},
		{sourceKey: "b", status: "ok", checkedAt: now.Add(-time.Hour*2 - time.Second)},
	}, now)
	summary := allOld["summary"].(map[string]any)
	if summary["stale"] != true || summary["stale_count"] != 2 {
		t.Fatalf("全部过期必须判 stale：%#v", summary)
	}
}

func TestBuildPriceSourceReportUnknownStatusCountsAsFailed(t *testing.T) {
	now := time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)
	report := buildPriceSourceReport([]priceSourceRow{{
		sourceKey: "legacy", status: "unexpected", checkedAt: now,
	}}, now)

	summary := report["summary"].(map[string]any)
	// 未知状态宁可算坏源，也不要算正常 —— 否则状态机变化会静默"全绿"
	if summary["failed"] != 1 || summary["ok"] != 0 {
		t.Fatalf("未知状态应按失败计：%#v", summary)
	}
}

func TestGetPriceSourcesRequiresAuth(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/billing/price-sources", nil)
	rec := httptest.NewRecorder()

	GetPriceSources(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}
	// 与其它 admin 端点同口径：claims 缺失时直接拒绝，不回"空源列表"
	if rec.Body.Len() == 0 {
		t.Fatal("401 也应有错误体")
	}
}

// repeatChar 构造 n 个相同字符（避免 strings.Repeat 与 encoding 细节分散注意力）。
func repeatChar(c byte, n int) string {
	out := make([]byte, n)
	for i := range out {
		out[i] = c
	}
	return string(out)
}
