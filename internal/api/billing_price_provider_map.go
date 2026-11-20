package api

// 模型 → 厂商的映射规则（导入反查与历史回填共用）。
//
// **一条规则**：只从 `llm_models` 目录（`name` 或 `id`）取厂商；同一模型对应
// **多个不同厂商**时不映射（歧义 ⇒ 不猜）；目录里没有就保持为空。
//
// 明确**禁止**按模型名前缀/关键词猜厂商（`claude-` → anthropic 这种）。猜错的厂商会把
// "某厂商的模型缺价"的统计做歪，而界面上看不出它是编的。用户补价依据的是自己的
// 网关证据，不是命名习惯。
//
// 之所以把它做成**纯函数**而不是散在两条 SQL 里：导入需要一个映射、回填需要另一个，
// 两处若各自实现"歧义怎么处理"，迟早出现"导入留空、回填随便挑了一个"的分叉 ——
// 而价格表是按 `(model, provider)` 唯一的两列，这种分叉会直接变成两行同模型价格。

import (
	"sort"
	"strings"
)

// modelProviderRow 是"模型名 + 厂商"的一行（目录行或价格行）。
type modelProviderRow struct {
	Model    string
	Provider string
}

// collapseProviderMap 把"模型 → 厂商候选"折成唯一映射。
//
// 同一模型名在目录里可能有多行（不同租户/网关重复注册）：只有候选厂商**只剩一个**
// 时才产出映射。否则宁可不填 —— provider 只影响归属展示与统计，填错比留空更糟。
func collapseProviderMap(candidates []modelProviderRow) map[string]string {
	sets := make(map[string]map[string]bool, len(candidates))
	for _, item := range candidates {
		model := strings.TrimSpace(item.Model)
		provider := strings.TrimSpace(item.Provider)
		if model == "" || provider == "" {
			continue
		}
		if sets[model] == nil {
			sets[model] = make(map[string]bool, 1)
		}
		sets[model][provider] = true
	}
	out := make(map[string]string, len(sets))
	for model, providers := range sets {
		if len(providers) != 1 {
			continue
		}
		for provider := range providers {
			out[model] = provider
		}
	}
	return out
}

// providerBackfillSample 是一条"将要把 model 的空 provider 改成 provider"的计划行。
type providerBackfillSample struct {
	Model    string `json:"model"`
	Provider string `json:"provider"`
}

// planProviderBackfill 计算"哪些空 provider 行会被改成什么"（纯函数，干跑与执行共用）。
//
// 输入是 DB 里的**全部**价格行 + 目录行，而不是只查"provider 为空的行"：
// 因为还要判断目标 `(model, provider)` 是否**已经有行**（主键冲突）—— 那种情况下
// 回填会撞唯一约束，只能跳过。把"是否冲突"放在 Go 里算，预览与执行就不可能给出
// 不同的答案。
func planProviderBackfill(priceRows, catalog []modelProviderRow) []providerBackfillSample {
	mapping := collapseProviderMap(catalog)

	occupied := make(map[string]bool, len(priceRows))
	pending := make([]modelProviderRow, 0, len(priceRows))
	for _, row := range priceRows {
		model := strings.TrimSpace(row.Model)
		if model == "" {
			continue
		}
		provider := strings.TrimSpace(row.Provider)
		occupied[model+"\x00"+provider] = true
		if provider == "" {
			pending = append(pending, modelProviderRow{Model: model})
		}
	}

	plan := make([]providerBackfillSample, 0, len(pending))
	seen := make(map[string]bool, len(pending))
	for _, row := range pending {
		target, ok := mapping[row.Model]
		if !ok {
			// 目录里没有这个模型，或目录里它对应多个厂商：**留空**。
			continue
		}
		if occupied[row.Model+"\x00"+target] {
			// `(model, target)` 已有行，回填会撞主键；这一行跳过（不是失败）。
			continue
		}
		if seen[row.Model] {
			continue
		}
		seen[row.Model] = true
		plan = append(plan, providerBackfillSample{Model: row.Model, Provider: target})
	}
	// 稳定顺序：预览示例与执行顺序都与模型名无关，便于核对与复现。
	sort.SliceStable(plan, func(i, j int) bool { return plan[i].Model < plan[j].Model })
	return plan
}
