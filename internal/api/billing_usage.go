package api

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 用量分摊：GET /v1/billing/usage
//
// **为什么这个端点曾经"存在但不可用"**：前端计费页一直在请求它，而网关与引擎
// 两侧都从未注册 —— 每次进页面都是 404，页面只剩一句加载失败。同时
// `billing_records` / `credit_transactions` 随计费域一起从权威迁移里删除了，
// 于是**唯一还在写的用量事实源是 `usage_events`**（一行一次 LLM 调用，由
// `RecordUsageEvent` 在回合收尾时写入）。本端点只读这一张表。
//
// 口径取舍（有意的，不是遗漏）：
//
//   - `groupBy=day` 的响应沿用前端的 `credits_spent` / `total_spent` 字段名以保持
//     契约不动，但数值来自 `usage_events.cost_cents`（**分**）。
//   - `credits_added` 恒为 0：充值域已删除，没有可以相加的来源。字段保留是因为
//     前端按它渲染"充值"列，删字段会让老客户端解析崩掉。
//   - 明细按**花费倒序**截断到 limit 项，但 `total_*` 始终是**全量**合计 ——
//     两套口径混用会让"明细加起来对不上合计"，而界面上不会有任何报错。
const (
	usageBucketLimit  = 100
	usageDefaultRange = "30d"
)

// usageRangeDays 是 range 白名单。**不接受任意整数**：区间直接进 SQL 的
// `make_interval`，放开就等于把查询成本交给调用方。
var usageRangeDays = map[string]int{"7d": 7, "30d": 30, "90d": 90}

// usageGroupColumn 是分组白名单：分组名会拼进 SELECT 的列表达式，必须白名单。
var usageGroupColumn = map[string]string{
	"model":   "COALESCE(NULLIF(model, ''), 'unknown')",
	"session": "COALESCE(NULLIF(session_id, ''), 'unknown')",
}

// usageWindow 是所有查询共用的时间窗谓词。
//
// 用 `date_trunc('day', now())` 对齐到自然日边界，而不是 `now() - interval`：
// 后者会让窗口起点落在半天处，与下面补零生成的"整天"序列错开一格，
// 表现为趋势图第一根柱子永远比明细少。
const usageWindow = `created_at >= date_trunc('day', now()) - make_interval(days => $2::int - 1)`

// GetUsage 返回区间内的用量汇总（按天 / 按模型 / 按会话）。
func GetUsage(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	groupBy := strings.TrimSpace(r.URL.Query().Get("groupBy"))
	if groupBy == "" {
		groupBy = "day"
	}
	if groupBy != "day" && usageGroupColumn[groupBy] == "" {
		BadRequest(w, "invalid groupBy (expected day|model|session)")
		return
	}
	rangeKey := strings.TrimSpace(r.URL.Query().Get("range"))
	if rangeKey == "" {
		rangeKey = usageDefaultRange
	}
	days, ok := usageRangeDays[rangeKey]
	if !ok {
		BadRequest(w, "invalid range (expected 7d|30d|90d)")
		return
	}

	if db.GlobalDBManager == nil {
		// 库不可用是**服务端暂时不可用**，不是"你没有用量"。返回空数组会被
		// 误读成"这 30 天一次都没用过"，用户会以为统计坏了（事实上是库没连上）。
		// 放在参数校验之后：参数写错是调用方的问题，跟库在不在没关系。
		ServiceUnavailable(w, "usage store unavailable")
		return
	}

	ctx := r.Context()
	if groupBy == "day" {
		view, err := usageDaily(ctx, claims.UserID, days)
		if err != nil {
			slog.Warn("billing usage: daily aggregate failed", "user", claims.UserID, "error", err)
			InternalError(w, "usage query failed")
			return
		}
		OK(w, view)
		return
	}

	view, err := usageBreakdown(ctx, claims.UserID, groupBy, days)
	if err != nil {
		slog.Warn("billing usage: breakdown failed", "user", claims.UserID, "group", groupBy, "error", err)
		InternalError(w, "usage query failed")
		return
	}
	OK(w, view)
}

