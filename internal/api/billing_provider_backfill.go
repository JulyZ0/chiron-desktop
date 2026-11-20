package api

// ── provider 归属回填：POST /v1/billing/backfill-provider（P1-7）──
//
// **它解决的问题**
//
// `usage_events.provider` 是**可空**的：写入那一刻若引擎没报 provider，这一列就是
// NULL。读路径（`enrichBreakdownProviders`）能按模型名当场反查出厂商，但那只是
// "显示时补一下" —— 数据库里仍然是空的。于是：
//
//   - 任何按 (model, provider) 维度做的对账/导出都少一块；
//   - 每次读都要多打一次 `llm_models` / `model_prices`；
//   - 计费页上一句"有 N 个模型查不到厂商归属"会**永远**停在那个数字上，
//     用户没有任何入口去改变它。
//
// 本端点就是那个入口：把"按模型名能查到归属"的历史行**真的写回去**。
//
// **口径（与读路径完全一致）**
//
//   - 只处理 `provider IS NULL OR provider = ''` 的行：已经有值的一律不动 ——
//     写入侧的 provider 是"这次请求配的那个"，可能比按名字反查更准；
//   - 厂商来源只有 `resolveProviderForModel`（`llm_models` → `model_prices`），
//     **不按模型名前缀猜**。查不到的模型原样留在"未解析"列表里；
//   - **只写 provider**。token / cost_cents 一律不碰：金额是单价换算的结果，
//     回填厂商不该顺手重算钱（那会把"某天补了价"的历史事实抹掉）。
//
// **dry_run 是默认值，不是可选项**
//
// 一次调用可能改写上万行。默认 `dry_run=true` 只回预览（哪个模型、能填成什么厂商、
// 影响多少行），前端把预览摆给用户确认，再以 `dry_run=false` 落库。请求体省略
// 时也按 true 处理 —— "误触发一次写"的代价远大于"多看一次预览"。

