package api

// ── 统一链路（`/v1/chat/submit`、`/v1/quick-execute`）的用量记账 ──
//
// **为什么需要它**
//
// 仓里有两条真正在跑的对话链路：
//
//  1. SSE `/submit`（`/v1/agent/submit`）—— 网关自己消费引擎的事件流，回合结算段
//     把用量写进 `usage_events`（见 `submit_handler.go`）；
//  2. 统一任务模式 `/v1/chat/submit`（`/v1/quick-execute` 是它的别名）—— 走
//     `routes_proxy.go` 的**通用代理**：读 body、注入会话运行时、转发、原样回包。
//     这条链路**一个字的用量都没记**。
//
// 后果与"接管不记账"同源：用户在统一模式下聊一整天，会话在长、界面正常，
// 而计费页里这些回合**根本不存在**（不是 0，是没有行）。两条链路各写一份统计
// 才是正确做法 —— 前提是**同源同口径**（同一张表、同一套单价、同样的零用量处理）。
//
// **口径**
//
// 一行一次调用（与 `submit_handler` 一致），`cost_cents` 由 `model_prices` 换算，
// 取不到单价写 NULL（"不知道"）而不是 0（"免费"）。归属（tenant/user）取自鉴权
// claims，**不取引擎回包里的字段** —— 那是请求方可以影响的。
//
// 引擎没有回报用量时（老版本 / 路由到不报 usage 的能力）也落一行：调用次数是
// 真实发生的，token 为 0 只表示"这次没拿到明细"，两种情况不能互相冒充。

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/id"
)

// unifiedUsageStoreTimeout 是回包写完之后单独计时的落库窗口。
//
// 与 `submit_handler` 的 `storeWriteTimeout` 同理由：请求 ctx 在响应写完后就会被
// 取消，用它写库等于让记账随机丢失。
const unifiedUsageStoreTimeout = 5 * time.Second

// unifiedUsage 是统一链路回包里携带的用量（引擎 `submit_task` 产出）。
type unifiedUsage struct {
	Model        string `json:"model"`
	Provider     string `json:"provider"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	CachedTokens int    `json:"cached_tokens"`
}

// unifiedUsageFromResponse 从引擎的统一链路回包里取用量。
//
// 形状容忍：`usage` 可能在顶层，也可能挂在 `metadata.usage` 下（引擎按工作台
// 不同把统计放在 metadata 里），两种都要认 —— 只认一种就会出现"某个模式有账、
// 另一个模式没有"，而这正是这条链路最初的问题形态。
func unifiedUsageFromResponse(resp map[string]any) (unifiedUsage, bool) {
	if resp == nil {
		return unifiedUsage{}, false
	}
	raw, ok := resp["usage"].(map[string]any)
	if !ok {
		meta, _ := resp["metadata"].(map[string]any)
		raw, ok = meta["usage"].(map[string]any)
	}
	if !ok {
		// 没有 usage 字段：不代表没发生调用（老引擎）。返回 false 让调用方决定
		// 要不要写"零 token 行"—— 这里选择**写**，理由见文件头。
		return unifiedUsage{}, false
	}
	return unifiedUsage{
		Model:        strings.TrimSpace(stringOf(raw["model"])),
		Provider:     strings.TrimSpace(stringOf(raw["provider"])),
		InputTokens:  intOf(raw["input_tokens"]),
		OutputTokens: intOf(raw["output_tokens"]),
		CachedTokens: intOf(raw["cached_tokens"]),
	}, true
}

// recordUnifiedUsage 把统一链路的一次调用写进 `usage_events`。
//
// sessionID 取请求体里的 `session_id`（统一链路的会话是引擎侧的 unified_sessions，
// 与网关的 sessions 不是一张表，所以这里**不做**跨表反查：查不到会让整笔账消失）。
// turnID 用回包的 task_id / trace_id —— 同一次任务重复回包时据此幂等去重由上报方
// 负责，这里只保证"有值可追溯"。
func recordUnifiedUsage(
	r *http.Request,
	claims *auth.Claims,
	sessionID, turnID, model string,
	u unifiedUsage,
	hasUsage bool,
) {
	if claims == nil {
		return
	}
	if !hasUsage && model == "" {
		// 连模型都没有、引擎也没报用量：这条链路这次确实没有任何可记的事实。
		// 不写"全 NULL 行" —— 那只会污染统计基数。SSE 链路之所以写零行，
		// 是因为那里有确定的 turn_id 与真实回合，情况不同。
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), unifiedUsageStoreTimeout)
	defer cancel()

	model = strings.TrimSpace(model)
	if model == "" {
		model = u.Model
	}
	provider := u.Provider

	var costCents *int
	if u.InputTokens > 0 || u.OutputTokens > 0 {
		costCents = usageCostCents(ctx, model, u.InputTokens, u.OutputTokens, u.CachedTokens)
	}

	RecordUsageEvent(ctx, UsageEvent{
		TenantID:     claims.TenantID,
		UserID:       claims.UserID,
		SessionID:    sessionID,
		TurnID:       turnID,
		Model:        model,
		Provider:     provider,
		InputTokens:  u.InputTokens,
		OutputTokens: u.OutputTokens,
		CachedTokens: u.CachedTokens,
		CostCents:    costCents,
	})
}

// recordUnifiedUsageFromResponse 是 `proxyOpt.afterResponse` 的适配器：
// 从回包里取用量与归属，然后写库。任何解析失败只告警，绝不改回包。
func recordUnifiedUsageFromResponse(
	r *http.Request,
	claims *auth.Claims,
	requestBody map[string]any,
	resp map[string]any,
) {
	if resp == nil {
		return
	}
	// 引擎侧 `success:false` 的回包同样可能带用量（半途失败的调用也是真花钱）。
	usage, hasUsage := unifiedUsageFromResponse(resp)

	sessionID := stringOf(requestBody["session_id"])
	model := ""
	if cfg, ok := requestBody["llm_config"].(map[string]any); ok {
		model = stringOf(cfg["model"])
	}
	turnID := stringOf(resp["task_id"])
	if turnID == "" {
		turnID = stringOf(resp["trace_id"])
	}
	if turnID == "" {
		turnID = "unified_" + id.NextID()
	}

	if !hasUsage {
		slog.Debug("unified usage: engine reported no usage; recording call with zero tokens",
			"session", sessionID, "turn", turnID)
	}
	recordUnifiedUsage(r, claims, sessionID, turnID, model, usage, hasUsage)
}
