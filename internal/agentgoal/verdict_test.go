package agentgoal

import (
	"testing"

	"github.com/athenavi/chiron/internal/evidence"
)

func collectorWith(calls [][2]string, results [][3]string) *evidence.Collector {
	c := evidence.NewCollector()
	for i, call := range calls {
		id := call[0]
		res := results[i]
		c.ObserveToolCall(id, call[1], res[1])
		c.ObserveToolResult(id, "", res[2])
	}
	return c
}

// 说明：calls 里第二项是工具名，results 里第二项是参数、第三项是结果 JSON。
// 为可读性，下面用显式的三元素构造 calls/results。

func TestPureQAIsCompleted(t *testing.T) {
	// 没改任何东西、有回答 ⇒ 完成。
	rep := Evaluate(nil, Signals{HasAnswer: true})
	if rep.Outcome != Completed {
		t.Fatalf("want completed, got %s (%v)", rep.Outcome, rep.Reasons)
	}
	if !rep.Complete() {
		t.Fatalf("Complete() must be true for completed")
	}
}

func TestErrorIsFailed(t *testing.T) {
	rep := Evaluate(nil, Signals{HasAnswer: true, HasError: true, ErrorMessage: "boom"})
	if rep.Outcome != Failed {
		t.Fatalf("want failed, got %s", rep.Outcome)
	}
	if rep.Complete() {
		t.Fatalf("failed must not be Complete()")
	}
}

func TestMutationWithoutVerificationIsIncomplete(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Incomplete {
		t.Fatalf("want incomplete (unverified change), got %s", rep.Outcome)
	}
	if rep.Complete() {
		t.Fatalf("incomplete must not be Complete()")
	}
	// 应当点名是 verified-changes 欠着。
	found := false
	for _, f := range rep.Findings {
		if f.Obligation.ID == "verified-changes" && f.Verdict == Owed {
			found = true
		}
	}
	if !found {
		t.Fatalf("expected verified-changes owed; findings=%+v", rep.Findings)
	}
}