import (
	"context"
	"log/slog"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// providerBackfillPath 是回填端点的路径（响应里把它交给前端，避免前端写死路由）。
const providerBackfillPath = "/v1/billing/backfill-provider"

// providerBackfillDays 是默认回填窗口：与计费页默认区间一致（近 90 天）。
const providerBackfillDays = 90

// providerBackfillMaxDays 是窗口上限：窗口进 `make_interval`，放开等于把扫描
// 成本交给调用方（与 `usageRangeDays` 白名单同一理由）。
const providerBackfillMaxDays = 365

// providerBackfillModelsMax 是单次预览的模型数上限。
//
// 每个模型都要回源查一次归属（带缓存），不设上限就是"一次请求打 N 次库"。
// 超过上限时响应里 `truncated=true`，前端提示再次调用即可（已回填的模型不会再出现）。
const providerBackfillModelsMax = 200

// providerBackfillBody 是请求体。所有字段都可省略：省略即"预览最近 90 天"。
type providerBackfillBody struct {
	// DryRun 三态：nil（省略）= true。**不能**用 bool 的零值当默认 —— 那会让
	// "没传"和"明确要写"混为一谈，而这里的默认动作必须是只读。
	DryRun *bool `json:"dry_run"`
	Days   int   `json:"days"`
}

// providerBackfillCandidate 是一个"能回填"的模型及其影响行数。
type providerBackfillCandidate struct {
	Model    string `json:"model"`
	Provider string `json:"provider"`
	Rows     int    `json:"rows"`
}

// providerBackfillUnresolved 是一个"查不到归属"的模型（预览里也要列出来，
// 否则用户会以为回填成功之后就全好了）。
type providerBackfillUnresolved struct {
	Model string `json:"model"`
	Rows  int    `json:"rows"`
}

// BackfillUsageProvider 按模型名回填 `usage_events.provider`。
func BackfillUsageProvider(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body providerBackfillBody
	// 允许空 body：前端预览时可以不发任何字段（全部走默认值）。
	if r.ContentLength != 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}

	days := body.Days
	if days == 0 {
		days = providerBackfillDays
	}
	if days < 1 || days > providerBackfillMaxDays {
		BadRequest(w, "invalid days (expected 1-365)")
		return
	}
	dryRun := true
	if body.DryRun != nil {
		dryRun = *body.DryRun
	}

	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "usage store unavailable")
		return
	}

	ctx := r.Context()
	// 只取"归属为空且模型名非空"的行：模型名都没有的行连反查键都没有，
	// 留在原地（它们本来也无法判定归属）。
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT model               AS model,
		       count(*)            AS rows
		  FROM usage_events
		 WHERE user_id = $1
		   AND (provider IS NULL OR provider = '')
		   AND COALESCE(NULLIF(model, ''), '') <> ''
		   AND `+usageWindowAt("", 2)+`
		 GROUP BY 1
		 ORDER BY rows DESC, model ASC
		 LIMIT $3`, claims.UserID, days, providerBackfillModelsMax+1)
	if err != nil {
		slog.Warn("provider backfill: candidate query failed", "user", claims.UserID, "error", err)
		InternalError(w, "usage query failed")
		return
	}

	truncated := len(rows) > providerBackfillModelsMax
	if truncated {
		rows = rows[:providerBackfillModelsMax]
	}

	candidates := make([]providerBackfillCandidate, 0, len(rows))
	unresolved := make([]providerBackfillUnresolved, 0, len(rows))
	for _, row := range rows {
		model := strings.TrimSpace(stringOf(row["model"]))
		rowCount := intOf(row["rows"])
		if model == "" || rowCount <= 0 {
			continue
		}
		// 归属走与读路径同一个函数（同一份缓存、同一套来源），
		// 保证"界面显示 openai"与"回填写 openai"不会出现两个答案。
		provider := resolveProviderForModel(ctx, model)
		if provider == "" {
			unresolved = append(unresolved, providerBackfillUnresolved{Model: model, Rows: rowCount})
			continue
		}
		candidates = append(candidates, providerBackfillCandidate{
			Model:    model,
			Provider: provider,
			Rows:     rowCount,
		})
	}

	view := map[string]any{
		"dry_run":       dryRun,
		"period_days":   days,
		"candidates":    candidates,
		"unresolved":    unresolved,
		"model_limit":   providerBackfillModelsMax,
		"truncated":     truncated,
		"write_scope":   "provider_only",
		"resolve_basis": "llm_models_then_model_prices",
	}

	if dryRun {
		// 预览也要给出"会发生多少行写入"，否则用户无从判断这次点击的规模。
		total := 0
		for _, c := range candidates {
			total += c.Rows
		}
		view["would_update_rows"] = total
		view["updated_rows"] = 0
		OK(w, view)
		return
	}

	updated, models := applyProviderBackfill(ctx, claims.UserID, days, candidates)
	view["updated_rows"] = updated
	view["updated_models"] = models
	OK(w, view)
}

// applyProviderBackfill 把候选行写回库，返回实际影响的行数与成功的模型数。
//
// 逐模型一条 UPDATE（而不是拼一条大 CASE）：每条语句的影响行数可单独统计，
// 某个模型写失败也不会让整批回滚成"什么都没发生"却回 200。
// 条件里重复了 `provider IS NULL` —— 预览与写入之间可能有新数据进来，
// 不能让已经写好的行被覆盖。
func applyProviderBackfill(
	ctx context.Context,
	userID string,
	days int,
	candidates []providerBackfillCandidate,
) (int, int) {
	total := 0
	models := 0
	for _, c := range candidates {
		tag, err := db.GlobalDBManager.Exec(ctx, `
			UPDATE usage_events
			   SET provider = $1
			 WHERE user_id = $3
			   AND lower(COALESCE(model, '')) = lower($4)
			   AND (provider IS NULL OR provider = '')
			   AND `+usageWindowAt("", 2), c.Provider, days, userID, c.Model)
		if err != nil {
			// 单个模型失败不中断整批：其余模型仍应被回填。失败会体现在
			// `updated_models` 与 `candidates` 的长度差上，用户可以重跑一次。
			slog.Warn("provider backfill: update failed",
				"user", userID, "model", c.Model, "provider", c.Provider, "error", err)
			continue
		}
		affected := int(tag.RowsAffected())
		if affected > 0 {
			models++
		}
		total += affected
	}
	return total, models
}
