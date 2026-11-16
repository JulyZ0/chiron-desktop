package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

// 用量分摊端点（`GET /v1/billing/usage`）的契约。
//
// 这个端点此前**根本没注册**：前端每进一次计费页就 404，页面只剩加载失败。
// 下面钉的是容易再犯的那几处：
//
//  1. 参数白名单 —— groupBy 会拼进 SELECT 列表达式，range 会进 make_interval。
//     放开任意值等于把查询成本和注入面交给调用方。
//  2. 补零到整天 —— 只回有数据的天会让趋势图把 30 天压成两根柱子。
//  3. 合计口径 —— `total_*` 必须覆盖窗口内**全部**行，不能只加窗口内的天；
//     更不能被明细截断影响。

func authedRequest(target string) *http.Request {
	req := httptest.NewRequest(http.MethodGet, target, nil)
	claims := &auth.Claims{UserID: "u-1", TenantID: 1}
	return req.WithContext(auth.WithClaims(req.Context(), claims))
}

// ── buildDailySeries（纯函数） ──

func TestBuildDailySeriesFillsGapsAndSumsWindow(t *testing.T) {
	today := time.Date(2026, 11, 6, 15, 0, 0, 0, time.UTC)
	spent := map[string]int{"2026-11-04": 30, "2026-11-06": 12}
	events := map[string]int{"2026-11-04": 2, "2026-11-06": 1}

	daily, total := buildDailySeries(spent, events, 3, today)

	if len(daily) != 3 {
		t.Fatalf("应补成 3 天，实际 %d", len(daily))
	}
	// 升序：最旧的在前，图表从左到右读
	if daily[0]["day"] != "2026-11-04" || daily[2]["day"] != "2026-11-06" {
		t.Fatalf("序列应为升序的连续 3 天，实际 %v", daily)
	}
	// 中间那天没有事件：必须有零值行，而不是被跳过
	if daily[1]["credits_spent"] != 0 || daily[1]["tx_count"] != 0 {
		t.Fatalf("空白天应补零，实际 %v", daily[1])
	}
	if total != 42 {
		t.Fatalf("区间累计应为 42，实际 %d", total)
	}
	// 充值域已删除，但键必须存在，否则前端渲染 undefined
	if daily[0]["credits_added"] != 0 {
		t.Fatalf("credits_added 应为 0，实际 %v", daily[0]["credits_added"])
	}
}

func TestBuildDailySeriesIgnoresRowsOutsideWindow(t *testing.T) {
	today := time.Date(2026, 11, 6, 0, 0, 0, 0, time.UTC)
	// 窗口外的历史行不该偷渡进"区间累计"
	spent := map[string]int{"2026-10-01": 999, "2026-11-06": 5}

	_, total := buildDailySeries(spent, nil, 7, today)

	if total != 5 {
		t.Fatalf("只应累加窗口内的 5，实际 %d", total)
	}
}

func TestBuildDailySeriesHandlesNonPositiveDays(t *testing.T) {
	// 空区间不该产出 nil（JSON 会变成 null，前端 .length 直接崩）
	daily, total := buildDailySeries(nil, nil, 0, time.Now())
	if daily == nil {
		t.Fatal("空区间应返回空切片而不是 nil")
	}
	if len(daily) != 0 || total != 0 {
		t.Fatalf("空区间应为空，实际 %d 天 / %d", len(daily), total)
	}
}

// ── 参数校验 ──

func TestGetUsageRejectsUnknownGroupBy(t *testing.T) {
	rec := httptest.NewRecorder()
	GetUsage(rec, authedRequest("/v1/billing/usage?groupBy=password"))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法 groupBy 应 400，实际 %d", rec.Code)
	}
}

func TestGetUsageRejectsUnknownRange(t *testing.T) {
	// range 直接进 make_interval：任意整数必须被白名单挡住
	rec := httptest.NewRecorder()
	GetUsage(rec, authedRequest("/v1/billing/usage?range=3650d"))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法 range 应 400，实际 %d", rec.Code)
	}
}

func TestGetUsageRequiresAuth(t *testing.T) {
	rec := httptest.NewRecorder()
	GetUsage(rec, httptest.NewRequest(http.MethodGet, "/v1/billing/usage", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未认证应 401，实际 %d", rec.Code)
	}
}
