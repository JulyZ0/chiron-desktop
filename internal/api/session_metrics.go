package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// 会话遥测的**实时层写入端**。
//
// **要修的故障**：`GET /v1/sessions/{id}/metrics` 的读取链路是
// 「Redis 热层优先 → DB 回落」。热层用 `session:metrics:<tenant>:<session>` 这个
// hash（field = turn_id）。但**写入端从来不存在** —— 全仓只有读端点
// `metricsFromRedis`，没有任何 `HSET` 往这个 key 里写。于是热层永远为空，
// 每次请求都落到 DB 回落分支：
//
//   - 落库走的是 `turns` 表 + 一次额外的 `usage_events` SUM，两张大表的聚合
//     压在一次 HTTP 请求上；
//   - 更要紧的是 `turns` 的写入是**尽力而为**的，`FinishTurn` 失败只打日志，
//     此时遥测里这一轮就完全消失，而复盘时看起来像"这一轮没跑"。
//
// 热层在回合收尾时**同步写**（与 `usage_events` 同一次收尾、同一个 storeCtx）：
// 谁便宜谁贵不改变"事实只该采一次"的原则，两处写的是同一个回合的同一组数字。
//
// ⚠ Redis 写失败**只告警**：遥测不是回合成败的一部分，DB 回落仍然可用。

// SessionTurnMetrics 是一轮对话在实时层里的记录。
//
// 字段名即 JSON 键，必须与 `aggregateMetrics` 读取的键一致
// （`internal/api/session_runtime.go`）：`input_tokens` / `output_tokens` /
// `cached_tokens` / `cache_hit` / `cost_cents` / `duration_ms`。
type SessionTurnMetrics struct {
	TurnID       string `json:"turn_id"`
	Model        string `json:"model"`
	Status       string `json:"status"`
	InputTokens  int    `json:"input_tokens"`
	OutputTokens int    `json:"output_tokens"`
	CachedTokens int    `json:"cached_tokens"`
	// CacheHit 只有 bool：`aggregateMetrics` 用 `t["cache_hit"] == true` 比较，
	// 写成 "true" 字符串或 1 都会**静默**不计入命中率。
	CacheHit  bool   `json:"cache_hit"`
	CostCents int    `json:"cost_cents"`
	DurationMs int64 `json:"duration_ms"`
	CreatedAt string `json:"created_at"`
}

// RecordSessionMetrics 把一个回合的遥测写进会话实时层。
//
// 与 `RecordUsageEvent` 的区别：那边写**每个模型一次**的明细（一轮多行），
// 这边写**每轮一行**的汇总 —— 遥测问的是"这一轮多快、命中多少"，明细问的是
// "钱花在哪个模型上"。
func RecordSessionMetrics(ctx context.Context, tenant int16, sessionID string, m SessionTurnMetrics) {
	if db.Redis == nil || sessionID == "" || m.TurnID == "" {
		return
	}
	if m.CreatedAt == "" {
		m.CreatedAt = time.Now().UTC().Format(time.RFC3339)
	}
	if m.Status == "" {
		m.Status = "completed"
	}
	payload, err := json.Marshal(m)
	if err != nil {
		// 结构体全是标量，理论上不会失败；真失败了也不能拖住回合收尾。
		slog.Warn("session metrics: marshal failed", "session", sessionID, "turn", m.TurnID, "error", err)
		return
	}
	key := sessionMetricsKey(tenantStr(tenant), sessionID)
	if err := db.Redis.Do(ctx, "HSET", key, m.TurnID, string(payload)).Err(); err != nil {
		slog.Warn("session metrics: redis write failed (db 回落仍可用)",
			"session", sessionID, "turn", m.TurnID, "error", err)
		return
	}
	// 每轮都续期：TTL 是"多久没人说话就忘掉"，不是"这个会话一共活多久"。
	// 只在首轮设置会让长会话的中后期遥测中途全体过期。
	if err := db.Redis.Expire(ctx, key, sessionMetricsTTL).Err(); err != nil {
		slog.Debug("session metrics: expire failed", "session", sessionID, "error", err)
	}
}