// usageDaily 是按天视图：daily 序列 + 区间累计花费。
//
// **补零到整天**：只回"有消耗的那几天"会让趋势图把 30 天压成两三根柱子，
// 看起来像数据丢了。补齐后 x 轴才是真实的时间轴。
func usageDaily(ctx context.Context, userID string, days int) (map[string]any, error) {
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT to_char(date_trunc('day', created_at), 'YYYY-MM-DD') AS day,
		       count(*)                             AS event_count,
		       COALESCE(SUM(cost_cents), 0)         AS cost_cents
		  FROM usage_events
		 WHERE user_id = $1 AND `+usageWindow+`
		 GROUP BY 1`, userID, days)
	if err != nil {
		return nil, err
	}

	spentByDay := make(map[string]int, len(rows))
	eventsByDay := make(map[string]int, len(rows))
	for _, row := range rows {
		day := stringOf(row["day"])
		spentByDay[day] = intOf(row["cost_cents"])
		eventsByDay[day] = intOf(row["event_count"])
	}

	daily, total := buildDailySeries(spentByDay, eventsByDay, days, time.Now())

	return map[string]any{
		"daily":       daily,
		"total_spent": total,
		"period_days": days,
	}, nil
}

// buildDailySeries 把"有消耗的那几天"补成连续 days 天的序列（升序）。
//
// **补零到整天**：只回有数据的天会让趋势图把 30 天压成两三根柱子，看起来像
// 数据丢了；补齐后 x 轴才是真实的时间轴。today 可注入，测试里用固定日期，
// 否则跨零点跑的用例会随机落在两条不同的序列上。
//
// 返回的 total 只累加窗口内的天：库里有窗口外的行时（理论上不应有），
// 不能让它们偷渡进"区间累计"。
func buildDailySeries(spentByDay, eventsByDay map[string]int, days int, today time.Time) ([]map[string]any, int) {
	if days <= 0 {
		return []map[string]any{}, 0
	}
	daily := make([]map[string]any, 0, days)
	total := 0
	for i := days - 1; i >= 0; i-- {
		day := today.AddDate(0, 0, -i).Format("2006-01-02")
		spent := spentByDay[day]
		total += spent
		daily = append(daily, map[string]any{
			"day":      day,
			"tx_count": eventsByDay[day],
			// 字段名沿用历史契约；数值是 usage_events.cost_cents（分）
			"credits_spent": spent,
			// 充值域已删除：这里恒为 0，但没有这个键前端会渲染成 undefined
			"credits_added": 0,
		})
	}
	return daily, total
}

// usageBreakdown 是按模型 / 按会话的明细视图。
//
// 两次查询而不是一次窗口函数：`total_*` 必须是**全量**合计，而明细要截断。
// 用窗口函数在 SQL 里同时算两者（SUM() OVER ()）也能做，但一旦有人给明细加上
// WHERE 就会静默污染合计 —— 拆开后这两件事在语法上就不可能串味。
func usageBreakdown(ctx context.Context, userID, groupBy string, days int) (map[string]any, error) {
	column := usageGroupColumn[groupBy]

	totals, err := db.GlobalDBManager.FetchOne(ctx, fmt.Sprintf(`
		SELECT count(DISTINCT %[1]s)                AS buckets,
		       count(*)                             AS event_count,
		       COALESCE(SUM(input_tokens), 0)        AS input_tokens,
		       COALESCE(SUM(output_tokens), 0)       AS output_tokens,
		       COALESCE(SUM(cached_tokens), 0)       AS cached_tokens,
		       COALESCE(SUM(cost_cents), 0)          AS cost_cents
		  FROM usage_events
		 WHERE user_id = $1 AND `+usageWindow, column), userID, days)
	if err != nil {
		return nil, err
	}

	rows, err := db.GlobalDBManager.FetchAll(ctx, fmt.Sprintf(`
		SELECT %[1]s                               AS bucket,
		       count(*)                             AS event_count,
		       COALESCE(SUM(input_tokens), 0)        AS input_tokens,
		       COALESCE(SUM(output_tokens), 0)       AS output_tokens,
		       COALESCE(SUM(cached_tokens), 0)       AS cached_tokens,
		       COALESCE(SUM(cost_cents), 0)          AS cost_cents
		  FROM usage_events
		 WHERE user_id = $1 AND `+usageWindow+`
		 GROUP BY 1
		 ORDER BY cost_cents DESC, bucket ASC
		 LIMIT $3`, column), userID, days, usageBucketLimit)
	if err != nil {
		return nil, err
	}

	breakdown := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		breakdown = append(breakdown, map[string]any{
			"bucket":        stringOf(row["bucket"]),
			"event_count":   intOf(row["event_count"]),
			"input_tokens":  intOf(row["input_tokens"]),
			"output_tokens": intOf(row["output_tokens"]),
			"cached_tokens": intOf(row["cached_tokens"]),
			"cost_cents":    intOf(row["cost_cents"]),
		})
	}

	buckets := 0
	view := map[string]any{
		"group_by":    groupBy,
		"period_days": days,
		"breakdown":   breakdown,
		"limit":       usageBucketLimit,
		// daily 在非按天分组时是空数组占位：前端用它判断"要不要画柱状图"，
		// 键缺失会让它读到 undefined 而不是"没有数据"。
		"daily": []any{},
	}
	if totals != nil {
		buckets = intOf(totals["buckets"])
		view["total_input"] = intOf(totals["input_tokens"])
		view["total_output"] = intOf(totals["output_tokens"])
		view["total_cached"] = intOf(totals["cached_tokens"])
		view["total_cost"] = intOf(totals["cost_cents"])
	}
	view["total_buckets"] = buckets
	view["truncated"] = buckets > len(breakdown)
	return view, nil
}
