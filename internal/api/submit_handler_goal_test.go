package api

import (
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/agentgoal"
)

func TestGoalEnforcePromptIncludesReasons(t *testing.T) {
	rep := &agentgoal.Report{
		Outcome: agentgoal.Incomplete,
		Reasons: []string{
			"verified-changes: 1 change(s) were not followed by a successful verification",
		},
	}
	p := goalEnforcePrompt(rep)
	if !strings.Contains(p, "verified-changes") {
		t.Fatalf("prompt must carry the owed reasons; got %q", p)
	}
	if !strings.Contains(p, "系统自动续跑") {
		t.Fatalf("prompt must say it is a system continuation, not a user question; got %q", p)
	}
}

func TestGoalEnforcePromptHandlesNil(t *testing.T) {
	p := goalEnforcePrompt(nil)
	if strings.TrimSpace(p) == "" {
		t.Fatalf("prompt must never be empty")
	}
}

func TestWithGoalEnforceDoesNotMutateInput(t *testing.T) {
	in := map[string]interface{}{"kb_id": "kb1"}
	out := withGoalEnforce(in)
	if _, leaked := in[GoalEnforceKey]; leaked {
		t.Fatalf("withGoalEnforce must not mutate the caller's map")
	}
	if out[GoalEnforceKey] != true {
		t.Fatalf("withGoalEnforce must set the marker")
	}
	if out["kb_id"] != "kb1" {
		t.Fatalf("withGoalEnforce must preserve existing keys")
	}
}

func TestGoalRoundsAreBounded(t *testing.T) {
	// 续跑轮数必须有界（否则失控任务永不收敛），但界不能小到装不下一次
	// 典型的多轮工具链（定位 → 改 → 跑测试 → 修）。此前断言 <=5，
	// 与"持续运行长线任务"的目标直接冲突：第 4 轮就被判 blocked。
	if n := agentgoal.DefaultGoalRounds; n < 8 || n > 60 {
		t.Fatalf("default goal rounds must fit a multi-round tool chain while staying bounded, got %d", n)
	}
	// 整条任务的总闸门必须明显宽于单个小目标 —— 否则"20 轮按小目标计"这个层级
	// 改造在总量上等于没做：第一个小目标就用光了整条任务的预算。
	total := agentgoal.DefaultMaxTotalRounds
	if total <= agentgoal.DefaultGoalRounds {
		t.Fatalf("the task-level total must exceed the per-sub-goal budget, got %d vs %d",
			total, agentgoal.DefaultGoalRounds)
	}
	if total/agentgoal.DefaultGoalRounds < 4 {
		t.Fatalf("the task-level total must cover several sub-goals, got %d/%d",
			total, agentgoal.DefaultGoalRounds)
	}
}

// TestSubmitChainBudgetCoversEveryGoalRound 守的是"长线任务能不能真的跑下去"这条线。
//
// 曾经的缺陷：单轮预算与续跑链预算共用一个 ctx。第一轮跑完，外层就是
// DeadlineExceeded，`HandleSubmit` 的续跑循环靠 `ctx.Err() != nil` 退出 ——
// agentgoal.MaxRounds 根本没被执行到。所以这里断言链级预算必须**宽于**单轮，
// 且足够容纳默认轮数，否则轮数上限只是个写在代码里的数字。
func TestSubmitChainBudgetCoversEveryGoalRound(t *testing.T) {
	turn := 5 * time.Minute
	goal := 24 * time.Hour

	got := submitChainTimeout(turn, goal)
	if got < goal {
		t.Fatalf("chain budget must not be narrowed below its own value; got %s", got)
	}

	// 整条任务的总轮数 × 单轮预算必须装得进链级预算。装不进来说明三层参数不自洽：
	// 轮数闸门还没用完，链级墙钟就先掐断了 —— 用户看到的是"轮数还有剩却停了"。
	//
	// 断言用 DefaultMaxTotalRounds（整条任务的闸门）而不是 DefaultGoalRounds
	// （单个小目标的闸门）：后者只覆盖一个清单项，链级预算要覆盖的是整条链。
	need := turn * time.Duration(agentgoal.DefaultMaxTotalRounds)
	if need > got {
		t.Fatalf("chain budget %s cannot hold %d total rounds of %s (need %s)",
			got, agentgoal.DefaultMaxTotalRounds, turn, need)
	}
}

