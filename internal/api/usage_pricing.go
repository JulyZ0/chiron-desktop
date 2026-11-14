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
// 读取，但长期**没有人写入** —— `RecordUsageEvent` 的 `CostCents` 字段曾只有声明点
// 和读点、没有赋值点，于是计费页的"花费"始终是 0，看起来像统计彻底坏了。
//
// 现在写入点在 `submit_handler.go` 的结算段：按模型逐行定价（`modelLedger.settle`
// 拆出各模型的 token），整轮金额再用 `aggregateCostCents` 精确累加后**只取整一次**。
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

// usageCostUSD 按模型单价把一次调用的 token 用量换算成**美元**（未取整）。
//
// 拆出这一层是为了**避免多次取整**：`usage_events` 按模型逐行落库，每行都要写成
// 整数分，于是每行都会被「不足 1 分记 1 分」的规则抬一次。若整轮金额也由这些
// 已取整的行相加得到，3 次各 0.3 分的调用会变成 3 分而不是 0.9 分 —— 贵了一倍多。
// 整轮金额必须先按精确值累加，最后只取整一次。
//
// ⚠ **缓存命中的 token 按输入价计**：`model_prices` 没有 cached 档（多数厂商的
// 缓存读价是输入价的 10%~50%），这里不打折是**保守**选择 —— 宁可成本算高一点，
// 也不要凭空编一个折扣率出来。要精确就得让价格表带上 cached 列，那是另一件事。
//
// 第二个返回值表示**是否取到了单价**。取不到时不该是 0 分（"免费"），
// 而应是不写（"不知道"）。
func usageCostUSD(ctx context.Context, model string, input, output int) (float64, bool) {
	model = strings.TrimSpace(model)
	if model == "" || db.GlobalDBManager == nil {
		return 0, false
	}
	entry := lookupPrice(ctx, model)
	if !entry.found {
		return 0, false
	}
	// cached 是 input 的子集（缓存命中那部分），所以输入侧总量仍是 input ——
	// 不能把 cached 再加一遍，否则输入 token 被算两遍、成本虚高。
	usd := float64(input)/1000*entry.inputPer1k + float64(output)/1000*entry.outputPer1k
	if usd <= 0 {
		// 价格为 0 或负（数据脏）：当成"不知道"，别写成免费。
		return 0, false
	}
	return usd, true
}

// usageCostCents 按模型单价把一次调用的 token 用量换算成**分**。
//
// 换算：`tokens/1000 * usd_per_1k * 100`，即 `tokens * usd_per_1k / 10`。
// 这是**逐行落库**用的口径：库里的 `cost_cents` 是 integer，只能存整数分。
func usageCostCents(ctx context.Context, model string, input, output, cached int) *int {
	usd, ok := usageCostUSD(ctx, model, input, output)
	if !ok {
		return nil
	}
	return roundCents(usd, input > 0 || output > 0)
}

// roundCents 把精确美元金额换算成整数分。
//
// hasUsage 表示"确实发生了 token 消耗"：此时金额不足 1 分也要记 1 分，
// 否则"用了一次但没花钱"与"模型免费"就混为一谈了。
func roundCents(usd float64, hasUsage bool) *int {
	if usd <= 0 {
		return nil
	}
	cents := int(math.Round(usd * 100))
	if cents == 0 && hasUsage {
		cents = 1
	}
	return &cents
}

// aggregateCostCents 汇总**整轮**成本。
//
// ## 为什么不能把逐行的分相加
//
// 逐行落库必须写整数分（`cost_cents` 是 integer），于是每行都会被
// `roundCents` 的「不足 1 分记 1 分」抬一次。三行各 0.3 分的调用相加得 3 分，
// 而它们的真实合计是 0.9 分 —— 贵了两倍多，且模型越多偏得越狠。所以整轮金额
// 必须由**未取整**的美元金额累加，最后只取整一次。
//
// ## unknown 为什么让结果是 nil 而不是部分和
//
// 只要有真实用量的那一行取不到单价，整轮金额就是**不可知**的。返回"已知行的和"
// 会让界面显示一个具体但偏小的数 —— 那比 NULL（"不知道"）更糟：用户没有任何
// 线索去怀疑它。
func aggregateCostCents(usdTotal float64, unknown, hasUsage bool) *int {
	if unknown || !hasUsage {
		return nil
	}
	return roundCents(usdTotal, hasUsage)
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
