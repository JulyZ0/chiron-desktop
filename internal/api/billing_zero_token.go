package api

// ── 零 token 记账行的来源分类（P1-7）──
//
// **为什么需要分类**
//
// `usage_events` 里有相当一部分行 `input + output + cached = 0`。它们不是"没发生
// 调用"（调用确实发生了，否则不会有这一行），而是**这一轮没拿到用量**。此前页面
// 只能给出一个总数（`zero_token_events`），用户无法判断这是"我按了停止""引擎报错"
// 还是"上游没回 usage 帧" —— 三种情况的处置完全不同：
//
//   - `failed`：上游/引擎失败。值得去看错误日志、考虑重试；
//   - `aborted`：用户自己停的。属于正常操作，不该当成故障；
//   - `upstream_no_usage`：轮次正常结束（completed），但 usage 帧没到。属于
//     **观测缺口**（上游不报用量），既不是故障也不是用户操作；
//   - `unknown`：缺少判定依据（没有对应的 turn/run 行，或仍处于 running）。宁可
//     归入未知，也不按模型名/时间"猜"一个来源。
//
// **判定依据**
//
// 只认数据库里的终态，不猜：
//
//   1. `usage_events.turn_id` 关联 `turns.id` 取 `turns.status`；
//   2. 同一 `turn_id` 关联 `agent_runs.turn_id` 取最近一次 run 的 `status`
//      （接管 / 子 Agent 的 run 不一定写 turns 行）；
//   3. 两组状态**先归一**再判定，优先级：用户中断 > 失败 > 上游未回 usage >
//      无法判定。
//
// 优先级里"中断优先于失败"与网关自己的收尾口径一致（`submit_handler`：
// `ctx.Err() != nil` 先判 cancelled，再判 streamErr → failed）。这样同一次中断在
// 两处的归类不会互相矛盾。
//
// **为什么不写成 SQL 的 CASE**
//
// 分类规则要能被单测钉住（三种来源 + 未知各一条），而 `DBManager` 是具体类型、
// 没有可注入的接口。于是把 SQL 缩到"按 (turn.status, run.status) 分组计数"
// （基数极小），分类逻辑放在 Go 侧纯函数里 —— 一个 CASE 写错只会静默算偏，
// 而纯函数能被表驱动用例直接覆盖。

import (
	"context"
	"strings"

	"github.com/athenavi/chiron/internal/db"
)

// 零 token 行的四个来源标签（响应里的键名，同时也是前端的 i18n 键后缀）。
const (
	zeroTokenFailed          = "failed"
	zeroTokenAborted         = "aborted"
	zeroTokenUpstreamNoUsage = "upstream_no_usage"
	zeroTokenUnknown         = "unknown"
)

// 归一后的终态档位（只用于分类，不外泄）。
const (
	terminalAborted   = "aborted"
	terminalFailed    = "failed"
	terminalCompleted = "completed"
	terminalOther     = "other"
)

// zeroTokenStatusRow 是一组"同状态组合"的零 token 行计数（SQL 聚合的中间形态）。
//
// 按状态组合而不是按行取回：零 token 行可能有上万条，但 (turn.status, run.status)
// 的组合数是个位数量级。分类逻辑因此仍能在 Go 里被单测覆盖。
type zeroTokenStatusRow struct {
	turnStatus string
	runStatus  string
	events     int
}

// normalizeTerminalStatus 把库里的状态串归一到四档。
//
// 覆盖各家写法（cancelled/canceled、timeout/timed_out）是因为这些状态由
// 网关、引擎、子 Agent 三条链路分别写入，拼写并不统一；漏认一个就会把一次
// 用户中断算成"未知"，让分类失去意义。**认不出的状态一律归 other（→ unknown）**，
// 不按"包含某个子串"去猜。
func normalizeTerminalStatus(raw string) string {
	switch strings.ToLower(strings.TrimSpace(raw)) {
	case "cancelled", "canceled", "aborted", "stopped", "interrupted", "abandoned", "lost":
		return terminalAborted
	case "failed", "failure", "error", "errored", "timeout", "timed_out", "deadline_exceeded":
		return terminalFailed
	case "completed", "complete", "succeeded", "success", "done", "finished":
		return terminalCompleted
	default:
		// 空串（没有对应 turn/run 行）、running、checkpointed、resuming、
		// awaiting_input 等都属于"还判不了"。
		return terminalOther
	}
}

