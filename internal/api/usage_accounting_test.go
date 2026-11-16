package api

import "testing"

// 计费口径的回归测试：`usage`（按次增量）**不得**并入整轮累计。
//
// 背景：引擎 C3 起按**每次 LLM 调用**下发 `usage`（增量），而终态 `done` 仍给整轮累计。
// 若累计器按"token 字段非零就相加"实现，同一次调用会被计两遍，而它直接进
// `DeductTokens` / `FinishTurn` —— 表现为**用户被多扣费**、缓存命中率也被算错。
func TestUsageEventIsNotCountedIntoRunTotals(t *testing.T) {
	var got usageTotals

	// ① 按次增量：**主累计**必须被忽略（否则与 done 的累计重复）。
	//    它可以进 `fallback*`（那是"done 没带 token 时的兜底"），
	//    但绝不能进 `input/output/cached` —— 后者直接喂 DeductTokens。
	got = got.withEvent(usageEventType, 100, 50, 10)
	if got.input != 0 || got.output != 0 || got.cached != 0 {
		t.Fatalf("usage 是按次增量，不得并入整轮主累计（会重复计费），得到 %+v", got)
	}

	// ② 终态事件的累计值：必须并入主累计。
	got = got.withEvent("done", 1000, 200, 30)
	if got.input != 1000 || got.output != 200 || got.cached != 30 {
		t.Fatalf("done 的累计值必须并入，得到 %+v", got)
	}
	// 且此时结算结果必须**完全等于** done 的累计，不含增量 —— 这是不重复计费的断言。
	if s := got.settled(); s.input != 1000 || s.output != 200 || s.cached != 30 {
		t.Fatalf("done 非零时 settled 必须原样返回 done 的累计，得到 %+v", s)
	}

	// ③ 其它事件若带用量字段（历史或未来新增）仍照常按累计处理：只针对 usage 特判，
	//    避免"顺手把累加整体关掉"这种过度修正。
	got = got.withEvent("text", 5, 7, 0)
	if got.input != 1005 || got.output != 207 || got.cached != 30 {
		t.Fatalf("非 usage 事件应照常并入，得到 %+v", got)
	}
}

// `done` 没带 token 时（取消 / 断线 / 旧引擎）必须回落到增量之和。
//
// 这是用户可见的 bug：统计面板上"输入 0 / 输出 0 / 费用 $0.0000"，
// 而 `usage` 增量其实一路都在 —— 既看不到真实消耗，计费也漏掉这些回合。
func TestSettledFallsBackToUsageIncrementsWhenDoneHasNoTokens(t *testing.T) {
	var got usageTotals
	// 三次 LLM 调用，每次都有增量。
	got = got.withEvent(usageEventType, 300, 40, 120)
	got = got.withEvent(usageEventType, 500, 60, 200)
	got = got.withEvent(usageEventType, 200, 20, 80)

	t.Run("done 到达但不带 token", func(t *testing.T) {
		s := got.withEvent("done", 0, 0, 0).settled()
		if s.input != 1000 || s.output != 120 || s.cached != 400 {
			t.Fatalf("done 全零时必须回落到增量之和，得到 %+v", s)
		}
	})

	t.Run("根本没有 done（断线/超时）", func(t *testing.T) {
		s := got.settled()
		if s.input != 1000 || s.output != 120 || s.cached != 400 {
			t.Fatalf("没有终态事件时也必须回落到增量之和，得到 %+v", s)
		}
	})

	t.Run("done 只给了一部分字段", func(t *testing.T) {
		// 只要 done 给了任一项非零，就完全信任它，不再叠加增量 —— 混合拼接
		// 会让"输入取自 done、输出取自增量"这种半真半假的数出现，比全零更糟。
		s := got.withEvent("done", 1200, 0, 0).settled()
		if s.input != 1200 || s.output != 0 || s.cached != 0 {
			t.Fatalf("done 给出非零时不得再叠加增量，得到 %+v", s)
		}
	})
}

// M15d（方案 2B）：子 Agent 的 token 只进引擎侧的任务线账，**不进租户额度池**。
//
// 引擎今天把子 Agent 的用量放在 `subagent.done` 的**嵌套** `usage` 字段里，父帧的扁平
// `input_tokens/output_tokens` 是 0 —— 所以"不并入"过去只是一个巧合。这一条把它变成契约：
// 只要事件类型带 `subagent.` 前缀，即便带上了扁平 token 也不许进入这里的累计，
// 因为它下游就是 `DeductTokens` / `RecordTokenUsage` / `TrueUpTenantQuota`。
func TestSubagentEventsNeverChargeTheTenantPool(t *testing.T) {
	for _, et := range []string{"subagent.done", "subagent.text", "subagent.status", "subagent.notice"} {
		var got usageTotals
		got = got.withEvent(et, 4000, 900, 120)
		if got != (usageTotals{}) {
			t.Fatalf("%s 的用量不得并入租户累计（子 Agent 只记线账），得到 %+v", et, got)
		}
	}

	// 前缀判定不能顺手把主对话的终态也挡掉：done 仍要计。
	var got usageTotals
	got = got.withEvent("subagent.done", 4000, 900, 0).withEvent("done", 10, 2, 1)
	if got.input != 10 || got.output != 2 || got.cached != 1 {
		t.Fatalf("父 turn 的 done 必须照常计入，得到 %+v", got)
	}
}
