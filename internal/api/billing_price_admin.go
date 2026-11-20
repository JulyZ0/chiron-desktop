package api

// 手工设置模型单价：PUT /v1/admin/model-prices/{model}
//
// **它补的是哪一段**
//
// 价格表 `model_prices` 的唯一自动写入方是 `python-engine/app/pricing/sync.py`
// 的多源交叉验证循环，而它**默认关闭**（MODEL_PRICE_SYNC=off），且只写"至少两个源
// 在 5% 容差内一致"的模型。于是自建 / 聚合网关 / 新发布的模型（用户实测：
// `deepseek-v4.1-flash`）永远没有价格 ⇒ 那些调用的 `cost_cents` 恒为 NULL ⇒
// 计费页"有 token、花费 0"，且没有任何界面入口能把价格补上。
//
// 本端点就是那个入口：**由人给出单价**，不猜、不写死任何厂商价格。
//
// ## 契约
//
//	PUT /v1/admin/model-prices/{model}
//	{ "input_per_1k": 0.27, "output_per_1k": 1.10, "currency": "USD", "provider": "deepseek" }
//
//   - `{model}` 走 URL 编码（名里可能带 `/`，如 `meta-llama/Llama-3`，必须写成
//     `%2F` 才能落在一个路径段内）；请求体里的 `model` 可作为回退键。
//   - `currency` 缺省 USD，**只接受 USD**：读侧（`fetchPrice`）只认 USD 行，
//     允许写别的币种等于让这一行永远查不到，用户会以为"保存成功了却没生效"。
//   - `provider` 缺省从 `llm_models` / 已有价格行反查（`resolveProviderForModel`），
//     查不到就写空串（表默认值）。**不猜厂商**。
//   - 两个价格都必须是有限非负数，且**不能都为 0**：`usageCostUSD` 把 <= 0 当作
//     "不知道"，写 0 不会让调用变成免费，只会让这一行看起来有价却仍算不出钱。
//
// ## 生效时间
//
// 保存成功即生效：进程内的单价缓存会被整体清空（见 `clearPriceCache` 的注释），
// **下一次调用的 `cost_cents` 立刻按新单价折算**。已经落库的历史行不会被改写
// （`cost_cents` 是当时的事实），但价格覆盖率**立刻**变好 —— 它按"模型有没有价"
// 判定，与历史行无关。所以：新调用即时、覆盖率即时、历史金额不回填。

