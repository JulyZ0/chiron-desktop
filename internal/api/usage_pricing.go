package api

import (
	"context"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// 按模型的成本换算（`usage_events.cost_cents` 的**唯一**写入依据）。
//
// **为什么需要它**：`cost_cents` 这一列一直存在、一直被 `GetUsage` / `GetMetrics`
// 读取，但**从来没有人写入过** —— `RecordUsageEvent` 的 `CostCents` 字段全仓只有
// 一个声明点和一个读点，没有赋值点。结果是计费页的"花费"始终是 0，而"今日消耗"
// 看起来像统计彻底坏了。
//
// 价格来自 `model_prices`（由 `python-engine/app/pricing/sync.py` 多源交叉验证后
// 写入，USD / 1k tokens）。**它是参考数据，不是计费权威** —— 但计费域
// （`billing_records` / `credit_transactions`）已随迁移整体删除，此刻它是仓里唯一
// 一条可按模型取到的单价来源。用它算**成本观测值**是合理的；用它做**扣费**不是。
//
// 取不到价格时返回 nil（写 NULL），而不是 0：0 是"这个模型免费"，NULL 是
// "我们不知道" —— 两者在聚合里都得 0，但在明细里不该互相冒充。

// priceCacheTTL 是单价缓存时长。价格是慢变量（上游日更），但也不该让一次同步
// 之后进程还要重启才生效，5 分钟是个够短的折中。
const priceCacheTTL = 5 * time.Minute

type priceEntry struct {
	inputPer1k  float64
	outputPer1k float64
	fetchedAt   time.Time
	found       bool
}

var (
	priceCacheMu sync.RWMutex
	priceCache   = map[string]priceEntry{}
)

// usageCostCents 按模型单价把一次调用的 token 用量换算成**分**。
//
// 换算：`tokens/1000 * usd_per_1k * 100`，即 `tokens * usd_per_1k / 10`。
//
// ⚠ **缓存命中的 token 按输入价计**：`model_prices` 没有 cached 档（多数厂商的
// 缓存读价是输入价的 10%~50%），这里不打折是**保守**选择 —— 宁可成本算高一点，
// 也不要凭空编一个折扣率出来。要精确就得让价格表带上 cached 列，那是另一件事。
func usageCostCents(ctx context.Context, model string, input, output, cached int) *int {
	model = strings.TrimSpace(model)
	if model == "" || db.GlobalDBManager == nil {
		return nil
	}
	entry := lookupPrice(ctx, model)
	if !entry.found {
		return nil
	}
	// cached 是 input 的子集（缓存命中那部分），所以输入侧总量仍是 input ——
	// 不能把 cached 再加一遍，否则输入 token 被算两遍、成本虚高。
	usd := float64(input)/1000*entry.inputPer1k + float64(output)/1000*entry.outputPer1k
	if usd <= 0 {
		return nil
	}
	cents := int(math.Round(usd * 100))
	// 有真实用量但换算后不足 1 分：记 1 分而不是 0。
	// 记为 0 会让"用了一次但怎么没花钱"和"模型免费"混为一谈。
	if cents == 0 && (input > 0 || output > 0) {
		cents = 1
	}
	return &cents
}

// lookupPrice 取单价：精确名优先，回落到规范化名（去日期后缀/分隔符差异）。
func lookupPrice(ctx context.Context, model string) priceEntry {
	now := time.Now()

	priceCacheMu.RLock()
	if cached, ok := priceCache[model]; ok && now.Sub(cached.fetchedAt) < priceCacheTTL {
		priceCacheMu.RUnlock()
		return cached
	}
	priceCacheMu.RUnlock()

	entry := fetchPrice(ctx, model)
	if entry.found || entry.fetchedAt.IsZero() {
		// 只缓存"查到了"和"查了但没这个模型"。查询失败（DB 抖动）不缓存，
		// 否则一次瞬时故障会让这个模型在 5 分钟内都算不出成本。
		entry.fetchedAt = now
		priceCacheMu.Lock()
		priceCache[model] = entry
		priceCacheMu.Unlock()
	}
	return entry
}

// fetchPrice 查一次价格表。
//
// 查询本身失败时返回零值（fetchedAt 为零 ⇒ 上层不缓存）。
func fetchPrice(ctx context.Context, model string) priceEntry {
	row, err := db.GlobalDBManager.FetchOne(ctx, `
		SELECT input_per_1k::float8  AS input_per_1k,
		       output_per_1k::float8 AS output_per_1k,
		       COALESCE(currency, 'USD') AS currency
		  FROM model_prices
		 WHERE lower(model) = lower($1)
		   AND (currency IS NULL OR upper(currency) = 'USD')
		 ORDER BY verified_at DESC NULLS LAST
		 LIMIT 1`, model)
	if err != nil {
		slog.Debug("usage pricing: price lookup failed", "model", model, "error", err)
		return priceEntry{}
	}
	if row != nil {
		return priceEntry{
			inputPer1k:  floatOf(row["input_per_1k"]),
			outputPer1k: floatOf(row["output_per_1k"]),
			found:       true,
		}
	}

	// 精确名没命中：按规范化名再找一次（`gpt-4o-2024-08-06` ↔ `gpt-4o`）。
	// 用 SQL 里的前缀匹配而不是把规范化逻辑复制到 Go —— 两个源的对齐启发式
	// （python 的 normalize_model）已经在写侧生效过，这里只需要"最长前缀"。
	row, err = db.GlobalDBManager.FetchOne(ctx, `
		SELECT input_per_1k::float8  AS input_per_1k,
		       output_per_1k::float8 AS output_per_1k
		  FROM model_prices
		 WHERE lower($1) LIKE lower(model) || '%'
		   AND (currency IS NULL OR upper(currency) = 'USD')
		 ORDER BY length(model) DESC, verified_at DESC NULLS LAST
		 LIMIT 1`, model)
	if err != nil || row == nil {
		return priceEntry{}
	}
	return priceEntry{
		inputPer1k:  floatOf(row["input_per_1k"]),
		outputPer1k: floatOf(row["output_per_1k"]),
		found:       true,
	}
}

// floatOf 把驱动返回的数值列转成 float64。
//
// numeric/float8 经 pgx 回来的形态不定（float64 / int64 / string），
// 与同包的 intOf 是同一类"驱动细节不外泄"的适配。
func floatOf(v interface{}) float64 {
	switch value := v.(type) {
	case float64:
		return value
	case float32:
		return float64(value)
	case int64:
		return float64(value)
	case int32:
		return float64(value)
	case int:
		return float64(value)
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
		if err == nil {
			return f
		}
	}
	return 0
}
