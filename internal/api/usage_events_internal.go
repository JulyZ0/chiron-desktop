package api

// ── 用量上报（引擎 → 网关）──
//
// **为什么需要这条通道**
//
// `usage_events` 是计费页唯一的事实源（见 `billing_usage.go` 的口径说明），而它的
// 写入点只有一处：`submit_handler.go` 的回合结算段 —— 那一处读的是**网关自己消费
// 的 SSE 事件流**。于是凡是不经过网关的 run，用量就永远进不了这张表。
//
// 而「接管」（super-agent）恰恰就是这种 run：`python-engine/app/queue/takeover_job.py`
// 在每个会话到点时**由引擎自己**起一次 `AgentRuntime.run`，事件流不回到网关。
// 后果不是"少记一笔账"这么轻：用户开着接管巡检、每天都能看到会话有推进，计费页
// 却一直显示 0 —— 一个"看起来正常工作、实际上不记账"的子系统，比坏掉的那个更难
// 发现。
//
// **为什么是内网端点而不是让引擎直连 PG**
//
// 同 `project_checkpoints.go` 与 `checkpoint_hooks.py` 的既有分工：引擎侧只上报
// **事实**（这次 run 用了哪个模型、多少 token），归属（tenant/user）与金额（单价
// 只在网关的 `model_prices` 里）都由网关决定。让引擎自己拼 tenant_id 就等于把
// "这次用量算谁的"交给调用方，而请求体是可以伪造的。
//
// **幂等靠 turn_id**
//
// 接管 run 的 `turn_id` 由引擎生成且带 `takeover_` 前缀（正常回合是 UUID，不会
// 冲突）。重复上报（网络重试、一拍莫名跑了两遍）会先把同一 `(session_id, turn_id)`
// 的旧行删掉再插入 —— 宁可重写一次，也不要让一次巡检被计两遍钱。

import (
	"context"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ingestStoreTimeout 是落库写窗口。
//
// 与 `submit_handler` 的 `storeWriteTimeout` 同值同理由：请求自身的 ctx 会在响应
// 写完之后被取消，用它写库等于让最后几行随机丢失。这里从"开始写"那一刻单独计时。
const ingestStoreTimeout = 10 * time.Second

// ingestUsageEntriesMax 是单次上报的模型行数上限。
//
// 一次 run 跨的模型数是个位数量级（主模型 + fallback + 摘要 + 子 Agent）。给
// 32 已远超真实场景，而它挡住了"调用方一次塞十万行"这种写入放大。
const ingestUsageEntriesMax = 32

type ingestUsageEntry struct {
	Model        string `json:"model"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	CachedTokens int    `json:"cached_tokens"`
}

type ingestUsageBody struct {
	SessionID string             `json:"session_id"`
	TurnID    string             `json:"turn_id"`
	Provider  string             `json:"provider"`
	Entries   []ingestUsageEntry `json:"entries"`
}

// IngestUsageEvents 接收引擎上报的用量（POST /v1/internal/usage-events）。
//
// 它只写 `usage_events`（观测数据），**不扣费**：扣费域已随迁移整体删除，仓里
// 没有第二个金额落点，所以这里也不存在"少扣一笔"的分支。
func IngestUsageEvents(w http.ResponseWriter, r *http.Request) {
	var body ingestUsageBody
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, "invalid request")
		return
	}
	body.SessionID = strings.TrimSpace(body.SessionID)
	body.TurnID = strings.TrimSpace(body.TurnID)
	body.Provider = strings.TrimSpace(body.Provider)

	if body.SessionID == "" || body.TurnID == "" {
		BadRequest(w, "session_id and turn_id are required")
		return
	}
	if len(body.Entries) == 0 {
		// 零用量 run 是合法事实（"巡检了但无事可做"），但连一行都没有就没什么可写。
		// 返回成功而不是 400：这是"没有用量"，不是"请求错了"。
		OK(w, map[string]interface{}{"stored": 0})
		return
	}
	if len(body.Entries) > ingestUsageEntriesMax {
		BadRequest(w, "too many entries")
		return
	}
	if db.GlobalDBManager == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
		return
	}

	// 归属只信数据库里的会话行，不信请求体。查不到就等于"这个会话不存在"。
	row, err := db.GlobalDBManager.FetchOne(r.Context(),
		`SELECT tenant_id, COALESCE(user_id, '') AS user_id
		   FROM sessions
		  WHERE id = $1
		  LIMIT 1`, body.SessionID)
	if err != nil {
		slog.Error("usage ingest: session lookup failed", "error", err, "session", body.SessionID)
		InternalError(w, "lookup failed")
		return
	}
	if row == nil {
		OK(w, map[string]interface{}{"stored": 0, "reason": "unknown session"})
		return
	}
	tenantID := int16(intOf(row["tenant_id"]))
	userID := stringOf(row["user_id"])

	// 先清同一 turn 的旧行：重试与重复上报都不该让一次巡检计两遍。
	// 删除失败不算致命（INSERT 仍会写入），但要告警 —— 那意味着幂等失效。
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`DELETE FROM usage_events WHERE session_id = $1 AND turn_id = $2`,
		body.SessionID, body.TurnID); err != nil {
		slog.Warn("usage ingest: stale rows delete failed; ingest is not idempotent",
			"session", body.SessionID, "turn", body.TurnID, "error", err)
	}

	storeCtx, cancel := context.WithTimeout(context.Background(), ingestStoreTimeout)
	defer cancel()

	stored := 0
	for _, entry := range body.Entries {
		model := strings.TrimSpace(entry.Model)
		if entry.InputTokens <= 0 && entry.OutputTokens <= 0 && entry.CachedTokens <= 0 {
			continue
		}
		RecordUsageEvent(storeCtx, UsageEvent{
			TenantID:     tenantID,
			UserID:       userID,
			SessionID:    body.SessionID,
			TurnID:       body.TurnID,
			Model:        model,
			Provider:     body.Provider,
			InputTokens:  entry.InputTokens,
			OutputTokens: entry.OutputTokens,
			CachedTokens: entry.CachedTokens,
			// 金额按 `model_prices` 换算：取不到单价写 NULL（"不知道"），不写 0（"免费"）。
			CostCents: usageCostCents(storeCtx, model, entry.InputTokens, entry.OutputTokens, entry.CachedTokens),
		})
		stored++
	}

	OK(w, map[string]interface{}{"stored": stored})
}
