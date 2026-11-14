package api

import (
	"sort"
	"strings"
)

// 引擎事件的**用量通道**：`done` 给**整轮累计**，`usage` 给**按次增量**（C3）。
//
// 为什么值得单独一个类型与单测：这里的输出直接喂 `DeductTokens` —— 多算一次就是
// **多扣用户的钱**；而"按字段非零就累加"这种写法在新增事件类型时会**静默**重复计。

// usageEventType 是引擎按**每次 LLM 调用**下发的用量事件
// （`python-engine/app/agent/runtime.py` 的 C3 通道，字段为本次调用的增量）。
const usageEventType = "usage"

// subagentEventPrefix 是子 Agent 进度事件的类型前缀（见 python-engine/app/agent/event_sink.py）。
const subagentEventPrefix = "subagent."

// usageTotals 是一轮对话的**累计**用量。
type usageTotals struct {
	input  int
	output int
	cached int

	// 逐次调用增量之和：**只在 `done` 完全没带 token 时兜底**。
	//
	// 它绝不能与 `input/output/cached` 相加 —— 那就是重复计费。它存在的理由是：
	// `done` 在若干异常路径上没有 token（引擎旧版本、被取消、流被截断），
	// 此时整轮用量会**静默变成 0**，用户在统计面板上看到"输入 0 / 输出 0"，
	// 而 `usage` 增量其实一路上都有。
	fallbackInput  int
	fallbackOutput int
	fallbackCached int
}

// withEvent 把一条事件里的用量并入累计。
//
// ⚠ 按**事件类型**分流，而不是"字段非零就累加"：
//   - `done`（引擎终态事件）携带的是**整轮累计**，是唯一该并入的来源；
//   - `usage` 携带的是**本次调用**的增量，它自己就是一条独立通道；若也并进来，
//     就会与 `done` 的累计**重复计一遍**，并顺着 `DeductTokens` 放大成多扣费。
//   - `subagent.*` 一律**不并入**（M15d/2B 的口径）：子 Agent 的 token 只进引擎侧的
//     任务线账（`subagent_runs` + `run_lineage_ledger`），由线账闸门与预算待办来约束，
//     不进租户额度池。引擎今天只把它的用量放在 `usage` 这个**嵌套字段**里（父帧的
//     扁平 `input_tokens/output_tokens` 为 0），所以此处本来就不会误计 —— 但那个"不会"
//     依赖引擎的序列化细节，一旦哪天有人把嵌套 usage 摊平、或给子 Agent 帧补上扁平字段，
//     这里就会**静默地**把子 Agent 的花费算进父 turn 的账单并扣到租户头上。宁可在唯一
//     的入账入口把它挡死，也不要留一个"靠对端不出错"的约定。
func (t usageTotals) withEvent(eventType string, input, output, cached int) usageTotals {
	if strings.HasPrefix(eventType, subagentEventPrefix) {
		return t
	}
	// `usage` 是按次增量：不进主累计（会重复计费），只单独攒起来当兜底。
	if eventType == usageEventType {
		t.fallbackInput += input
		t.fallbackOutput += output
		t.fallbackCached += cached
		return t
	}
	// `done` 携带整轮累计，走下面的通用累加；`usage` 已在上方单独处理。
	if input > 0 {
		t.input += input
	}
	if output > 0 {
		t.output += output
	}
	if cached > 0 {
		t.cached += cached
	}
	return t
}

// settled 返回**该入账**的用量：`done` 的累计优先，缺失时才用增量之和兜底。
//
// 为什么需要它（用户可见的 bug）：`done` 在异常路径上可能不带 token
// （引擎旧版本、被取消、流被截断），此时 `input/output/cached` 全是 0，
// 统计面板上就显示"输入 0 / 输出 0 / 费用 $0.0000"——而 `usage` 增量
// 其实一路都在。这既让用户看不到真实消耗，也让**计费漏掉**这些回合。
//
// 兜底是**互斥**的：只要 `done` 给了任一非零用量，就完全信任它，
// 绝不把增量再加一遍。所以不存在重复计费。
func (t usageTotals) settled() usageTotals {
	if t.input > 0 || t.output > 0 || t.cached > 0 {
		return usageTotals{input: t.input, output: t.output, cached: t.cached}
	}
	// 走到这里说明 `done` 没给任何 token —— 无论是"给了 0"还是"根本没到"，
	// 增量之和都是唯一能拿到的量。
	return usageTotals{
		input:  t.fallbackInput,
		output: t.fallbackOutput,
		cached: t.fallbackCached,
	}
}