// classifyZeroToken 按 turn / run 的终态判定零 token 行的来源。
//
// 优先级：中断 > 失败 > 正常结束（上游未回 usage）> 未知。理由见文件头：
// 中断是用户意图的直接结果，失败常常是中断的次生现象（超时/断连），
// 所以中断必须先判。
func classifyZeroToken(turnStatus, runStatus string) string {
	turn := normalizeTerminalStatus(turnStatus)
	run := normalizeTerminalStatus(runStatus)

	if turn == terminalAborted || run == terminalAborted {
		return zeroTokenAborted
	}
	if turn == terminalFailed || run == terminalFailed {
		return zeroTokenFailed
	}
	if turn == terminalCompleted || run == terminalCompleted {
		// 轮次正常结束却没有 usage：上游（provider）回了内容但没回用量帧。
		return zeroTokenUpstreamNoUsage
	}
	return zeroTokenUnknown
}

// buildZeroTokenBreakdown 把按状态组合聚合的行折成响应里的 `zero_token_breakdown`。
//
// 四个计数**互斥且合计为 total**：前端据此渲染三类来源 + 未知，并说明"这些行
// 没有 token 明细不代表没发生调用"。
func buildZeroTokenBreakdown(rows []zeroTokenStatusRow) map[string]any {
	counts := map[string]int{
		zeroTokenFailed:          0,
		zeroTokenAborted:         0,
		zeroTokenUpstreamNoUsage: 0,
		zeroTokenUnknown:         0,
	}
	total := 0
	for _, row := range rows {
		if row.events <= 0 {
			continue
		}
		counts[classifyZeroToken(row.turnStatus, row.runStatus)] += row.events
		total += row.events
	}
	return map[string]any{
		"total":             total,
		"failed":            counts[zeroTokenFailed],
		"aborted":           counts[zeroTokenAborted],
		"upstream_no_usage": counts[zeroTokenUpstreamNoUsage],
		"unknown":           counts[zeroTokenUnknown],
		// 口径标识：前端把它当"这段数字是怎么算出来的"的说明键，
		// 与 `count_basis` 同一套用法（改口径必须改这个值）。
		"basis": "zero_token_rows_by_turn_or_run_terminal_status",
	}
}

// usageZeroTokenBreakdown 查询区间内零 token 行并按 turn/run 终态分组。
//
// 两次 LEFT JOIN 而不是一次 JOIN：`turns` 与 `agent_runs` 都可能没有对应行
// （统一链路 / 引擎上报的 turn_id 不在 turns 里），缺哪一边都不该让整行消失。
// `agent_runs` 用 LATERAL + LIMIT 1：同一 turn 可能有多次 run（resume），
// 直接 JOIN 会把一行 usage 放大成多行，分类计数随 run 次数翻倍。
func usageZeroTokenBreakdown(ctx context.Context, userID string, days int) (map[string]any, error) {
	rows, err := db.GlobalDBManager.FetchAll(ctx, `
		SELECT COALESCE(t.status, '')              AS turn_status,
		       COALESCE(r.status, '')              AS run_status,
		       count(*)                            AS event_count
		  FROM usage_events ue
		  LEFT JOIN turns t ON t.id = ue.turn_id
		  LEFT JOIN LATERAL (
		      SELECT status
		        FROM agent_runs
		       WHERE turn_id = ue.turn_id
		       ORDER BY updated_at DESC NULLS LAST
		       LIMIT 1
		  ) r ON true
		 WHERE ue.user_id = $1
		   AND ue.input_tokens + ue.output_tokens + ue.cached_tokens = 0
		   AND `+usageWindowOn("ue")+`
		 GROUP BY 1, 2`, userID, days)
	if err != nil {
		return nil, err
	}

	statusRows := make([]zeroTokenStatusRow, 0, len(rows))
	for _, row := range rows {
		statusRows = append(statusRows, zeroTokenStatusRow{
			turnStatus: stringOf(row["turn_status"]),
			runStatus:  stringOf(row["run_status"]),
			events:     intOf(row["event_count"]),
		})
	}
	return buildZeroTokenBreakdown(statusRows), nil
}
