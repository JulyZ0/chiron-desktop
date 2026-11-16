package api

import "testing"

// 按模型归因（`usage_events` 逐行写入的依据）。
//
// ## 修的是什么
//
// 一次提交可以跨多个模型：主模型 + fallback、上下文摘要、子 Agent 的外层调用。
// 而 `done` 只给**整轮合计**、不给归属，此前的实现是拿"最后一个非空模型名"
// 去乘全部 token —— 整轮花费被算成"全都在那个模型上跑"。单价差几倍时，
// 成本就直接错几倍，计费页的"钱花在哪个模型上"也就无从看起。
//
// `usage` 事件其实一直带 `model`，所以按模型拆的原始数据本来就存在，只是没人用。

// 整轮 token 必须按各自模型的单价折算，而不是全压在一个模型上。
func TestModelLedgerAttributesEachCallToItsOwnModel(t *testing.T) {
	var ledger modelLedger
	ledger.add("cheap-model", 1000, 100, 0)
	ledger.add("expensive-model", 1000, 100, 0)

	total := usageTotals{input: 2000, output: 200}
	rows := ledger.settle(total, "expensive-model")

	if len(rows) != 2 {
		t.Fatalf("两个模型应产生两行明细，得到 %d 行：%+v", len(rows), rows)
	}
	got := map[string]usageTotals{}
	for _, r := range rows {
		got[r.Model] = r.usageTotals
	}
	if got["cheap-model"].input != 1000 || got["expensive-model"].input != 1000 {
		t.Fatalf("每个模型应各自拿到自己的增量，得到 %+v", got)
	}
}

// **总额不变式**：归因只改粒度，绝不改总额。
//
// 这条是「明细加起来对不上这一轮的扣费」的唯一防线。
func TestModelLedgerNeverChangesTheTurnTotal(t *testing.T) {
	var ledger modelLedger
	ledger.add("a", 300, 40, 120)
	ledger.add("b", 500, 60, 200)
	ledger.add("a", 200, 20, 80)

	total := usageTotals{input: 1000, output: 120, cached: 400}
	rows := ledger.settle(total, "a")

	sum := usageTotals{}
	for _, r := range rows {
		sum.input += r.input
		sum.output += r.output
		sum.cached += r.cached
	}
	if sum != total {
		t.Fatalf("各行之和必须等于整轮入账量 %+v，得到 %+v", total, sum)
	}
	// 同一个模型多次调用要合并成一行，而不是各占一行。
	if len(rows) != 2 {
		t.Fatalf("同一模型的多次调用应合并，得到 %d 行：%+v", len(rows), rows)
	}
}

// `done` 里有增量没覆盖到的部分（某次调用没发 `usage`）：差额并到主模型那行。
func TestModelLedgerPutsTheGapOnTheFallbackModel(t *testing.T) {
	var ledger modelLedger
	ledger.add("main", 800, 80, 0)

	// 整轮合计比增量多 200 输入 / 20 输出：那段没发 usage 的调用只能记在主模型上。
	total := usageTotals{input: 1000, output: 100}
	rows := ledger.settle(total, "main")

	if len(rows) != 1 {
		t.Fatalf("差额应并入已存在的行而不是新增一行，得到 %+v", rows)
	}
	if rows[0].Model != "main" || rows[0].input != 1000 || rows[0].output != 100 {
		t.Fatalf("差额必须并到 fallback 模型行，得到 %+v", rows[0])
	}
}

// fallback 模型不在增量里时，差额单独成行 —— 不能假装它属于某个具体模型。
func TestModelLedgerCreatesAGapRowWhenFallbackHasNoIncrements(t *testing.T) {
	var ledger modelLedger
	ledger.add("sub", 500, 50, 0)

	rows := ledger.settle(usageTotals{input: 800, output: 80}, "main")

	if len(rows) != 2 {
		t.Fatalf("应保留 sub 那行，并为差额补一行，得到 %+v", rows)
	}
	var gapRow *modelUsage
	for i := range rows {
		if rows[i].Model == "main" {
			gapRow = &rows[i]
		}
	}
	if gapRow == nil {
		t.Fatalf("差额行应挂在 fallback 模型名下，得到 %+v", rows)
	}
	if gapRow.input != 300 || gapRow.output != 30 {
		t.Fatalf("差额行应只含未被覆盖的部分，得到 %+v", *gapRow)
	}
}

// 增量反而多于整轮合计：两份数据打架，退回单行只保总额。
func TestModelLedgerFallsBackToASingleRowWhenDataDisagrees(t *testing.T) {
	var ledger modelLedger
	ledger.add("a", 900, 90, 0)
	ledger.add("b", 900, 90, 0)

	// 整轮合计反而更小 —— 负数差额不可能靠"并到某行"修正。
	rows := ledger.settle(usageTotals{input: 100, output: 10}, "a")

	if len(rows) != 1 {
		t.Fatalf("数据打架时必须退回单行，得到 %+v", rows)
	}
	if rows[0].input != 100 || rows[0].output != 10 {
		t.Fatalf("退回单行时总额仍必须准确，得到 %+v", rows[0])
	}
}

