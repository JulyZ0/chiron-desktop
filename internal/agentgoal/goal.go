// Goal FSM（P6）—— 目标级自动续跑。
//
// 对照实现：Reasonix `internal/session/control/goal.go` 的 `goalMachine` +
// `turn_orchestrator.go` 的 `advanceGoalAfterTurn`（run_loop 注释："In Goal mode the FSM
// auto-continues"）。Chiron 在 P1c 已实现**单次提交内**的有限续跑
// （internal/api/submit_handler.go 的 maxGoalEnforceRounds）；本文件补的是**跨轮**的那一半：
// 目标的状态被**记下来**（跨用户请求），于是"上一次没做完"不会因为用户没再说话就消失。
//
// 设计纪律（与 P1 判定层一致）：
//
//  1. **Advance 是纯函数** —— 不读时钟、不读网络；时间戳由调用方写入（便于单测）。
//  2. **有界** —— 回合数（RoundsUsed vs MaxRounds）是硬闸门，防止"永不收敛的目标"烧 token。
//     到顶即 blocked（**不是** completed）—— 如实说"没做完"，绝不谎报。
//  3. **失败不伪装成继续** —— Failed 直接 blocked（要人介入），而不是无限续跑到预算耗尽。
package agentgoal

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// GoalStatus 是目标的生命周期状态。
type GoalStatus string

const (
	// GoalActive：目标进行中（可以继续推进）。
	GoalActive GoalStatus = "active"
	// GoalCompleted：目标已达成（host 证据支持）。
	GoalCompleted GoalStatus = "completed"
	// GoalBlocked：目标卡住 / 到顶 —— **没有达成**，需要人或新的输入。
	GoalBlocked GoalStatus = "blocked"
	// GoalAbandoned：回合非自然结束（取消 / 超时），目标结论不适用。
	GoalAbandoned GoalStatus = "abandoned"
)

// Goal 是一个会话级的目标。
type Goal struct {
	ID          string     `json:"id"`
	SessionID   string     `json:"session_id"`
	Description string     `json:"description"`
	Status      GoalStatus `json:"status"`
	// RoundsUsed 是**当前小目标**已消耗的续跑回合数（**由 host 计数**，模型不能改）。
	// 换到下一个清单项时归零 —— 20 轮的墙只拦"这一件事卡住"，不拦整条任务。
	RoundsUsed int `json:"rounds_used"`
	// MaxRounds 是**单个小目标**的续跑回合上限（0 表示用 DefaultGoalRounds）。
	MaxRounds int `json:"max_rounds"`
	// TotalRounds 是**整条任务**累计消耗的续跑回合数（跨小目标累加，只增不减）。
	TotalRounds int `json:"total_rounds"`
	// MaxTotalRounds 是整条任务的累计回合上限（0 表示用 DefaultMaxTotalRounds）。
	MaxTotalRounds int `json:"max_total_rounds"`
	// SubGoal 是当前小目标的指纹（见 SubGoalOf）。它变化即"推进到下一件事"，
	// RoundsUsed 随之归零。为空表示还没有可识别的小目标边界（整条任务算一个）。
	SubGoal   string `json:"sub_goal,omitempty"`
	UpdatedAt int64  `json:"updated_at"`
	// LastReason 是最近一次推进的原因（给用户/日志）。
	LastReason string `json:"last_reason,omitempty"`
}

// DefaultGoalRounds 是**单个小目标**的续跑轮数上限（不是整条任务的上限）。
//
// 层级必须分清：一条长任务由多个小目标串起来（清单里的每一条），每个小目标最多
// 跑 20 轮；小目标推进到下一个，计数就重置。20 轮原来被当作**整条任务**的总上限，
// 于是一次典型的代码任务（定位 → 改 → 跑测试 → 修 → 再跑）第 20 轮之后整条链就
// 判 blocked，用户看到的是"长任务做不长" —— 这是层级错误，不是数字大小问题。
//
// 为什么仍要有个上限：卡在同一个死结上的小目标会反复撞墙，无上限等于无人看管的
// token 燃烧。到顶即 blocked（**不是** completed）—— 如实说"这个小目标没做完"，
// 让人或下一次输入来换方向。整条任务的成本闸门见 DefaultMaxTotalRounds。
const DefaultGoalRounds = 20

// DefaultMaxTotalRounds 是**整条任务**的累计轮数上限（成本闸门）。
//
// 单个小目标放开到 20 轮之后，缺的就是这一层：chiron 的 token 预算只在 per-run 层
// （`AGENT_RUN_MAX_TOKENS`），续跑链上没有跨轮预算，所以"小目标数 × 20 轮"必须有
// 一个总闸门，否则模型可以无限列新小目标把成本推到底。取 200（等于 10 个满额
// 小目标），同时必须小于链级墙钟预算能容纳的轮数（见 config.AgentGoalTimeout）。
const DefaultMaxTotalRounds = 200

// EffectiveMaxRounds 返回**单个小目标**生效的回合上限。
func (g Goal) EffectiveMaxRounds() int {
	if g.MaxRounds > 0 {
		return g.MaxRounds
	}
	return DefaultGoalRounds
}

