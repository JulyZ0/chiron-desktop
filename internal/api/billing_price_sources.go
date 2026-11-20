package api

import (
	"log/slog"
	"net/http"
	"sort"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 价格源健康：GET /v1/admin/billing/price-sources
//
// **它回答的问题**：价格表为什么是空的。此前只有一个间接证据 —— 覆盖率面板说
// "有模型没价"，以及引擎日志里一行 `model price fetch failed (pricetoken): …`。
// 用户看不到那行日志，也没有任何界面能区分三种完全不同的原因：
//
//  1. 某个源**不可达**（DNS/超时/401）—— 同步只会白跑；
//  2. 源可达但**对不上我们的模型名**（`empty`）—— 加源或改名才有用；
//  3. 源都正常，但**两源报价不一致** —— 交叉验证按设计拒写，补价只能靠人工/导入。
//
// 本端点把第 1、2 种直接显示出来；第 3 种落在同步日志与 `MODEL_PRICE_SYNC` 指标里。
//
// ## 数据从哪来
//
// `price_source_status`：Python 引擎每轮同步结束写一行/源（见
// `python-engine/app/pricing/sync.py::_write_source_status`）。**不是实时探活** ——
// 这里展示的是"最近一轮同步看到了什么"，所以 `checked_at` 必须一并回给界面，
// 让"3 小时前的失败"不会被读成"现在也失败"。
//
// ## 与覆盖率卡片的关系
//
// 覆盖率说"缺多少价"，这里说"为什么缺"。两者都是运营视角（平台读权限），
// 租户 admin 看到同一份价格源状态没有信息泄露风险 —— 它只含主机名与错误摘要，
// 不含任何租户数据。
const (
	// priceSourceStaleAfter 超过这个时长没有更新就标记 stale：默认同步间隔 30min，
	// 两个小时没动静说明循环卡住 / 引擎没起来，而不是"刚好这一轮慢"。
	priceSourceStaleAfter = 2 * time.Hour
	// priceSourceErrorMaxChars 与写侧（Python `ERROR_MAX_CHARS`）对齐：读侧再截一次，
	// 防止老行/手工写入的超长文本把面板撑坏。
	priceSourceErrorMaxChars = 200
)

// priceSourceRow 是 `price_source_status` 一行的中间形态。
type priceSourceRow struct {
	sourceKey  string
	parser     string
	provider   string
	urlHost    string
	status     string
	rows       int
	durationMS int
	error      string
	checkedAt  time.Time
}

// priceSourceView 是响应里"一个源"的形状。
type priceSourceView struct {
	Source     string `json:"source"`
	Parser     string `json:"parser"`
	Provider   string `json:"provider"`
	URLHost    string `json:"url_host"`
	Status     string `json:"status"`
	Rows       int    `json:"rows"`
	DurationMS int    `json:"duration_ms"`
	Error      string `json:"error"`
	CheckedAt  string `json:"checked_at"`
	AgeSeconds int64  `json:"age_seconds"`
	Stale      bool   `json:"stale"`
}

// GetPriceSources 返回每个价格源最近一轮同步的结果。
func GetPriceSources(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "price source status unavailable")
		return
	}

	// 行数极少（源目录个位数），不排序也不分页；顺序在纯函数里统一。
	rows, err := db.GlobalDBManager.FetchAll(r.Context(), `
		SELECT source_key,
		       COALESCE(parser, '')      AS parser,
		       COALESCE(provider, '')    AS provider,
		       COALESCE(url_host, '')    AS url_host,
		       COALESCE(status, '')      AS status,
		       COALESCE(rows, 0)         AS rows,
		       COALESCE(duration_ms, 0)  AS duration_ms,
		       COALESCE(error, '')       AS error,
		       checked_at
		  FROM price_source_status
		 ORDER BY source_key`)
	if err != nil {
		// 表不存在（未跑 0006 迁移）也走这里。**不返回空列表**：那会让界面显示
		// "所有源都正常"而实际上一个源的状态都没读到 —— 与覆盖率端点同一取舍。
		slog.Warn("billing price sources: query failed", "user", claims.UserID, "error", err)
		ServiceUnavailable(w, "price source status unavailable")
		return
	}

	parsed := make([]priceSourceRow, 0, len(rows))
	for _, row := range rows {
		parsed = append(parsed, priceSourceRow{
			sourceKey:  stringOf(row["source_key"]),
			parser:     stringOf(row["parser"]),
			provider:   stringOf(row["provider"]),
			urlHost:    stringOf(row["url_host"]),
			status:     stringOf(row["status"]),
			rows:       intOf(row["rows"]),
			durationMS: intOf(row["duration_ms"]),
			error:      stringOf(row["error"]),
			checkedAt:  timeOf(row["checked_at"]),
		})
	}

	OK(w, buildPriceSourceReport(parsed, time.Now()))
}