// TestSubmitChainBudgetNeverBelowOneTurn 防止链级预算被配成比单轮还短。
//
// 这种配置下第一轮就会被链级限住，续跑链永远是零轮——比不配更糟，因为它
// 看起来"有预算"却不可能推进。
func TestSubmitChainBudgetNeverBelowOneTurn(t *testing.T) {
	turn := 30 * time.Minute
	got := submitChainTimeout(turn, 2*time.Minute)
	if got < turn {
		t.Fatalf("chain budget must be at least one turn (%s), got %s", turn, got)
	}
}

// TestSubmitChainBudgetFloorsTurnAtDefault 守住"配置为 0/负值"时的兜底。
func TestSubmitChainBudgetFloorsTurnAtDefault(t *testing.T) {
	got := submitChainTimeout(0, 0)
	if got < DefaultAgentTimeout {
		t.Fatalf("zero config must fall back to DefaultAgentTimeout (%s), got %s", DefaultAgentTimeout, got)
	}
}

// TestParseTodoItemsReadsOnlyWhatTheBudgetNeeds 守住"清单解析是 fail-soft"这条线。
//
// 它只服务一件事：把清单转成「当前小目标」指纹，让续跑预算按小目标计数。
// 因此解析失败必须返回 ok=false（调用方退化为"没有边界"），绝不能报错中断
// 这一轮 —— 一份形状不对的清单不该让用户的提交失败。
func TestParseTodoItemsReadsOnlyWhatTheBudgetNeeds(t *testing.T) {
	items, ok := parseTodoItems(`[{"id":"t1","content":"改配置","status":"in_progress","owner":"main"}]`)
	if !ok || len(items) != 1 {
		t.Fatalf("a well-formed checklist must parse; ok=%v items=%+v", ok, items)
	}
	if items[0].Content != "改配置" || items[0].Status != "in_progress" {
		t.Fatalf("must keep content and status, got %+v", items[0])
	}

	for _, bad := range []string{"", "   ", "not json", "{}", "null"} {
		if _, ok := parseTodoItems(bad); ok {
			t.Fatalf("payload %q must be rejected as unusable, not accepted", bad)
		}
	}
}

// TestTodoEventDrivesTheSubGoalBoundary 把"事件 → 指纹 → 重置"这条链接起来。
//
// 分开测每一段都不够：parseTodoItems 通过、SubGoalOf 通过，但两者拼不上
// （比如网关读了 status 却传了 content）时，预算仍然会按整条任务累加。
func TestTodoEventDrivesTheSubGoalBoundary(t *testing.T) {
	raw := `[{"content":"改配置","status":"completed"},{"content":"跑测试","status":"in_progress"}]`
	items, ok := parseTodoItems(raw)
	if !ok {
		t.Fatalf("checklist must parse")
	}
	fp := agentgoal.SubGoalOf(items)
	if fp == "" {
		t.Fatalf("an in-progress item must yield a boundary")
	}

	g := agentgoal.NewGoal("g1", "s1", "long task", 0)
	g.RoundsUsed = 19
	g.TotalRounds = 19
	g.SubGoal = "previous-item"
	g = agentgoal.BeginRound(g, fp)
	if g.RoundsUsed != 0 {
		t.Fatalf("advancing the checklist must reset the per-sub-goal budget, got %d", g.RoundsUsed)
	}
	if g.TotalRounds != 19 {
		t.Fatalf("the task-level total must survive a sub-goal change, got %d", g.TotalRounds)
	}
}
