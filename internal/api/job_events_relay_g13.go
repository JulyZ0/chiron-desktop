package api

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/athenavi/chiron/internal/broadcast"
	"github.com/athenavi/chiron/internal/db"
)

// jobEventsChannel 是引擎侧广播**作业生命周期事件**的 Redis 通道。
//
// 发布端：python-engine/app/queue/job_events.py（emit_job_event → redis.publish）。
// 订阅端：本文件的 StartJobEventsRelay。
//
// 这个字面量是**跨语言契约**：两侧任一改动都会让实时推送静默失效（事件照发、没人收，
// 不报错）。因此 python-engine/tests/test_queue_job_events_g13.py 对两侧都做了断言 ——
// 剥掉注释后匹配 `db.RedisKey("job:events")`，改动任何一边都会红。
func jobEventsChannel() string { return db.RedisKey("job:events") }

// decodeJobEvent 把一条引擎投递的作业事件解成 SSE 帧需要的 (type, sessionID, data)。
//
// 抽成纯函数是刻意的：它承载了全部校验规则（有没有 type、有没有会话归属），
// 不该只能靠"连上 Redis 跑一遍"才验证得到 —— 见 job_events_g13_test.go。
//
// 缺 session_id 的**必须**丢弃：SSE 端点按 session 过滤，没有归属就只能广播给所有
// 订阅者（串扰）。宁可不推，也不要让 A 会话的后台作业出现在 B 会话里 ——
// 与 subagent_events_relay.go 同一判据。
func decodeJobEvent(raw []byte) (eventType string, sessionID string, data map[string]interface{}, ok bool) {
	var payload map[string]interface{}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return "", "", nil, false
	}
	eventType, _ = payload["type"].(string)
	sessionID, _ = payload["session_id"].(string)
	if eventType == "" || sessionID == "" {
		return "", "", nil, false
	}
	return eventType, sessionID, payload, true
}

// StartJobEventsRelay 订阅引擎的作业事件并转投 SSE hub（G13）。
//
// 为什么需要它：任务的起停/完成/失败此前只存在于队列内部（PEL + idempotency）与
// 各端自己拼的"进度"里。引擎现在把这三件事发成 `job.started` / `job.settled`，
// 浏览器只从网关的 `/events` 收推送 —— 没有这个中继，会话里的作业卡片永远不会更新，
// 前端只能退回到轮询队列状态（既不实时，也看不到"没跑就被丢弃"的任务）。
//
// 形态与 StartSubagentEventsRelay 一致：进程内一个 goroutine + 一条 Redis 订阅。
// 两者的差别只有订阅的通道名 —— 通道分开是刻意的：作业事件与会话归属的语义不同
// （作业可以没有会话），混在一条通道上会让"要不要丢"的判断互相污染。
func StartJobEventsRelay(ctx context.Context, hub *broadcast.Hub) {
	if db.Redis == nil {
		slog.Warn("job events relay disabled: redis unavailable")
		return
	}
	if hub == nil {
		slog.Warn("job events relay disabled: event hub unavailable")
		return
	}
	go func() {
		channel := jobEventsChannel()
		pubsub := db.Redis.Subscribe(ctx, channel)
		defer pubsub.Close()
		ch := pubsub.Channel()
		slog.Info("job events relay started", "channel", channel)
		for {
			select {
			case <-ctx.Done():
				return
			case msg, ok := <-ch:
				if !ok {
					slog.Warn("job events relay channel closed")
					return
				}
				// 单条消息的处理体包 recover：这条中继是进程级常驻 goroutine，
				// 裸 panic 会终止**整个进程**（Go 的 goroutine panic 语义），
				// 一条畸形 Redis 消息就能让所有副本 CrashLoop。
				runBackgroundCycle("job-events-relay", func() {
					eventType, sessionID, payload, ok := decodeJobEvent([]byte(msg.Payload))
					if !ok {
						slog.Warn("job event dropped (no session or no type)", "payload_bytes", len(msg.Payload))
						return
					}
					// 走 RelayEvent 而不是 Publish：事件本来就是跨实例来的（Redis pub/sub），
					// 再 Publish 会让每个实例都把同一事件跨实例广播一次 ⇒ 客户端收到 N 份重复。
					hub.RelayEvent(broadcast.Event{
						Type:      eventType,
						SessionID: sessionID,
						Data:      payload,
					})
				})
			}
		}
	}()
}