func TestMutationThenVerificationIsCompleted(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	c.ObserveToolCall("2", "shell_exec", `{"command":"go test ./..."}`)
	c.ObserveToolResult("2", "", `{"ok":true}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Completed {
		t.Fatalf("want completed, got %s (%v)", rep.Outcome, rep.Reasons)
	}
}

func TestNoAnswerIsOwed(t *testing.T) {
	rep := Evaluate(nil, Signals{HasAnswer: false})
	if rep.Outcome != Incomplete {
		t.Fatalf("want incomplete (no answer), got %s", rep.Outcome)
	}
}

func TestCancelledDoesNotOweAnswer(t *testing.T) {
	// 取消的回合不该因为"没有回答"被算作欠 —— 更不该被算作"完成"。
	//
	// 曾经这里断言 Completed：取消不派生 answered 义务，而 no-error 自动满足，
	// 于是 summarize 返回 Completed，Advance 把 Goal 置为 GoalCompleted 并停止续跑。
	// 那意味着"用户点停止"与"任务真的做完了"在状态机里不可区分。现在取消走 Blocked。
	rep := Evaluate(nil, Signals{Cancelled: true})
	if rep.Outcome != Blocked {
		t.Fatalf("cancelled turn must be blocked, not %s", rep.Outcome)
	}
	if rep.Complete() {
		t.Fatalf("a cancelled turn must never count as complete")
	}
}

func TestTruncatedTurnIsIncompleteSoTheChainContinues(t *testing.T) {
	// 单轮 deadline 截断：现场已 checkpoint，义务未了结。
	//
	// 判定必须是 Incomplete（而非 Completed / Blocked）——这是长线任务能续跑的关键：
	// Completed 会让 Advance 停止续跑并谎称"做完了"；Blocked 会让 Goal 变 Abandoned。
	rep := Evaluate(nil, Signals{Truncated: true})
	if rep.Outcome != Incomplete {
		t.Fatalf("truncated turn must be incomplete so the chain continues, got %s", rep.Outcome)
	}
	if rep.Complete() {
		t.Fatalf("a truncated turn must never count as complete")
	}
	if len(rep.Reasons) == 0 {
		t.Fatalf("a truncated report must say why it is incomplete")
	}
}

func TestReadOnlyTurnIsCompleted(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "read_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"content":"x"}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Completed {
		t.Fatalf("read-only turn must not require verification; got %s", rep.Outcome)
	}
}

func TestEveryNaturalTurnDerivesRequiredObligations(t *testing.T) {
	// 曾经这里断言"取消 + 无义务 ⇒ None"。取消现在在 Evaluate 入口返回 Blocked
	// （否则会被判成 Completed），而 derive 恒派生 no-error 与 answered 两条必需义务，
	// 所以 None 从评估路径不可达。这里改为守住真正的不变式：
	// 任何自然结束（非取消/非截断）的回合都必须至少判到这两条义务。
	rep := Evaluate(nil, Signals{HasAnswer: true})
	if rep.Outcome == None {
		t.Fatalf("a natural turn must always carry obligations")
	}
	if len(rep.Findings) < 2 {
		t.Fatalf("want at least no-error and answered findings, got %d", len(rep.Findings))
	}
	for _, f := range rep.Findings {
		if !f.Obligation.Required {
			continue
		}
		if f.Obligation.ID != "no-error" && f.Obligation.ID != "answered" {
			t.Fatalf("unexpected required obligation %q", f.Obligation.ID)
		}
	}
}

func TestFailedVerificationStillOwed(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "write_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	c.ObserveToolCall("2", "shell_exec", `{"command":"pytest"}`)
	c.ObserveToolResult("2", "", `{"error":"1 failed"}`)

	rep := Evaluate(c, Signals{HasAnswer: true})
	if rep.Outcome != Incomplete {
		t.Fatalf("a failing verification must keep the change unverified; got %s", rep.Outcome)
	}
}

func TestModelClaimNeverSatisfies(t *testing.T) {
	// 直接构造一条 model-claim 义务，确认它永远是 owed（claim 自身不满足义务）。
	f := judge(Obligation{ID: "c1", Required: true, Class: classModelClaim}, nil, Signals{})
	if f.Verdict != Owed {
		t.Fatalf("model claim must be owed until host evidence exists; got %s", f.Verdict)
	}
}

func TestBlockedReport(t *testing.T) {
	rep := BlockedReport("deadline exceeded")
	if rep.Outcome != Blocked {
		t.Fatalf("want blocked, got %s", rep.Outcome)
	}
}

func TestSameInputSameResult(t *testing.T) {
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	a := Evaluate(c, Signals{HasAnswer: true})
	b := Evaluate(c, Signals{HasAnswer: true})
	if a.Outcome != b.Outcome || len(a.Findings) != len(b.Findings) {
		t.Fatalf("Evaluate must be deterministic")
	}
}

// TestBudgetExceededKeepsTheChainAlive 守住"段级额度用完不等于失败"。
//
// 曾经 `budget_exceeded:<轴>` 与引擎硬错误同形（都是 error 事件）⇒ 判 Failed ⇒
// `Advance` 置 GoalBlocked 并停止续跑。于是一次典型的代码任务在 tokens 用尽时
// **整条长线任务终结**，用户看到的是"做一半就停"，而且没有任何机制会把它接上。
// 正确语义：这一段停在这里，下一段接着跑。
func TestBudgetExceededKeepsTheChainAlive(t *testing.T) {
	rep := Evaluate(nil, Signals{
		HasError:     true,
		ErrorMessage: "budget_exceeded:tokens",
		BudgetAxis:   "tokens",
		HasAnswer:    false,
	})
	if rep.Outcome != Incomplete {
		t.Fatalf("a budget wall must be incomplete (continue), not %s", rep.Outcome)
	}
	if rep.BudgetAxis != "tokens" {
		t.Fatalf("the hit axis must survive into the report, got %q", rep.BudgetAxis)
	}
	// 撞墙时不能同时派生 no-error：那会让同一件事被判两次，且其中一次是失败。
	for _, f := range rep.Findings {
		if f.Obligation.ID == "no-error" {
			t.Fatalf("a budget wall must not derive the no-error obligation: %+v", f)
		}
	}
}

// TestBudgetExceededStillOwedAnswer：撞墙的那一轮通常没有正文，这条仍然欠着 ——
// 于是 Incomplete 有两条独立理由，不依赖"只有 budget 一条"的偶然成立。
func TestBudgetExceededStillOwedAnswer(t *testing.T) {
	rep := Evaluate(nil, Signals{BudgetAxis: "steps"})
	owed := map[string]bool{}
	for _, f := range rep.Findings {
		if f.Verdict == Owed {
			owed[f.Obligation.ID] = true
		}
	}
	if !owed["budget-continuation"] || !owed["answered"] {
		t.Fatalf("want both budget-continuation and answered owed, got %+v", rep.Findings)
	}
}

// TestOpenChecklistItemKeepsTheTurnIncomplete 是这次修复的核心那条：
// "没有错误 + 有正文"不再等于完成。
func TestOpenChecklistItemKeepsTheTurnIncomplete(t *testing.T) {
	rep := Evaluate(nil, Signals{HasAnswer: true, Pending: []string{"跑测试", "写文档"}})
	if rep.Outcome != Incomplete {
		t.Fatalf("an open checklist must keep the turn incomplete, got %s", rep.Outcome)
	}
	if len(rep.Pending) != 2 {
		t.Fatalf("the pending items must survive into the report, got %v", rep.Pending)
	}
	var found bool
	for _, f := range rep.Findings {
		if f.Obligation.ID == "goal-progress" {
			found = true
			if f.Verdict != Owed {
				t.Fatalf("goal-progress must be owed while items remain, got %s", f.Verdict)
			}
		}
	}
	if !found {
		t.Fatalf("expected a goal-progress obligation; findings=%+v", rep.Findings)
	}
}

// TestFinishedChecklistDoesNotByItselfComplete：清单全 green 只是**撤掉**额外义务，
// 不能把回合抬成完成 —— 判定仍由 no-error / answered / verified-changes 支撑。
func TestFinishedChecklistDoesNotByItselfComplete(t *testing.T) {
	// 有正文 + 无未完成项 ⇒ 完成（这是正常路径，清单不该变成噪声）。
	if rep := Evaluate(nil, Signals{HasAnswer: true, Pending: nil}); rep.Outcome != Completed {
		t.Fatalf("an empty pending set must not block completion, got %s", rep.Outcome)
	}
	// 没有正文时，清单全 green 也救不回来。
	if rep := Evaluate(nil, Signals{HasAnswer: false, Pending: nil}); rep.Outcome != Incomplete {
		t.Fatalf("a finished checklist must not substitute for the answer, got %s", rep.Outcome)
	}
	// 有未验证改动时同理：清单全 green 不能替代验证。
	c := evidence.NewCollector()
	c.ObserveToolCall("1", "edit_file", `{"path":"a.go"}`)
	c.ObserveToolResult("1", "", `{"ok":true}`)
	if rep := Evaluate(c, Signals{HasAnswer: true}); rep.Outcome != Incomplete {
		t.Fatalf("a finished checklist must not substitute for verification, got %s", rep.Outcome)
	}
}

// TestPendingSurvivesTruncation：被 deadline 掐断时未完成项也要带出去 ——
// 否则续跑指令只能说"超时了"，说不出"还剩哪几件事"。
func TestPendingSurvivesTruncation(t *testing.T) {
	rep := Evaluate(nil, Signals{Truncated: true, Pending: []string{"跑测试"}})
	if rep.Outcome != Incomplete {
		t.Fatalf("want incomplete, got %s", rep.Outcome)
	}
	if len(rep.Pending) != 1 || rep.Pending[0] != "跑测试" {
		t.Fatalf("pending items must survive truncation, got %v", rep.Pending)
	}
}
