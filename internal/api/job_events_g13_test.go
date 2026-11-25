package api

import (
	"encoding/json"
	"testing"
)

// G13：作业事件中继的纯函数判据。
//
// 为什么测这个而不是测整条 relay：relay 本体要真 Redis + hub，属于集成面；
// 而"哪条事件能进 SSE"全部由 decodeJobEvent 决定 —— 把这层规则钉住，
// 串扰（事件被广播给不属于它的会话）这类最贵的问题就不必靠联调才发现。
func TestDecodeJobEventKeepsSessionScopedEvents(t *testing.T) {
	raw, err := json.Marshal(map[string]interface{}{
		"type":       "job.settled",
		"job_id":     "job-1",
		"task_type":  "tool_job",
		"status":     "completed",
		"session_id": "s1",
		"error":      "",
		"ts":         1700000000.5,
	})
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}

	eventType, sessionID, data, ok := decodeJobEvent(raw)
	if !ok {
		t.Fatalf("带会话归属的事件不应被丢弃")
	}
	if eventType != "job.settled" || sessionID != "s1" {
		t.Fatalf("解码结果不符: type=%q session=%q", eventType, sessionID)
	}
	// payload 必须整份透传：前端按 job_id/status/error 渲染卡片，少一个字段就渲染不出来。
	for _, field := range []string{"job_id", "task_type", "status", "error", "ts"} {
		if _, exists := data[field]; !exists {
			t.Fatalf("透传的 payload 丢了字段 %q", field)
		}
	}
}

func TestDecodeJobEventDropsUnroutableEvents(t *testing.T) {
	cases := map[string]string{
		"没有会话归属":    `{"type":"job.started","job_id":"j1","session_id":""}`,
		"没有 type":   `{"job_id":"j1","session_id":"s1"}`,
		"不是 json":   `job.started`,
		"json 不是对象": `[1,2,3]`,
	}
	for name, payload := range cases {
		if _, _, _, ok := decodeJobEvent([]byte(payload)); ok {
			t.Fatalf("%s：应当被丢弃（否则会广播给所有会话）", name)
		}
	}
}

func TestJobEventsChannelFollowsRedisPrefix(t *testing.T) {
	// 多环境共用 Redis 时靠前缀隔离；通道名也必须跟着前缀走，
	// 否则 dev 的事件会推给 prod 的前端。
	t.Setenv("REDIS_KEY_PREFIX", "dev:")
	if got := jobEventsChannel(); got != "dev:job:events" {
		t.Fatalf("通道名没带上前缀: %q", got)
	}
}
