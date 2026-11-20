package api

import (
	"log/slog"
	"net/http"
	"sort"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 价格覆盖率：GET /v1/admin/billing/price-coverage
//
// **它回答的问题**：计费页上"花费算不出来"不是 bug，而是价格表缺行。此前这个
// 事实只藏在 `cost_cents IS NULL` 里 —— 用户看到一串 0，没有任何线索指向
// 「去补价格」。本端点把"近 N 天出现过、但没有单价"的模型列出来，并给出调用数，
// 让补价有入口（前端一键跳 `/billing/prices`）。
//
// 口径（与 `usage_pricing.go` 的换算**同一套**）：
//
//   - 判定"有没有价"用的是 `lookupPrice`（精确名 → 规范化名前缀回落），而不是
//     一条 SQL JOIN 的等值匹配：JOIN 只认精确名，会把 `gpt-4o-2024-08-06`
//     误判成缺价，与真实计费口径不一致 —— 面板说的缺价必须是**真的算不出钱**。
//   - 只统计**有 token 消耗**的记账行：零 token 的历史行本来就没有金额，
//     把它们算成"未定价"会把覆盖率做低，掩盖真正需要补价的模型。
//   - 价格查询本身失败（库抖动 / 表不存在）时**不返回 0% 覆盖率**：那会把一次
//     读失败说成"全都没价"。直接 503，让界面显示"暂时不可用"而不是一个假数字。
const priceCoverageDays = 90

// coverageModelRow 是「某模型在区间内的调用情况」（查询的中间形态）。
type coverageModelRow struct {
	model        string
	calls        int
	tokenCalls   int
	inputTokens  int
	outputTokens int
	lastSeen     string
}

// coverageView 是响应里"未定价模型"一行的形状。
type coverageView struct {
	Model        string `json:"model"`
	Calls        int    `json:"calls"`
	TokenCalls   int    `json:"token_calls"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	LastSeen     string `json:"last_seen"`
}

// GetPriceCoverage 返回近 90 天出现过的模型的价格覆盖情况。
func GetPriceCoverage(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "usage store unavailable")
		return
	}

	// 按租户聚合（平台 owner 的 TenantID 为 0 时不做过滤）：覆盖率是运营指标，
	// 不该让一个租户看到别家的模型使用结构。
	//
	// `SUM(...)::bigint` 不能省：token 三列是 bigint，PG 的 `SUM(bigint)` 返回
	// **numeric**，而 `FetchAll` 扫进 `*interface{}` 时得到 `pgtype.Numeric`。
	// 漏了 cast 的后果不是报错，而是"未定价列表里的 token 数恒为 0" ——
	// 面板看起来正常，实际把"用了多少"这一栏做废了。
	sql := `
		SELECT COALESCE(NULLIF(model, ''), 'unknown')            AS model,
		       count(*)                                          AS calls,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		       )                                                 AS token_calls,
		       COALESCE(SUM(input_tokens), 0)::bigint             AS input_tokens,
		       COALESCE(SUM(output_tokens), 0)::bigint            AS output_tokens,
		       to_char(max(created_at), 'YYYY-MM-DD')            AS last_seen
		  FROM usage_events
		 WHERE created_at >= now() - make_interval(days => $1::int)`
	args := []any{priceCoverageDays}
	if claims.TenantID > 0 {
		sql += ` AND tenant_id = $2`
		args = append(args, claims.TenantID)
	}
	sql += ` GROUP BY 1 ORDER BY calls DESC`

	rows, err := db.GlobalDBManager.FetchAll(r.Context(), sql, args...)
	if err != nil {
		slog.Warn("billing price coverage: usage aggregate failed", "error", err)
		ServiceUnavailable(w, "usage query failed")
		return
	}

	models := make([]coverageModelRow, 0, len(rows))
	for _, row := range rows {
		models = append(models, coverageModelRow{
			model:        stringOf(row["model"]),
			calls:        intOf(row["calls"]),
			tokenCalls:   intOf(row["token_calls"]),
			inputTokens:  intOf(row["input_tokens"]),
			outputTokens: intOf(row["output_tokens"]),
			lastSeen:     stringOf(row["last_seen"]),
		})
	}

	// 逐个模型查价：失败（fetchedAt 为零）意味着"这次问不到价"，不是"没价"。
	pricedCache := make(map[string]bool, len(models))
	for _, m := range models {
		entry := lookupPrice(r.Context(), m.model)
		if entry.fetchedAt.IsZero() {
			// 查询失败：宁可整体 503，也不回一个"覆盖率 0%"的假数。
			slog.Warn("billing price coverage: price lookup failed", "model", m.model)
			ServiceUnavailable(w, "price lookup failed")
			return
		}
		pricedCache[m.model] = entry.found
	}

	OK(w, buildPriceCoverage(models, func(model string) bool {
		return pricedCache[model]
	}, priceCoverageDays))
}

// buildPriceCoverage 是纯聚合：把"区间内的模型调用"与"有没有价"折成覆盖率视图。
//
// 拆成纯函数是为了让"coverage 怎么算、哪些模型算未定价"能被单测钉住 —— 这里
// 一个 off-by-one 就会让运营以为价格表很全（或很烂），而界面上看不出错。
func buildPriceCoverage(
	rows []coverageModelRow,
	priced func(model string) bool,
	days int,
) map[string]any {
	total := 0
	pricedModels := 0
	unpriced := make([]coverageView, 0, 4)
	// 未定价模型按调用数降序：补价优先级由它决定（用得多的先补）。
	sorted := make([]coverageModelRow, len(rows))
	copy(sorted, rows)
	sort.SliceStable(sorted, func(i, j int) bool {
		return sorted[i].calls > sorted[j].calls
	})
	for _, row := range sorted {
		if row.tokenCalls <= 0 {
			// 零 token 的行没有金额可言，不计入覆盖率分母：
			// 把它们算进来会让"覆盖率"看起来比实际低，制造无谓的补价压力。
			continue
		}
		total++
		if priced(row.model) {
			pricedModels++
			continue
		}
		unpriced = append(unpriced, coverageView{
			Model:        row.model,
			Calls:        row.calls,
			TokenCalls:   row.tokenCalls,
			InputTokens:  row.inputTokens,
			OutputTokens: row.outputTokens,
			LastSeen:     row.lastSeen,
		})
	}

	coverage := 0.0
	if total > 0 {
		coverage = float64(pricedModels) / float64(total)
	}
	return map[string]any{
		"period_days": days,
		// 分母只算"有 token 消耗的模型"，与 unpriced 列表同口径。
		"total_models":     total,
		"priced_models":    pricedModels,
		"unpriced_models":  len(unpriced),
		"coverage":         coverage,
		"unpriced":         unpriced,
		"prices_entry_url": "/billing/prices",
	}
}
