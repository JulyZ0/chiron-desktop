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
//
// **必须显式 `AT TIME ZONE 'UTC'`**：`created_at` 是 timestamptz，库的会话时区
// 可能是 Asia/Shanghai 而 Go 侧补零用的是 UTC 日期串 —— 两边各按自己的时区切天，
// 会让"近 N 天"的第一天/最后一天与明细互相对不上（差 8 小时的错位只在晚间出现，
// 白天一切正常，所以极难复现）。切天口径统一到 UTC，与 `buildDailySeries` 一致。
const usageWindow = `created_at >= (
	date_trunc('day', now() AT TIME ZONE 'UTC') - make_interval(days => $2::int - 1)
) AT TIME ZONE 'UTC'`

// usageDayKey 把 `created_at` 折成**UTC** 的 `YYYY-MM-DD`，与 usageWindow 同口径。
const usageDayKey = `to_char(date_trunc('day', created_at AT TIME ZONE 'UTC'), 'YYYY-MM-DD')`

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

// usageDaily 是按天视图：daily 序列 + 区间累计花费与 token。
//
// **补零到整天**：只回"有消耗的那几天"会让趋势图把 30 天压成两三根柱子，
// 看起来像数据丢了。补齐后 x 轴才是真实的时间轴。
//
// **token 必须在这里也 SUM**：此前按天视图只汇总 `cost_cents`，于是"花费算不出来"
// （模型名缺失 ⇒ 无单价 ⇒ 列是 NULL）时整页看起来只有一串 0，而 token 其实完全
// 正常地躺在表里。按天视图缺 token，等于让用户无法自证"用量确实被记下来了"。
func usageDaily(ctx context.Context, userID string, days int) (map[string]any, error) {
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT `+usageDayKey+`                    AS day,
		       count(*)                           AS event_count,
		       COALESCE(SUM(cost_cents), 0)       AS cost_cents,
		       COALESCE(SUM(input_tokens), 0)     AS input_tokens,
		       COALESCE(SUM(output_tokens), 0)    AS output_tokens,
		       COALESCE(SUM(cached_tokens), 0)    AS cached_tokens,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens = 0
		       )                                  AS zero_token_events,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		             AND cost_cents IS NULL
		       )                                  AS unpriced_calls
		  FROM usage_events
		 WHERE user_id = $1 AND `+usageWindow+`
		 GROUP BY 1`, userID, days)
	if err != nil {
		return nil, err
	}

	daily := make([]usageDayRow, 0, len(rows))
	for _, row := range rows {
		daily = append(daily, usageDayRow{
			day:             stringOf(row["day"]),
			events:          intOf(row["event_count"]),
			costCents:       intOf(row["cost_cents"]),
			inputTokens:     intOf(row["input_tokens"]),
			outputTokens:    intOf(row["output_tokens"]),
			cachedTokens:    intOf(row["cached_tokens"]),
			zeroTokenEvents: intOf(row["zero_token_events"]),
			unpricedCalls:   intOf(row["unpriced_calls"]),
		})
	}

	view := buildDailyView(daily, days, time.Now().UTC())
	return view, nil
}

// usageDayRow 是一天的聚合值（`usageDaily` 的中间形态，便于纯函数测试）。
type usageDayRow struct {
	day             string
	events          int
	costCents       int
	inputTokens     int
	outputTokens    int
	cachedTokens    int
	zeroTokenEvents int
	// unpricedCalls 是「有真实 token、但取不到单价」的记账行数。
	// **不由 0 分冒充**：0 分是"这个模型免费"，缺价是"我们不知道"，
	// 两者在合计里都是 0，但在明细里必须能分开（派生口径：cost_cents IS NULL）。
	unpricedCalls int
}

// buildDailyView 把按天的聚合补齐成连续 days 天，并算出区间合计。
//
// 与 `buildDailySeries` 的关系：那个只处理"花费 + 事件数"（旧契约），这里扩到
// token 维度。补零、升序、只累加窗口内的天 —— 三条不变式完全一致，故复用同一份
// 逻辑：先用行构造 map，再交给 buildDailySeries 生成骨架，最后把 token 填回去。
// 这样两条路径不可能在"哪些天算进来"上漂移。
func buildDailyView(rows []usageDayRow, days int, today time.Time) map[string]any {
	spent := make(map[string]int, len(rows))
	events := make(map[string]int, len(rows))
	byDay := make(map[string]usageDayRow, len(rows))
	for _, r := range rows {
		spent[r.day] = r.costCents
		events[r.day] = r.events
		byDay[r.day] = r
	}

	daily, totalSpent := buildDailySeries(spent, events, days, today)

	totalInput, totalOutput, totalCached, totalCalls, zeroToken, unpriced := 0, 0, 0, 0, 0, 0
	for _, point := range daily {
		day, _ := point["day"].(string)
		r := byDay[day]
		point["input_tokens"] = r.inputTokens
		point["output_tokens"] = r.outputTokens
		point["cached_tokens"] = r.cachedTokens
		point["zero_token_events"] = r.zeroTokenEvents
		point["unpriced_calls"] = r.unpricedCalls
		point["price_missing"] = r.unpricedCalls > 0
		totalInput += r.inputTokens
		totalOutput += r.outputTokens
		totalCached += r.cachedTokens
		totalCalls += r.events
		zeroToken += r.zeroTokenEvents
		unpriced += r.unpricedCalls
	}

	return map[string]any{
		"daily":             daily,
		"total_spent":       totalSpent,
		"period_days":       days,
		"total_input":       totalInput,
		"total_output":      totalOutput,
		"total_cached":      totalCached,
		"total_calls":       totalCalls,
		"zero_token_events": zeroToken,
		// 缺价（有 token 但取不到单价）的记账行数：汇总卡据此显示"部分未定价"，
		// 而不是把一个偏小的合计当成全部花费。历史零值不回填，这里只陈述事实。
		"total_unpriced_calls": unpriced,
		"price_missing":        unpriced > 0,
		// 调用次数口径：`event_count` / `tx_count` = `usage_events` 的**记账行数**。
		// 一行对应"某个模型在这一轮里的一次或多次调用"（同模型同轮合并），
		// 所以它是**调用次数的下界**，不是精确调用数 —— 字段写清楚是为了让
		// "9 次调用 / 12 次调用"这类对不上的疑问有据可查，而不是靠猜。
		"count_basis": "usage_rows",
	}
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
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens = 0
		       )                                    AS zero_token_events,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		             AND cost_cents IS NOT NULL
		       )                                    AS priced_calls,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		             AND cost_cents IS NULL
		       )                                    AS unpriced_calls,
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
		       COALESCE(SUM(cost_cents), 0)          AS cost_cents,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		             AND cost_cents IS NOT NULL
		       )                                    AS priced_calls,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		             AND cost_cents IS NULL
		       )                                    AS unpriced_calls
		  FROM usage_events
		 WHERE user_id = $1 AND `+usageWindow+`
		 GROUP BY 1
		 ORDER BY cost_cents DESC, bucket ASC
		 LIMIT $3`, column), userID, days, usageBucketLimit)
	if err != nil {
		return nil, err
	}

	bucketRows := make([]usageBucketRow, 0, len(rows))
	for _, row := range rows {
		bucketRows = append(bucketRows, usageBucketRow{
			bucket:        stringOf(row["bucket"]),
			events:        intOf(row["event_count"]),
			inputTokens:   intOf(row["input_tokens"]),
			outputTokens:  intOf(row["output_tokens"]),
			cachedTokens:  intOf(row["cached_tokens"]),
			costCents:     intOf(row["cost_cents"]),
			pricedCalls:   intOf(row["priced_calls"]),
			unpricedCalls: intOf(row["unpriced_calls"]),
		})
	}
	breakdown := buildBreakdownRows(bucketRows)

	buckets := 0
	zeroToken := 0
	view := map[string]any{
		"group_by":    groupBy,
		"period_days": days,
		"breakdown":   breakdown,
		"limit":       usageBucketLimit,
		// daily 在非按天分组时是空数组占位：前端用它判断"要不要画柱状图"，
		// 键缺失会让它读到 undefined 而不是"没有数据"。
		"daily": []any{},
		// 调用次数口径见 usageDaily 的注释：`event_count` 是记账行数。
		"count_basis": "usage_rows",
	}
	if totals != nil {
		buckets = intOf(totals["buckets"])
		zeroToken = intOf(totals["zero_token_events"])
		priced := intOf(totals["priced_calls"])
		unpriced := intOf(totals["unpriced_calls"])
		view["total_input"] = intOf(totals["input_tokens"])
		view["total_output"] = intOf(totals["output_tokens"])
		view["total_cached"] = intOf(totals["cached_tokens"])
		view["total_cost"] = intOf(totals["cost_cents"])
		view["total_calls"] = intOf(totals["event_count"])
		// 缺价派生（**不改表**）：cost_cents IS NULL ⇒ 这一行当时取不到单价。
		// priced_calls + unpriced_calls + zero_token_events = total_calls，
		// 三条互斥：有价 / 无价 / 连用量都没有。
		view["total_priced_calls"] = priced
		view["total_unpriced_calls"] = unpriced
		view["price_missing"] = unpriced > 0
	}
	// 零 token 的记账行：升级前的历史行（当时链路只写"有一次调用"，没拿到 token）
	// 与"引擎没回报用量"的行混在一起，无法事后区分，也不做回填（那等于编数据）。
	// 界面据此标注"这段时间的记录不含 token 明细"，而不是让用户以为统计坏了。
	view["zero_token_events"] = zeroToken
	view["total_buckets"] = buckets
	view["truncated"] = buckets > len(breakdown)
	return view, nil
}