import (
	"log/slog"
	"math"
	"net/http"
	"strings"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

const (
	// modelPriceNameMax 与 `model_prices.model character varying(128)` 对齐。
	modelPriceNameMax = 128
	// modelPriceProviderMax 与 `model_prices.provider character varying(50)` 对齐。
	modelPriceProviderMax = 50
)

// modelPriceUpsertBody 是 `PUT /v1/admin/model-prices/{model}` 的请求体。
type modelPriceUpsertBody struct {
	// Model 只是路径段的回退键：路径里没给（或为了兼容老客户端）时才用它。
	Model       string  `json:"model"`
	Provider    string  `json:"provider"`
	InputPer1K  float64 `json:"input_per_1k"`
	OutputPer1K float64 `json:"output_per_1k"`
	Currency    string  `json:"currency"`
}

// UpsertModelPrice 写入/更新一条模型单价（平台 owner 写权限，见路由注册）。
//
// 用 UPSERT 而不是 INSERT/UPDATE 两条分支：主键是 `(model, provider)`，管理员和
// 同步脚本可能先后写同一个模型的不同 provider 行 —— 让数据库按主键收敛，
// 应用层不必先查一遍（那会在并发下变成"查到没有 → 两个人都插"）。
func UpsertModelPrice(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	var body modelPriceUpsertBody
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request body")
		return
	}

	model := strings.TrimSpace(r.PathValue("model"))
	if model == "" {
		model = strings.TrimSpace(body.Model)
	}
	if model == "" {
		BadRequest(w, "model is required")
		return
	}
	if utf8.RuneCountInString(model) > modelPriceNameMax {
		BadRequest(w, "model name too long")
		return
	}

	input, output := body.InputPer1K, body.OutputPer1K
	if !isFiniteNonNegative(input) || !isFiniteNonNegative(output) {
		BadRequest(w, "input_per_1k / output_per_1k must be finite and >= 0")
		return
	}
	if input == 0 && output == 0 {
		// 0/0 不会被当成"免费"，而是被 `usageCostUSD` 判为"不知道"（见该函数注释）。
		// 与其让用户以为保存成功，不如直接说清楚。
		BadRequest(w, "input_per_1k and output_per_1k cannot both be 0")
		return
	}

	currency := strings.ToUpper(strings.TrimSpace(body.Currency))
	if currency == "" {
		currency = "USD"
	}
	if currency != "USD" {
		// 读侧只认 USD（`fetchPrice` 的 WHERE），写别的币种就是写一行查不到的数据。
		BadRequest(w, "only USD is supported")
		return
	}

	provider := strings.TrimSpace(body.Provider)
	if provider == "" {
		// 反查失败就是空串：表默认值 `''`，读侧不按 provider 过滤，照样能查到价。
		provider = resolveProviderForModel(r.Context(), model)
	}
	if utf8.RuneCountInString(provider) > modelPriceProviderMax {
		provider = ""
	}

	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "price store unavailable")
		return
	}

	// `sources='manual'` 让"这行是人填的"在价格一览表里可见（同步脚本写的是域名列表），
	// `source_count=1` 与"单源"的既有语义一致（表格据此打"仅 1 源"标记）。
	// `verified_at = now()`：人工确认就是一次确认。
	if _, err := db.GlobalDBManager.Exec(r.Context(), `
		INSERT INTO model_prices
		       (model, provider, input_per_1k, output_per_1k, currency,
		        sources, source_count, verified_at, updated_at)
		VALUES ($1, $2, $3, $4, $5, 'manual', 1, now(), now())
		ON CONFLICT (model, provider) DO UPDATE
		   SET input_per_1k  = EXCLUDED.input_per_1k,
		       output_per_1k = EXCLUDED.output_per_1k,
		       currency      = EXCLUDED.currency,
		       sources       = EXCLUDED.sources,
		       source_count  = EXCLUDED.source_count,
		       verified_at   = now(),
		       updated_at    = now()`,
		model, provider, input, output, currency); err != nil {
		slog.Warn("model price: upsert failed", "model", model, "provider", provider, "error", err)
		// 表不存在（未跑迁移）也走这里：不是"你写错了"，而是"服务端暂时给不出"。
		ServiceUnavailable(w, "price write failed")
		return
	}

	// 缓存整体清空（不是只删这个键）：`fetchPrice` 的规范化名前缀回落意味着
	// `gpt-4o` 的新价会影响所有 `gpt-4o-*` 的解析结果，只失效一个键会留下
	// "补了价但部分模型仍按旧价算"的错位。
	clearPriceCache()

	slog.Info("model price: upserted", "model", model, "provider", provider,
		"input_per_1k", input, "output_per_1k", output, "by", claims.UserID)

	OK(w, map[string]any{
		"model":         model,
		"provider":      provider,
		"input_per_1k":  input,
		"output_per_1k": output,
		"currency":      currency,
		// 生效口径直接写进响应：前端提示语不必各写一份，也不会说错。
		"effective": "next_call",
		"note": "新调用的成本立即按此单价折算；历史行 cost_cents 不回填，" +
			"价格覆盖率立即生效。",
	})
}

// isFiniteNonNegative 判定单价是否可用（拒绝 NaN / ±Inf / 负数）。
//
// NaN 最危险：它在 JSON 里不合法，但经过 `float64` 解析后仍可能出现在测试或
// 上层拼接的请求里；一旦落库，`numeric` 列会直接报错或存入 `NaN`，
// 之后每一次 `SUM` 都被污染。
func isFiniteNonNegative(v float64) bool {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return false
	}
	return v >= 0
}
