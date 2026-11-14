package agentgoal

import (
	"strings"
	"testing"
)

func TestAdvanceCompletedStops(t *testing.T) {
	g := NewGoal("g1", "s1", "do the thing", 100)
	g, cont := Advance(g, Report{Outcome: Completed}, 200)
	if cont {
		t.Fatalf("completed goal must not continue")
	}
	if g.Status != GoalCompleted {
		t.Fatalf("want completed, got %s", g.Status)
	}
	if g.UpdatedAt != 200 {
		t.Fatalf("timestamp must be host-written")
	}
}

func TestAdvanceIncompleteContinuesUntilBudget(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g.MaxRounds = 2

	// 第一次未完成 → 继续（RoundsUsed=1）
	g, cont := Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 1)
	if !cont || g.Status != GoalActive || g.RoundsUsed != 1 {
		t.Fatalf("first incomplete should continue: %+v cont=%v", g, cont)
	}

	// 到上限 → blocked，不再继续
	g, cont = Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 2)
	if cont {
		t.Fatalf("exhausted budget must not continue")
	}
	if g.Status != GoalBlocked {
		t.Fatalf("want blocked at budget end, got %s", g.Status)
	}
	if g.RoundsUsed != 2 {
		t.Fatalf("want rounds_used=2, got %d", g.RoundsUsed)
	}
}

func TestAdvanceFailedBlocksWithoutContinuing(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g, cont := Advance(g, Report{Outcome: Failed, Reasons: []string{"no-error: boom"}}, 1)
	if cont {
		t.Fatalf("failed turn must not auto-continue")
	}
	if g.Status != GoalBlocked {
		t.Fatalf("want blocked, got %s", g.Status)
	}
}

func TestAdvanceBlockedIsAbandoned(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g, cont := Advance(g, BlockedReport("deadline"), 1)
	if cont {
		t.Fatalf("cancelled turn must not continue")
	}
	if g.Status != GoalAbandoned {
		t.Fatalf("want abandoned, got %s", g.Status)
	}
}

func TestAdvanceNeverMarksCompletedOnIncomplete(t *testing.T) {
	// 核心纪律：没做完就是没做完 —— 绝不因为"到了上限"而谎报 completed。
	g := NewGoal("g1", "s1", "x", 0)
	g.MaxRounds = 1
	g, _ = Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 1)
	if g.Status == GoalCompleted {
		t.Fatalf("incomplete must never become completed")
	}
}

func TestEffectiveMaxRoundsDefault(t *testing.T) {
	if NewGoal("g", "s", "d", 0).EffectiveMaxRounds() != DefaultGoalRounds {
		t.Fatalf("zero MaxRounds must fall back to the default")
	}
}

func TestTerminal(t *testing.T) {
	if !(Goal{Status: GoalCompleted}).Terminal() {
		t.Fatalf("completed is terminal")
	}
	if !(Goal{Status: GoalBlocked}).Terminal() {
		t.Fatalf("blocked is terminal")
	}
	if (Goal{Status: GoalActive}).Terminal() {
		t.Fatalf("active is not terminal")
	}
}

// TestTruncatedTurnKeepsTheChainAlive 是"长线任务能不能真的跑下去"的端到端语义链：
// Evaluate 把单轮 deadline 判成 Incomplete → Advance 让它继续。两段必须同时成立，
// 只修其中一段（比如只放宽网关预算）都不足以让链真正续上。
func TestTruncatedTurnKeepsTheChainAlive(t *testing.T) {
	g := NewGoal("g1", "s1", "long task", 0)
	g.MaxRounds = 3

	rep := Evaluate(nil, Signals{Truncated: true})
	if rep.Outcome != Incomplete {
		t.Fatalf("truncated turn must evaluate to incomplete, got %s", rep.Outcome)
	}

	g, cont := Advance(g, rep, 1)
	if !cont {
		t.Fatalf("a truncated turn must keep the chain running; status=%s reason=%s", g.Status, g.LastReason)
	}
	if g.Status != GoalActive {
		t.Fatalf("want active after truncation, got %s", g.Status)
	}
	if g.RoundsUsed != 1 {
		t.Fatalf("truncation must consume a round so the chain stays bounded; got %d", g.RoundsUsed)
	}
}

// TestCancelledTurnStopsTheChain 守住另一半：用户叫停不能被当成"可以继续"。
func TestCancelledTurnStopsTheChain(t *testing.T) {
	g := NewGoal("g1", "s1", "long task", 0)
	g.MaxRounds = 3

	rep := Evaluate(nil, Signals{Cancelled: true})
	g, cont := Advance(g, rep, 1)
	if cont {
		t.Fatalf("an explicitly cancelled turn must not continue")
	}
	if g.Status == GoalCompleted {
		t.Fatalf("cancellation must never be recorded as completion")
	}
}