// usageBucketRow 是一个分组（模型 / 会话）的聚合值。
//
// 单独成结构是为了让「缺价怎么派生」这一条口径能在**纯函数**里被测到：
// 它决定界面显示数字还是 "—"，而两者在合计里都是 0。
type usageBucketRow struct {
	bucket        string
	events        int
	inputTokens   int
	outputTokens  int
	cachedTokens  int
	costCents     int
	pricedCalls   int
	unpricedCalls int
}

// buildBreakdownRows 把聚合行转成前端契约（**派生** price_missing，不改表）。
//
// 口径：
//   - `cost_cents` 仍回"已定价部分之和"（保持老客户端可用）；
//   - `priced_calls` / `unpriced_calls` 让界面知道这个数是否**只覆盖了一部分**；
//   - `price_missing` = unpriced_calls > 0，前端据此显示 "—" + tooltip 而不是 0。
func buildBreakdownRows(rows []usageBucketRow) []map[string]any {
	out := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		out = append(out, map[string]any{
			"bucket":         r.bucket,
			"event_count":    r.events,
			"input_tokens":   r.inputTokens,
			"output_tokens":  r.outputTokens,
			"cached_tokens":  r.cachedTokens,
			"cost_cents":     r.costCents,
			"priced_calls":   r.pricedCalls,
			"unpriced_calls": r.unpricedCalls,
			"price_missing":  r.unpricedCalls > 0,
		})
	}
	return out
}
