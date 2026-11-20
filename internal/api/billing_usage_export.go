package api

// ── 用量明细 CSV 导出：GET /v1/billing/usage/export.csv（P1-7）──
//
// **为什么需要它**
//
// 计费页只能看，不能带走。用户要对账（和云厂商账单核、和自己记的表核）时，
// 唯一可行的办法是**把明细导出来**。没有导出，页面上任何一个"看起来对不上"
// 的数字都只能靠截图争论。
//
// **契约（改动必须同时改前端与测试）**
//
//	GET /v1/billing/usage/export.csv?days=90&group=session|model|day
//
//	列（固定顺序，始终 11 列）：
//	  session_id,day,model,provider,calls,input,output,cached,cost,currency,price_missing
//
//	- `group=session`：一行一个会话，session_id 有值，day/model/provider 留空；
//	- `group=model`  ：一行一个模型，model/provider 有值，session_id 留空；
//	- `group=day`    ：一行一天，day 有值，其余维度留空。
//
// 为什么要"始终 11 列 + 空列"而不是"按分组给不同的表头"：CSV 会被粘进表格、
// 被脚本批量读取。表头随参数变化意味着**同一份下游脚本**在换分组后静默错位
// （第 3 列从 model 变成空），而没有任何报错 —— 空列虽然丑，但列位置恒定。
//
// 口径与页面一致（同一张表、同一套窗口）：
//   - `calls`  = 记账行数（`count(*)`；一行 = 某模型在某个回合的调用）；
//   - `input` / `output` / `cached` = token 合计；
//   - `cost`   = **已定价部分**的合计，单位是**分**（USD cents，与 `cost_cents` 同口径）；
//   - `currency` = `USD`（价格表以 USD/1k 计价，换算后仍是 USD 分）；
//   - `price_missing` = 该分组里是否存在"有 token 但取不到单价"的行。
//     为 `true` 时 `cost` **只覆盖已定价部分**，不是全貌 —— 导出里必须能看见这件事，
//     否则一张"总花费 3 分"的表会被当成完整账单。
//
// **只导出本人**：user_id 来自鉴权 claims，**不接受** query 参数覆盖。
// 导出是数据外泄面最大的一类端点，任何"传入 user_id 就导别人的"都是漏洞。

import (
	"encoding/csv"
	"io"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// usageExportDefaultDays 是省略 days 时的窗口（与页面默认 30d 不同：导出通常
// 为对账，给更长的默认更有用；显式传参永远优先）。
const usageExportDefaultDays = 90

// usageExportMaxDays 是窗口上限：days 会进 `make_interval`，不设上限等于把
// 全表扫描成本交给调用方。
const usageExportMaxDays = 365

// usageExportHeader 是唯一的表头（顺序即契约）。
var usageExportHeader = []string{
	"session_id", "day", "model", "provider",
	"calls", "input", "output", "cached",
	"cost", "currency", "price_missing",
}

// usageExportRow 是导出的**已聚合**一行（与表头一一对应）。
//
// 空串/0 表示"这个维度在本次分组下不适用"，而不是缺数据。
type usageExportRow struct {
	sessionID    string
	day          string
	model        string
	provider     string
	calls        int
	inputTokens  int
	outputTokens int
	cachedTokens int
	costCents    int
	currency     string
	priceMissing bool
}

// ExportUsageCSV 导出当前用户区间内的用量明细。
func ExportUsageCSV(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	days := usageExportDefaultDays
	if raw := strings.TrimSpace(r.URL.Query().Get("days")); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed < 1 || parsed > usageExportMaxDays {
			BadRequest(w, "invalid days (expected 1-365)")
			return
		}
		days = parsed
	}

	group := strings.TrimSpace(r.URL.Query().Get("group"))
	if group == "" {
		// 默认按会话：导出的诉求通常是"哪个会话花了多少"。
		group = "session"
	}
	if group != "session" && group != "model" && group != "day" {
		BadRequest(w, "invalid group (expected session|model|day)")
		return
	}

	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "usage store unavailable")
		return
	}

	rows, err := usageExportRows(r, claims.UserID, group, days)
	if err != nil {
		slog.Warn("usage export: aggregate failed",
			"user", claims.UserID, "group", group, "days", days, "error", err)
		InternalError(w, "usage query failed")
		return
	}

	// 头必须在写 body 之前设好：一旦 WriteHeader 就没机会了。
	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition",
		`attachment; filename="`+usageExportFilename(group, days, time.Now().UTC())+`"`)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)

	if err := writeUsageCSV(w, rows); err != nil {
		// 响应头已发出，无法改状态码；只能留日志（客户端会看到截断的文件，
		// 比 200 + 空文件更容易被察觉）。
		slog.Warn("usage export: csv write failed", "user", claims.UserID, "error", err)
	}
}

