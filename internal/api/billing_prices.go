package api

import (
	"context"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// 价格一览表：GET /v1/billing/prices
//
// **这张表不参与计费**（见 `python-engine/app/pricing/sync.py` 模块头）：写入方是
// 那里的多源交叉验证循环，只有**至少两个源在 5% 容差内一致**的模型才会落库。
// 因此这里的语义是"参考价"，不是"我们收你多少钱"。
//
// 三条与读侧相关的口径：
//
//   - **只读 USD**：非 USD 的价格在写入侧就被跳过了，这里再筛一次是因为表可能有
//     人工写入的行 —— 把人民币当成美元显示比少一行糟得多。
//   - **`source_count` 直接吐给前端**：1 表示"只有一个源见过这个模型"，按不变量的
//     意思是它**不应该**存在；真出现了是写入侧有 bug，页面要能看出来。
//   - **默认按 provider + model 排序**而不是按价格：按价格排会让人误以为这张表在
//     推荐"最便宜的模型"，而这只是参考价清单。
const (
	priceListDefaultLimit = 200
	priceListMaxLimit     = 1000
)

// billablePrice 是一行价格。字段名用 snake_case 与其它响应保持一致。
type billablePrice struct {
	Model       string   `json:"model"`
	Provider    string   `json:"provider"`
	InputPer1K  float64  `json:"input_per_1k"`
	OutputPer1K float64  `json:"output_per_1k"`
	Currency    string   `json:"currency"`
	Sources     []string `json:"sources"`
	SourceCount int      `json:"source_count"`
	VerifiedAt  string   `json:"verified_at"`
	UpdatedAt   string   `json:"updated_at"`
}

// ListModelPrices 返回价格表（可按前缀过滤模型名）。
func ListModelPrices(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	limit := priceListDefaultLimit
	if raw := strings.TrimSpace(r.URL.Query().Get("limit")); raw != "" {
		parsed, err := strconv.Atoi(raw)
		if err != nil || parsed <= 0 {
			BadRequest(w, "invalid limit")
			return
		}
		// 夹到上限而不是拒绝：调用方想多要点不该报错，但也不能让它把整表拖出来。
		limit = min(parsed, priceListMaxLimit)
	}
	query := strings.TrimSpace(r.URL.Query().Get("q"))

	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "price store unavailable")
		return
	}

	rows, total, err := fetchModelPrices(r.Context(), query, limit)
	if err != nil {
		slog.Warn("billing prices: list failed", "user", claims.UserID, "error", err)
		// 表不存在（未跑迁移 / 从没同步过）也会走到这里。这不是"你没有价格数据"，
		// 而是服务端暂时给不出 —— 与 usage 端点同样的取舍。
		ServiceUnavailable(w, "price query failed")
		return
	}

	OK(w, map[string]any{
		"prices": rows,
		"total":  total,
		"limit":  limit,
		"query":  query,
		// 前端用它提示"价格是参考值、来自多源交叉验证"
		"unit": "USD/1k tokens",
	})
}

// fetchModelPrices 取一页价格 + 满足过滤条件的总行数。
//
// 两次查询的理由同 `usageBreakdown`：`total` 必须是全量计数，而列表要截断。
// 合并成一个窗口函数查询会让人在加 WHERE 时静默改掉总数。
func fetchModelPrices(ctx context.Context, query string, limit int) ([]billablePrice, int, error) {
	// 过滤条件是"模型名包含"，用 ILIKE 而不是前缀匹配：用户搜 "sonnet" 时
	// 期望命中 claude-3-5-sonnet，而不是只有以 sonnet 开头的（几乎没有）。
	pattern := "%" + strings.ReplaceAll(strings.ReplaceAll(query, `\`, `\\`), "%", `\%`) + "%"
	where := `WHERE (currency IS NULL OR upper(currency) = 'USD')`
	args := []any{}
	if query != "" {
		where += ` AND model ILIKE $1 ESCAPE '\'`
		args = append(args, pattern)
	}

	var total int
	countSQL := `SELECT count(*) AS n FROM model_prices ` + where
	countRow, err := db.GlobalDBManager.FetchOne(ctx, countSQL, args...)
	if err != nil {
		return nil, 0, err
	}
	if countRow != nil {
		total = intOf(countRow["n"])
	}

	listSQL := `
		SELECT model,
		       COALESCE(provider, '')                                AS provider,
		       COALESCE(input_per_1k, 0)::float8                     AS input_per_1k,
		       COALESCE(output_per_1k, 0)::float8                    AS output_per_1k,
		       COALESCE(currency, 'USD')                             AS currency,
		       COALESCE(sources, '')                                 AS sources,
		       COALESCE(source_count, 0)                             AS source_count,
		       to_char(verified_at, 'YYYY-MM-DD')                    AS verified_at,
		       to_char(updated_at, 'YYYY-MM-DD')                     AS updated_at
		  FROM model_prices ` + where + `
		 ORDER BY provider ASC, model ASC
		 LIMIT $` + strconv.Itoa(len(args)+1)
	args = append(args, limit)

	rows, err := db.GlobalDBManager.FetchAll(ctx, listSQL, args...)
	if err != nil {
		return nil, 0, err
	}

	out := make([]billablePrice, 0, len(rows))
	for _, row := range rows {
		out = append(out, billablePrice{
			Model:       stringOf(row["model"]),
			Provider:    stringOf(row["provider"]),
			InputPer1K:  floatOf(row["input_per_1k"]),
			OutputPer1K: floatOf(row["output_per_1k"]),
			Currency:    stringOf(row["currency"]),
			Sources:     splitSources(stringOf(row["sources"])),
			SourceCount: intOf(row["source_count"]),
			VerifiedAt:  stringOf(row["verified_at"]),
			UpdatedAt:   stringOf(row["updated_at"]),
		})
	}
	return out, total, nil
}

// splitSources 把 `sources` 列（逗号分隔的源名）拆成数组。
//
// 保持数组而不是原始字符串：前端要按源数打"已验证 / 单源"标记，让它在模板里
// split 一遍等于把契约细节散到视图层。
func splitSources(raw string) []string {
	out := make([]string, 0, 3)
	for _, part := range strings.Split(raw, ",") {
		if trimmed := strings.TrimSpace(part); trimmed != "" {
			out = append(out, trimmed)
		}
	}
	return out
}