// timeOf 把驱动返回的时间值折成 time.Time（取不到时返回零值）。
//
// 为什么单独写：`stringOf` 会把它格式化成 RFC3339 字符串，而这里需要算 `age_seconds`
// 与 stale 判定 —— 先格式化成字符串再 parse 回来会让"时钟口径"绕一圈。
func timeOf(v interface{}) time.Time {
	switch value := v.(type) {
	case time.Time:
		return value
	case *time.Time:
		if value == nil {
			return time.Time{}
		}
		return *value
	case string:
		if parsed, err := time.Parse(time.RFC3339, value); err == nil {
			return parsed
		}
	}
	return time.Time{}
}

// buildPriceSourceReport 是纯聚合：把"每源最近一轮结果"折成面板视图。
//
// 拆成纯函数是为了让口径能被单测钉住：
//
//   - `ok` / `empty` / `failed` **必须分开**（empty = 源活着但对不上模型名）；
//   - `last_run_at` 取**最大值**而不是"第一行"：源目录里 portkey 有 4 个文件，
//     谁排在前面取决于排名字典序，与"最近一轮"无关；
//   - `stale` 是**全源都过期**才为真：单个源 2 小时没更新可能只是它被移出目录，
//     而全部过期才说明同步循环本身停了。
func buildPriceSourceReport(rows []priceSourceRow, now time.Time) map[string]any {
	sorted := make([]priceSourceRow, len(rows))
	copy(sorted, rows)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].sourceKey < sorted[j].sourceKey })

	views := make([]priceSourceView, 0, len(sorted))
	okCount, emptyCount, failedCount, rowsTotal := 0, 0, 0, 0
	var lastRun time.Time
	staleCount := 0
	for _, row := range sorted {
		stale := false
		var ageSeconds int64
		if !row.checkedAt.IsZero() {
			// 时钟回拨时 age 会是负数；夹到 0，别让界面显示"未来 3 分钟前"。
			age := now.Sub(row.checkedAt)
			if age < 0 {
				age = 0
			}
			ageSeconds = int64(age / time.Second)
			stale = age > priceSourceStaleAfter
			if row.checkedAt.After(lastRun) {
				lastRun = row.checkedAt
			}
		} else {
			// 没有时间戳的行按"过期"处理：我们无法证明它是新鲜的。
			stale = true
		}
		if stale {
			staleCount++
		}

		switch row.status {
		case "ok":
			okCount++
		case "empty":
			emptyCount++
		case "failed":
			failedCount++
		default:
			// 未知状态按失败计：面板宁可多报一个坏源，也不要把它算成正常。
			failedCount++
		}
		rowsTotal += row.rows

		errText := row.error
		if len(errText) > priceSourceErrorMaxChars {
			errText = errText[:priceSourceErrorMaxChars]
		}
		views = append(views, priceSourceView{
			Source:     row.sourceKey,
			Parser:     row.parser,
			Provider:   row.provider,
			URLHost:    row.urlHost,
			Status:     row.status,
			Rows:       row.rows,
			DurationMS: row.durationMS,
			Error:      errText,
			CheckedAt:  row.checkedAt.UTC().Format(time.RFC3339),
			AgeSeconds: ageSeconds,
			Stale:      stale,
		})
	}

	lastRunText := ""
	if !lastRun.IsZero() {
		lastRunText = lastRun.UTC().Format(time.RFC3339)
	}
	return map[string]any{
		"sources": views,
		"summary": map[string]any{
			"total":       len(views),
			"ok":          okCount,
			"empty":       emptyCount,
			"failed":      failedCount,
			"stale":       len(views) > 0 && staleCount == len(views),
			"stale_count": staleCount,
			"rows_total":  rowsTotal,
			"last_run_at": lastRunText,
		},
		// 单位/口径直接写进响应：前端提示语不必各写一份
		"unit": "rows per source",
	}
}