// modelUsage 是一条**按模型**的用量明细。
type modelUsage struct {
	Model string `json:"model"`
	usageTotals
}

// modelLedger 按模型归集**每次 LLM 调用**的增量（`usage` 事件的 `model` 字段）。
//
// ## 为什么必须按模型拆
//
// 一次提交可以跨多个模型：主模型 + fallback + 子 Agent 外层调用 + 摘要。而
// `done` 只给**整轮合计**，不给归属。此前的做法是拿"最后一个非空模型名"去乘
// 全部 token —— 于是整轮的花费被算成"全都在那个模型上跑"，单价差几倍时成本
// 直接错几倍。这不是精度问题，是归因错误。
//
// `usage` 事件恰好带 `model`，所以按模型拆的原始数据一直都在，只是没人用。
//
// ## 为什么不改 usageTotals
//
// 给 `usageTotals` 加 map 字段会让它不可比较（`==` 编译失败），而它被
// `withEvent` 的单测当成值类型断言（`got != usageTotals{}`）。归因是**另一个
// 维度**，不该塞进被计费守卫钉住的那个类型里。
type modelLedger struct {
	entries map[string]usageTotals
}

// add 记录一次调用的增量。model 为空时归入"未知"档（键为空串）。
func (l *modelLedger) add(model string, input, output, cached int) {
	if input == 0 && output == 0 && cached == 0 {
		return
	}
	if l.entries == nil {
		l.entries = make(map[string]usageTotals)
	}
	key := strings.TrimSpace(model)
	cur := l.entries[key]
	if input > 0 {
		cur.input += input
	}
	if output > 0 {
		cur.output += output
	}
	if cached > 0 {
		cur.cached += cached
	}
	l.entries[key] = cur
}

// settle 把按模型归集的增量对齐到**整轮入账量** total。
//
// 返回的每一行都要写进 `usage_events`，各行之和**恒等于** total —— 归因只改变
// 粒度，绝不改变总额。这条不变式是这里全部逻辑的理由：总额一旦被归因逻辑改动，
// 就会出现"明细加起来对不上这一轮的扣费"。
//
// 三种情形：
//
//   - 没有增量（旧引擎 / 全零）：只能给一行，模型名用 fallbackModel。此时归因
//     退化为旧行为，但总额仍取自 `settled()`，不会漏计。
//   - 增量合计与 total 一致（正常路径）：逐模型返回，成本可按各自单价精确折算。
//   - 不一致：`done` 里有增量没覆盖到的部分（例如某次调用没发 `usage`）。把差额
//     并到 fallbackModel 那一行 —— 差额是真实发生过的用量，只能记在主模型名下。
//     若差额为负（增量反而多于整轮合计，理论上不该出现），说明两份数据有一份
//     不可信，整轮退回单行，只保证总额正确。
func (l *modelLedger) settle(total usageTotals, fallbackModel string) []modelUsage {
	sum := usageTotals{}
	for _, e := range l.entries {
		sum.input += e.input
		sum.output += e.output
		sum.cached += e.cached
	}

	keys := make([]string, 0, len(l.entries))
	for k := range l.entries {
		keys = append(keys, k)
	}
	sort.Strings(keys) // 输出顺序稳定，便于对账与快照测试

	fallback := strings.TrimSpace(fallbackModel)

	if len(keys) == 0 {
		if total.input == 0 && total.output == 0 && total.cached == 0 {
			return nil // 这一轮确实没有任何用量，不写空行
		}
		return []modelUsage{{Model: fallback, usageTotals: total}}
	}

	gap := usageTotals{
		input:  total.input - sum.input,
		output: total.output - sum.output,
		cached: total.cached - sum.cached,
	}
	if gap.input < 0 || gap.output < 0 || gap.cached < 0 {
		// 增量多于整轮合计：两份数据打架，不信归因，只保总额。
		return []modelUsage{{Model: fallback, usageTotals: total}}
	}

	out := make([]modelUsage, 0, len(keys)+1)
	for _, k := range keys {
		out = append(out, modelUsage{Model: k, usageTotals: l.entries[k]})
	}
	if gap.input > 0 || gap.output > 0 || gap.cached > 0 {
		merged := false
		for i := range out {
			if out[i].Model == fallback {
				out[i].input += gap.input
				out[i].output += gap.output
				out[i].cached += gap.cached
				merged = true
				break
			}
		}
		if !merged {
			// 空模型名 + 差额：明确标出"这部分用量不知道是哪个模型"，
			// 而不是假装它属于某个具体模型。
			out = append(out, modelUsage{
				Model:       fallback,
				usageTotals: gap,
			})
		}
	}
	return out
}
