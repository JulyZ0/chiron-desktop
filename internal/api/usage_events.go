package api

import (
	"context"
	"log/slog"

	"github.com/athenavi/chiron/internal/db"
)

// UsageEvent 是一次 LLM 调用的用量明细。
//
// 与 billing_records 的区别：那边一行一回合、靠 ON CONFLICT (turn_id) 幂等，是
// 扣费与对账的依据；这里是**只读的观测数据**，一回合可能多行（主模型 / 子 Agent /
// 摘要），供「钱花在哪个模型上」的统计使用。
//
// Model 取引擎事件里的实际模型（`evt.Model`），不是请求里的 llmConfig.model ——
// 后者只是意图，子 Agent 与 fallback 会让两者不一致，用它统计等于在统计用户想用什么。
type UsageEvent struct {
	TenantID     int16
	UserID       string
	SessionID    string
	TurnID       string
	Model        string
	Provider     string
	InputTokens  int
	OutputTokens int
	CachedTokens int
	CostCents    *int
	// UsageMissing 标记「调用真实发生，但引擎没有回报 token 明细」（auto 链路的
	// 纯工具回合 / 老引擎 / 中断的流）。
	//
	// **它不是数据库列**：`usage_events` 没有这一列，也不改迁移。它只在进程内
	// 传递（日志 / 用例断言）；持久化口径由「三列 token 全 0」派生，与计费接口的
	// `zero_token_events` 完全一致 —— 两者不能各说一套。
	UsageMissing bool
}

// RecordUsageEvent 写一条用量明细。
//
// **绝不返回错误给调用方**：明细写入失败不能让已完成的扣费回滚，也不能让一次成功
// 的对话因统计失败而报错。失败只告警。
func RecordUsageEvent(ctx context.Context, ev UsageEvent) {
	// 全空行不写：连模型名都没有、token 也全是 0 的那一行没有任何信息量。
	// `CachedTokens` 必须参与判断 —— 只有缓存命中的调用（输入全命中、输出截断为 0）
	// 是真事实，此前会被这条守卫当空气丢掉。
	if ev.Model == "" && ev.InputTokens == 0 && ev.OutputTokens == 0 && ev.CachedTokens == 0 {
		return
	}
	usageEventWrite(ctx, ev)
}

// usageEventWrite 是**实际落库**的那一步，抽成变量只为一件事：让单测能用一个
// 假引擎把整条链路（引擎 SSE → 事件 → 归因 → 落库行）跑通并断言写入的行内容。
//
// 为什么值得留这个缝：`usage_events` 的写入以前只有 `usd`/`tokens` 全零的形态被
// 观察到（"有行、tokens 全 0"），而这条链路跨三段序列化 + 一层归因，靠读代码
// 看不出是哪一段丢的。能断言"写进去的行"，才谈得上回归保护。
// 生产路径永远是 insertUsageEvent，替换只发生在测试里（`_test.go` 中 no-op 时还原）。
var usageEventWrite = insertUsageEvent

func insertUsageEvent(ctx context.Context, ev UsageEvent) {
	if db.GlobalDBManager == nil {
		return
	}
	_, err := db.GlobalDBManager.Exec(ctx,
		`INSERT INTO usage_events
		   (tenant_id, user_id, session_id, turn_id, model, provider,
		    input_tokens, output_tokens, cached_tokens, cost_cents)
		 VALUES ($1, $2, $3, $4, NULLIF($5, ''), NULLIF($6, ''), $7, $8, $9, $10)`,
		ev.TenantID, ev.UserID, ev.SessionID, ev.TurnID, ev.Model, ev.Provider,
		ev.InputTokens, ev.OutputTokens, ev.CachedTokens, ev.CostCents)
	if err != nil {
		slog.Warn("usage_events: record failed",
			"user", ev.UserID, "session", ev.SessionID, "model", ev.Model, "error", err)
	}
}