// EffectiveMaxTotalRounds 返回**整条任务**生效的累计回合上限。
func (g Goal) EffectiveMaxTotalRounds() int {
	if g.MaxTotalRounds > 0 {
		return g.MaxTotalRounds
	}
	return DefaultMaxTotalRounds
}

// SubGoalItem 是清单里的一条 —— 只保留判定"小目标边界"需要的两项。
type SubGoalItem struct {
	Content string
	Status  string
}

// SubGoalOf 由任务清单推出**当前小目标**的稳定指纹，空串表示还没有边界。
//
// 取"第一个进行中"的项；没有进行中的就取第一个未完成的（下一个待办）—— 后者让
// 模型刚写完清单、还没开始动第一项时就已经有边界，不至于把整份清单算作一个目标。
// 指纹用哈希而不是原文：清单条目可能很长且含换行，塞进 Redis 的 goal JSON 会膨胀，
// 而判等只需要稳定；同样的 content 永远得到同样的指纹。
//
// 空清单返回空串 —— 没有清单就没有小目标边界，此时整条任务算一个目标（保守：
// 20 轮不重置），而不是把 20 轮无限续下去。
func SubGoalOf(items []SubGoalItem) string {
	pick := ""
	for _, it := range items {
		if strings.EqualFold(strings.TrimSpace(it.Status), "in_progress") {
			pick = it.Content
			break
		}
	}
	if pick == "" {
		for _, it := range items {
			if !strings.EqualFold(strings.TrimSpace(it.Status), "completed") {
				pick = it.Content
				break
			}
		}
	}
	pick = strings.TrimSpace(pick)
	if pick == "" {
		return ""
	}
	sum := sha256.Sum256([]byte(pick))
	return hex.EncodeToString(sum[:6])
}

// BeginRound 在**每轮开始前**把目标绑定到当轮的小目标。
//
// 指纹变化意味着"上一件事已经推进到下一件"，于是单小目标的轮数清零 —— 这是
// "20 轮拦的是单个小目标、不是整条任务"的落点。TotalRounds 不清零：它是整条
// 任务的成本闸门，跨小目标累加。
func BeginRound(g Goal, subGoal string) Goal {
	if subGoal == "" || subGoal == g.SubGoal {
		return g
	}
	g.SubGoal = subGoal
	g.RoundsUsed = 0
	g.LastReason = "sub-goal advanced: per-sub-goal continuation rounds reset"
	return g
}

// NewGoal 建一个 active 目标。
func NewGoal(id, sessionID, description string, now int64) Goal {
	return Goal{
		ID:          id,
		SessionID:   sessionID,
		Description: description,
		Status:      GoalActive,
		UpdatedAt:   now,
	}
}

// Advance 依据**一次回合的判定结果**推进目标，返回更新后的目标与"是否应继续跑".
//
// 这是 goalMachine 的核心转移（纯函数）。调用方负责把返回的目标持久化。
func Advance(g Goal, report Report, now int64) (Goal, bool) {
	g.UpdatedAt = now
	switch report.Outcome {
	case Completed, None:
		g.Status = GoalCompleted
		g.LastReason = "host evidence satisfied every obligation"
		return g, false

	case Failed:
		// 硬失败：交给用户/运维，不自动续跑（继续跑只会在同一堵墙上再撞几次）。
		g.Status = GoalBlocked
		g.LastReason = "turn failed: " + firstReason(report)
		return g, false

	case Blocked:
		// 取消 / 超时：结论不适用，目标不推进也不判死。
		g.Status = GoalAbandoned
		g.LastReason = "turn ended without a conclusion (cancelled or truncated)"
		return g, false

	case Incomplete:
		g.RoundsUsed++
		g.TotalRounds++
		// 两层闸门，顺序有意义：先判**单个小目标**（这一件事卡住了 → 换方向），
		// 再判**整条任务**（成本到底了 → 停）。反过来的话，一个满额小目标会被
		// 误报成"整条任务超预算"，用户看不出到底哪一层拦的。
		if g.RoundsUsed >= g.EffectiveMaxRounds() {
			g.Status = GoalBlocked
			g.LastReason = "sub-goal continuation budget exhausted: " + firstReason(report)
			return g, false
		}
		if g.TotalRounds >= g.EffectiveMaxTotalRounds() {
			g.Status = GoalBlocked
			g.LastReason = "task continuation budget exhausted: " + firstReason(report)
			return g, false
		}
		g.Status = GoalActive
		g.LastReason = "continuing: " + firstReason(report)
		return g, true
	}

	// 未知 outcome：不猜 —— 保持原状且不续跑（fail-closed）。
	g.LastReason = "unknown turn outcome: " + string(report.Outcome)
	return g, false
}

// Terminal 判断目标是否已到终态（不再推进）。
func (g Goal) Terminal() bool {
	return g.Status == GoalCompleted || g.Status == GoalBlocked || g.Status == GoalAbandoned
}

func firstReason(report Report) string {
	if len(report.Reasons) > 0 {
		return report.Reasons[0]
	}
	return string(report.Outcome)
}