// usageExportFilename 生成下载文件名（纯函数，便于断言）。
func usageExportFilename(group string, days int, now time.Time) string {
	return "chiron-usage-" + group + "-" + strconv.Itoa(days) + "d-" + now.Format("20060102") + ".csv"
}

// writeUsageCSV 把行写成 CSV。
//
// 用 `encoding/csv` 而不是 `strings.Join`：模型名里出现过逗号/引号（自建网关的
// `vendor/model,beta` 这类名字），手拼会把它们拆成多列、把整张表错位 —— 而
// 这类文件打开时看起来"只是有点怪"，不会有人想到是导出端的 bug。
func writeUsageCSV(w io.Writer, rows []usageExportRow) error {
	writer := csv.NewWriter(w)
	if err := writer.Write(usageExportHeader); err != nil {
		return err
	}
	for _, row := range rows {
		currency := row.currency
		if currency == "" {
			// 单价一律按 USD 换算，导出里不留空（空币种会让下游把它当成"未知金额"）。
			currency = "USD"
		}
		record := []string{
			row.sessionID,
			row.day,
			row.model,
			row.provider,
			strconv.Itoa(row.calls),
			strconv.Itoa(row.inputTokens),
			strconv.Itoa(row.outputTokens),
			strconv.Itoa(row.cachedTokens),
			strconv.Itoa(row.costCents),
			currency,
			strconv.FormatBool(row.priceMissing),
		}
		if err := writer.Write(record); err != nil {
			return err
		}
	}
	writer.Flush()
	return writer.Error()
}

// usageExportRows 按分组查询并折成导出行。
//
// 三个分组共用同一套聚合列，只有"维度列"和 GROUP BY 不同 —— 分开写而不是
// 拼一段动态 SQL，是为了让每个分组的口径都能被单独读到（拼串最容易出现的
// 事故是"某个分组少了一个 SUM"，而 CSV 里看不出来）。
func usageExportRows(r *http.Request, userID, group string, days int) ([]usageExportRow, error) {
	var dimension, groupBy string
	switch group {
	case "model":
		dimension = `''::text AS session_id,
		             ''::text AS day,
		             COALESCE(NULLIF(model, ''), 'unknown') AS model,
		             COALESCE(NULLIF(max(provider), ''), '')::text AS provider`
		groupBy = "3"
	case "day":
		dimension = `''::text AS session_id,
		             ` + usageDayKey + ` AS day,
		             ''::text AS model,
		             ''::text AS provider`
		groupBy = "2"
	default: // session
		dimension = `COALESCE(NULLIF(session_id, ''), 'unknown') AS session_id,
		             ''::text AS day,
		             ''::text AS model,
		             ''::text AS provider`
		groupBy = "1"
	}

	sql := `
		SELECT ` + dimension + `,
		       count(*)                                AS event_count,
		       COALESCE(SUM(input_tokens), 0)::bigint  AS input_tokens,
		       COALESCE(SUM(output_tokens), 0)::bigint AS output_tokens,
		       COALESCE(SUM(cached_tokens), 0)::bigint AS cached_tokens,
		       COALESCE(SUM(cost_cents), 0)::bigint    AS cost_cents,
		       count(*) FILTER (
		           WHERE input_tokens + output_tokens + cached_tokens > 0
		             AND cost_cents IS NULL
		       )                                       AS unpriced_calls
		  FROM usage_events
		 WHERE user_id = $1 AND ` + usageWindow + `
		 GROUP BY ` + groupBy + `
		 ORDER BY cost_cents DESC, ` + groupBy + ` ASC`

	rows, err := db.GlobalDBManager.FetchAll(r.Context(), sql, userID, days)
	if err != nil {
		return nil, err
	}

	out := make([]usageExportRow, 0, len(rows))
	for _, row := range rows {
		out = append(out, usageExportRow{
			sessionID:    stringOf(row["session_id"]),
			day:          stringOf(row["day"]),
			model:        stringOf(row["model"]),
			provider:     stringOf(row["provider"]),
			calls:        intOf(row["event_count"]),
			inputTokens:  intOf(row["input_tokens"]),
			outputTokens: intOf(row["output_tokens"]),
			cachedTokens: intOf(row["cached_tokens"]),
			costCents:    intOf(row["cost_cents"]),
			currency:     "USD",
			priceMissing: intOf(row["unpriced_calls"]) > 0,
		})
	}
	return out, nil
}