// 没有增量（旧引擎 / 被取消）：归因退化为旧行为，但总额不能丢。
func TestModelLedgerWithoutIncrementsKeepsTheTotal(t *testing.T) {
	var ledger modelLedger

	rows := ledger.settle(usageTotals{input: 700, output: 70}, "main")

	if len(rows) != 1 || rows[0].Model != "main" {
		t.Fatalf("无增量时应给一行、模型名取 fallback，得到 %+v", rows)
	}
	if rows[0].input != 700 || rows[0].output != 70 {
		t.Fatalf("无增量时总额仍必须准确，得到 %+v", rows[0])
	}
}

// 真正零用量的回合不产生明细行（调用方会自行补一行"零 token"占位）。
func TestModelLedgerReturnsNothingForAnEmptyTurn(t *testing.T) {
	var ledger modelLedger

	if rows := ledger.settle(usageTotals{}, "main"); len(rows) != 0 {
		t.Fatalf("零用量不应产生归因行，得到 %+v", rows)
	}
}

// 模型名缺失时归入空键，而不是丢弃那次调用 —— 丢了总额就少了。
func TestModelLedgerKeepsCallsWhoseModelIsUnknown(t *testing.T) {
	var ledger modelLedger
	ledger.add("", 400, 40, 0)

	rows := ledger.settle(usageTotals{input: 400, output: 40}, "main")

	if len(rows) != 1 || rows[0].input != 400 {
		t.Fatalf("模型名缺失的调用仍须计入，得到 %+v", rows)
	}
}

// 成本必须**逐行**按各自模型单价折算 —— 这正是分行的目的。
//
// 用假价格表钉住：若把两行合并成一行再算，得到的金额会与这里不同。
func TestTurnCostIsSummedPerModelNotFromASingleRate(t *testing.T) {
	// 这里不碰数据库（`usageCostCents` 在无价格时返回 nil），只断言**聚合方式**：
	// 相信柜账的行数 === 需要独立定价的次数。
	var ledger modelLedger
	ledger.add("cheap", 100_000, 0, 0)  // 10 万 token
	ledger.add("pricey", 100_000, 0, 0) // 同样 10 万 token，但贵得多

	rows := ledger.settle(usageTotals{input: 200_000}, "pricey")
	if len(rows) != 2 {
		t.Fatalf("两次不同模型的调用必须分开计价，得到 %+v", rows)
	}
	for _, r := range rows {
		if r.input != 100_000 {
			t.Fatalf("每行应只含自己模型的 token，得到 %+v", r)
		}
	}
}

// **整轮只取整一次**：这是逐行落库引入的新风险。
//
// 三行各 0.3 分的调用，若把已取整的行相加会得 3 分（每行被抬到 1 分），
// 而真实合计是 0.9 分 ⇒ 1 分。模型越多，错误越大。
func TestRoundingHappensOnceForTheWholeTurn(t *testing.T) {
	// 每行 0.003 美元 = 0.3 分。三行合计 0.009 美元 = 0.9 分 ⇒ 取整 1 分。
	perRowUSD := 0.003
	rows := 3

	// 逐行取整后相加（错误做法）——用它来证明正确做法确实不同。
	wrongTotal := 0
	for i := 0; i < rows; i++ {
		c := roundCents(perRowUSD, true)
		if c == nil {
			t.Fatalf("有用量时不该返回 nil")
		}
		wrongTotal += *c
	}
	if wrongTotal != 3 {
		t.Fatalf("前提校验：逐行取整应得 3 分，得到 %d 分", wrongTotal)
	}

	// 正确做法：精确累加后只取整一次。
	got := aggregateCostCents(perRowUSD*float64(rows), false, true)
	if got == nil {
		t.Fatalf("价格齐全时不该返回 nil")
	}
	if *got != 1 {
		t.Fatalf("整轮应只取整一次得 1 分，得到 %d 分（逐行相加会得 %d 分）", *got, wrongTotal)
	}
}

// 有真实用量却取不到单价：整轮成本必须"不知道"（nil），不能是部分和。
func TestAggregateCostIsUnknownWhenAnyRowHasNoPrice(t *testing.T) {
	if got := aggregateCostCents(0.5, true, true); got != nil {
		t.Fatalf("任一行缺价格时整轮成本应为 nil（不知道），得到 %d 分", *got)
	}
}

// 完全没有用量的回合：成本是 nil 而不是 0 分。
func TestAggregateCostIsNilWithoutUsage(t *testing.T) {
	if got := aggregateCostCents(0, false, false); got != nil {
		t.Fatalf("零用量不该产生成本，得到 %d 分", *got)
	}
}

// 不足 1 分的**单行**仍记 1 分：否则"用了一次却免费"无法与"模型免费"区分。
func TestSubCentUsageIsChargedOneCent(t *testing.T) {
	got := roundCents(0.0001, true) // 0.01 分
	if got == nil {
		t.Fatalf("有用量时不该返回 nil")
	}
	if *got != 1 {
		t.Fatalf("不足 1 分应记 1 分，得到 %d", *got)
	}
}

// 金额为 0 或负（价格数据脏）：不写成本，而不是写成免费。
func TestNonPositiveAmountHasNoCost(t *testing.T) {
	if got := roundCents(0, true); got != nil {
		t.Fatalf("金额为 0 时不该写成本，得到 %d", *got)
	}
	if got := roundCents(-1, true); got != nil {
		t.Fatalf("金额为负时不该写成本，得到 %d", *got)
	}
}

