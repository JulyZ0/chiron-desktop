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

// TestGoalEnforcePromptFeedsTheBudgetWallBack 是回灌层的核心断言。
//
// 撞墙这一轮如果只说"继续完成"，模型会把段级额度用完当成任务失败（道歉、重读、
// 重做），下一段照样烧完就停。必须明确告诉它：这是额度用完、不是失败，已完成的
// 都保存了，而新的一段额度同样有限 —— 于是它才会先收尾而不是先探索。
func TestGoalEnforcePromptFeedsTheBudgetWallBack(t *testing.T) {
	rep := &agentgoal.Report{
		Outcome:    agentgoal.Incomplete,
		BudgetAxis: "tokens",
		Reasons:    []string{"the segment stopped on the tokens budget"},
		Pending:    []string{"跑测试", "更新文档"},
	}
	g := agentgoal.NewGoal("g1", "s1", "long task", 0)
	p := goalEnforcePromptFor(g, rep)

	for _, want := range []string{
		"tokens", // 点名撞的哪一档
		"不是失败",   // 明确否定"任务失败了"
		"跑测试",    // 把模型自己写的未完成项点出来
		"更新文档",   // 全部列出，不许只提一条
		"还剩",     // 剩下的轮次可见
		"系统自动续跑", // 不伪装成用户新提问
	} {
		if !strings.Contains(p, want) {
			t.Fatalf("continuation prompt must carry %q; got:\n%s", want, p)
		}
	}
}

// TestGoalEnforcePromptOmitsEmptySections：没有撞墙、没有清单时不许出现空标题 ——
// 空的"未满足项："会让模型以为那一节的内容丢了，反而开始自查整个任务。
func TestGoalEnforcePromptOmitsEmptySections(t *testing.T) {
	p := goalEnforcePromptFor(agentgoal.NewGoal("g1", "s1", "d", 0), &agentgoal.Report{Outcome: agentgoal.Incomplete})
	for _, unwanted := range []string{"未满足项", "还没完成的是", "额度用完"} {
		if strings.Contains(p, unwanted) {
			t.Fatalf("prompt must omit the %q section when it has nothing to say; got:\n%s", unwanted, p)
		}
	}
}

// TestGoalEnforcePromptStopsCountingForTerminalGoals：目标已终结（blocked/completed）
// 时不再报"还剩几轮"——那时不会再续跑，报数字只会误导排期。
func TestGoalEnforcePromptStopsCountingForTerminalGoals(t *testing.T) {
	g := agentgoal.NewGoal("g1", "s1", "d", 0)
	g.Status = agentgoal.GoalBlocked
	p := goalEnforcePromptFor(g, &agentgoal.Report{Outcome: agentgoal.Incomplete})
	if strings.Contains(p, "还剩") {
		t.Fatalf("a terminal goal must not advertise remaining rounds; got:\n%s", p)
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

// TestBudgetAxisOfSeparatesWallsFromHardFailures 守住"撞墙 ≠ 硬失败"的识别。
//
// 识别错了的代价不对称：把硬失败认成撞墙，续跑只会在同一堵墙上重复烧钱；
// 把撞墙认成硬失败，整条长线任务就此终结 —— 而后者正是这次要修的那个 bug。
func TestBudgetAxisOfSeparatesWallsFromHardFailures(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{"budget_exceeded:tokens", "tokens"},
		{"budget_exceeded:steps", "steps"},
		{"budget_exceeded:cost", "cost"},
		{"  budget_exceeded:wall  ", "wall"},
		{"budget_exceeded:line_tokens", "line_tokens"},
		// 尾部夹带说明：取轴名，不因多几个字就退化成硬失败。
		{"budget_exceeded:tokens (segment 3)", "tokens"},
		{"budget_exceeded:steps。请提额", "steps"},
		// 不是预算撞墙：一律返回空串（照旧按硬失败处理）。
		{"", ""},
		{"boom", ""},
		{"budget_exceeded", ""},
		{"budget_exceeded:", ""},
		{"error: budget_exceeded:tokens", ""}, // 必须在前缀位置，不做子串匹配
	}
	for _, c := range cases {
		if got := budgetAxisOf(c.in); got != c.want {
			t.Fatalf("budgetAxisOf(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// TestPrevTurnItemsKeepTheJudgementHonest 是这次修复的主线：
//
// 续跑轮通常不再重写清单（清单已经在往来历史里）。如果每轮只从**当轮**事件读，
// 第二轮就没有清单 ⇒ 判定层看不见未完成项 ⇒ "没有错误 + 有正文"又等于完成 ⇒
// 长任务在第一轮的续跑之后就静默收工。这里断言上一轮的清单能被沿用。
func TestPrevTurnItemsKeepTheJudgementHonest(t *testing.T) {
	prev, ok := parseTodoItems(`[{"content":"改配置","status":"completed"},{"content":"跑测试","status":"pending"}]`)
	if !ok {
		t.Fatalf("checklist must parse")
	}
	pending := agentgoal.PendingOf(prev)
	if len(pending) != 1 || pending[0] != "跑测试" {
		t.Fatalf("only the open item may count, got %v", pending)
	}
	// 只看清单仍不足以判定：还必须带上"这一轮有正文、没有错误、没有改动"。
	rep := agentgoal.Evaluate(nil, agentgoal.Signals{HasAnswer: true, Pending: pending})
	if rep.Outcome != agentgoal.Incomplete {
		t.Fatalf("the carried-over checklist must keep the turn incomplete, got %s", rep.Outcome)
	}
	// 清单全部完成之后，同样的信号才是 Completed（完成由其余证据支撑）。
	rep = agentgoal.Evaluate(nil, agentgoal.Signals{HasAnswer: true, Pending: nil})
	if rep.Outcome != agentgoal.Completed {
		t.Fatalf("a finished checklist must not block completion, got %s", rep.Outcome)
	}
}