// TestRoundsResetWhenTheSubGoalAdvances 是"20 轮拦小目标、不拦整条任务"的核心断言。
//
// 层级错误的表现：RoundsUsed 一直按整条任务累加，于是第 20 轮之后整条链判 blocked ——
// 一条由多个清单项串起来的长任务走不完第二件事。这里走满第一个小目标的预算，
// 然后切到第二个小目标，断言计数归零且链继续。
func TestRoundsResetWhenTheSubGoalAdvances(t *testing.T) {
	g := NewGoal("g1", "s1", "long task", 0)
	g.MaxRounds = 2

	first := SubGoalOf([]SubGoalItem{{Content: "改配置", Status: "in_progress"}})
	if first == "" {
		t.Fatalf("an in-progress item must yield a sub-goal fingerprint")
	}
	g = BeginRound(g, first)

	// 第一个小目标跑满预算 → blocked（这一件事卡住了，如实说没做完）。
	for i := 0; i < 2; i++ {
		var cont bool
		g, cont = Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, int64(i+1))
		if !cont {
			break
		}
	}
	if g.Status != GoalBlocked {
		t.Fatalf("a stuck sub-goal must hit its own budget; status=%s", g.Status)
	}

	// 推进到第二个小目标：单小目标计数必须归零，任务级累计必须保留。
	g.Status = GoalActive // 模型/用户换个方向继续（调用方会重新置 active）
	second := SubGoalOf([]SubGoalItem{
		{Content: "改配置", Status: "completed"},
		{Content: "跑测试", Status: "in_progress"},
	})
	if second == first {
		t.Fatalf("a different item must yield a different fingerprint")
	}
	before := g.TotalRounds
	g = BeginRound(g, second)
	if g.RoundsUsed != 0 {
		t.Fatalf("advancing to the next sub-goal must reset the per-sub-goal counter, got %d", g.RoundsUsed)
	}
	if g.TotalRounds != before {
		t.Fatalf("the task-level total must never reset (was %d, got %d)", before, g.TotalRounds)
	}

	// 新小目标有完整的 20 轮预算：一次未完成应当继续，而不是立刻判 blocked。
	g, cont := Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, 99)
	if !cont || g.Status != GoalActive {
		t.Fatalf("a fresh sub-goal must get its own rounds; status=%s cont=%v", g.Status, cont)
	}
}

// TestTotalRoundsStopsTheWholeTask 守住成本闸门：小目标可以换，任务总预算不能无限。
func TestTotalRoundsStopsTheWholeTask(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g.MaxRounds = 100    // 单小目标放得很宽
	g.MaxTotalRounds = 2 // 但整条任务只给 2 轮

	var cont bool
	for i := 0; i < 3; i++ {
		g, cont = Advance(g, Report{Outcome: Incomplete, Reasons: []string{"owed"}}, int64(i))
		if !cont {
			break
		}
	}
	if cont || g.Status != GoalBlocked {
		t.Fatalf("the task-level total must stop the chain; status=%s cont=%v", g.Status, cont)
	}
	if g.TotalRounds != 2 {
		t.Fatalf("want total_rounds=2, got %d", g.TotalRounds)
	}
	if !strings.Contains(g.LastReason, "task continuation budget") {
		t.Fatalf("the reason must name the task-level gate, got %q", g.LastReason)
	}
}

// TestSubGoalOfPrefersInProgressThenFirstPending 固定"当前小目标"的选取规则。
func TestSubGoalOfPrefersInProgressThenFirstPending(t *testing.T) {
	items := []SubGoalItem{
		{Content: "A", Status: "completed"},
		{Content: "B", Status: "pending"},
		{Content: "C", Status: "in_progress"},
	}
	if got := SubGoalOf(items); got != SubGoalOf([]SubGoalItem{{Content: "C"}}) {
		t.Fatalf("in_progress must win over pending; got %q", got)
	}

	// 没有进行中的 → 取第一个未完成（刚写完清单、还没开工时也要有边界）。
	got := SubGoalOf([]SubGoalItem{
		{Content: "A", Status: "completed"},
		{Content: "B", Status: "pending"},
	})
	if got != SubGoalOf([]SubGoalItem{{Content: "B"}}) {
		t.Fatalf("with nothing in progress, the first pending item is the boundary")
	}

	// 没有清单 → 没有边界（保守：整条任务算一个目标，20 轮不重置）。
	if got := SubGoalOf(nil); got != "" {
		t.Fatalf("an empty checklist must yield no boundary, got %q", got)
	}
	// 全部完成 → 也没有边界（没有下一件事要推进）。
	if got := SubGoalOf([]SubGoalItem{{Content: "A", Status: "completed"}}); got != "" {
		t.Fatalf("an all-completed checklist has no next item, got %q", got)
	}
}

// TestBeginRoundIgnoresEmptyFingerprint 守住"没有清单时不重置计数"。
func TestBeginRoundIgnoresEmptyFingerprint(t *testing.T) {
	g := NewGoal("g1", "s1", "x", 0)
	g.RoundsUsed = 7
	if got := BeginRound(g, ""); got.RoundsUsed != 7 || got.SubGoal != "" {
		t.Fatalf("an empty fingerprint must leave the goal untouched, got %+v", got)
	}
}
