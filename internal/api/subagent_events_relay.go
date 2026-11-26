package api

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
)

// subagentEventsChannel 是引擎侧广播子 Agent 事件的 Redis 通道
// （发布端见 python-engine/app/subagent/runtime_cache.py 的 publish_live_event）。
func subagentEventsChannel() string { return db.RedisKey("subagent:events") }

// StartSubagentEventsRelay 订阅引擎发布的子 Agent 事件并转投 SSE hub。
//
// 为什么必须有它 —— 子 Agent 的事件有两条去路，缺一不可：
//
//	Redis Stream（subagent:{tenant}:ev:{run}） → 历史回放
//	    GET /v1/subagent/runs/{id}/events 读它（前端断线重连/刷新后靠它补齐）
//	Redis pub/sub（本函数订阅）              → 实时推送
//	    网关转投 SSE hub → 浏览器 EventSource
//
// 而浏览器只从网关的 `/events` 收推送。没有这个中继，后台子 Agent 的事件永远到不了
// 前端：`run_in_background` 的场景下父 turn 早已结束、父 SSE 流也没了，面板就只能靠
// 「选中某个 run 时 3s 轮询」—— 既不实时，还要求用户先点开那个 run。
//
// 形态与 StartAgentCancelSubscriber 一致：进程内一个 goroutine + 一条 Redis 订阅。
func StartSubagentEventsRelay(ctx context.Context, hub *broadcast.Hub) {
	if db.Redis == nil {
		slog.Warn("subagent events relay disabled: redis unavailable")
		return
	}
	if hub == nil {
		slog.Warn("subagent events relay disabled: event hub unavailable")
		return
	}
	go func() {
		channel := subagentEventsChannel()
		pubsub := db.Redis.Subscribe(ctx, channel)
		defer pubsub.Close()
		ch := pubsub.Channel()
		slog.Info("subagent events relay started", "channel", channel)
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("subagent events relay channel closed")
					return
				}
				// 单条坏消息的 panic 不能升级成进程退出：中继是常驻 goroutine，
				// Go 里未捕获的 panic 会终止整个进程（不是单个 goroutine）。
				runBackgroundCycle("subagent-events-relay", func() {
					processSubagentEvent(ctx, hub, msg.Payload)
				})
			}
		}
	}()
}

// maybeNotifySubagentFailed 把"子 Agent 以失败收尾"落成一条站内通知。
//
// ## 为什么从库里反查归属，而不是信 payload
//
// 引擎的 `SubagentEvent` 是**面向渲染**的（type/run_id/status/summary…），刻意不带
// `user_id`/`tenant_id` —— 它经 Redis pub/sub 与 SSE 到浏览器，多带一个身份字段只是
// 多一份可泄露的面。归归属的真相在 `subagent_runs`（引擎落库时写死了 tenant/user），
// 所以这里按 run_id 回查。查不到就跳过：事件可能早于落库到达，而通知是旁路，
// 不该为它等待、重试或反查第二次。
//
// ## 只认 failed / lost
//
// `completed` 与 `partial` 都不是"用户需要知道的事"（partial 的部分产出就在面板里）。
// 把每条终态都写成通知，通知中心就成了运行日志 —— 那正好是它在 SaaS 残留里失败的方式。
func maybeNotifySubagentFailed(ctx context.Context, payload map[string]interface{}) {
	status, _ := payload["status"].(string)
	if status != "failed" && status != "lost" {
		return
	}
	runID, _ := payload["run_id"].(string)
	if runID == "" || db.GlobalDBManager == nil {
		return
	}

	var (
		tenantID  int16
		userID    string
		errorCode string
	)
	// `error_code` 不在 payload 里：`SubagentEvent` 是面向渲染的，只有 status/summary；
	// 而错误码是"用户该知道的那一句原因"，真相在引擎落库的 `subagent_runs` 里。
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT tenant_id, COALESCE(user_id, ''), COALESCE(error_code, '')
		 FROM subagent_runs WHERE id = $1`,
		runID).Scan(&tenantID, &userID, &errorCode)
	if err != nil || userID == "" {
		return
	}

	sessionID, _ := payload["session_id"].(string)
	profile, _ := payload["profile"].(string)

	// dedupeKey 用 run_id：同一个 run 只会失败一次，重跑是新 run_id。
	// 这一条**必须**有 —— 每个网关副本都订阅了同一条 Redis 通道。
	insertNotification(ctx,
		NotificationItem{
			Kind:      NotifSubagentFailed,
			SessionID: sessionID,
			Payload: map[string]interface{}{
				"run_id":     runID,
				"status":     status,
				"profile":    profile,
				"error_code": errorCode,
			},
		},
		tenantID, userID, "subagent_failed:"+runID)
}

// processSubagentEvent 处理一条跨实例子 Agent 事件（由中继循环在 recover 包装内调用）。
func processSubagentEvent(ctx context.Context, hub *broadcast.Hub, rawPayload string) {
	var payload map[string]interface{}
	if err := json.Unmarshal([]byte(rawPayload), &payload); err != nil {
		slog.Warn("subagent event payload not json", "error", err)
		return
	}
	eventType, _ := payload["type"].(string)
	sessionID, _ := payload["session_id"].(string)
	if eventType == "" {
		return
	}
	if sessionID == "" {
		// 没有会话归属就无法路由：SSE 端点是按 session 过滤的，硬塞会给
		// **所有**会话的订阅者都推一份（串扰）。宁可丢这一条并留日志。
		slog.Warn("subagent event without session_id, dropped", "type", eventType)
		return
	}
	// 走 RelayEvent 而不是 Publish：事件本来就是跨实例来的（Redis pub/sub），
	// 再 Publish 会让每个实例都把同一事件跨实例广播一次 ⇒ 客户端收到 N 份重复。
	hub.RelayEvent(broadcast.Event{
		Type:      eventType,
		SessionID: sessionID,
		Data:      payload,
	})

	// 子 Agent 失败 → 站内通知。只在终态且非正常完成时落；去重靠 event_id。
	if eventType == "subagent.done" {
		maybeNotifySubagentFailed(ctx, payload)
	}
}
