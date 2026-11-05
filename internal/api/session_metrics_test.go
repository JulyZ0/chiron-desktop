package api

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/athenavi/chiron/internal/db"
)

// 会话遥测实时层（`session:metrics:<tenant>:<session>`）的写入契约。
//
// **要修的故障**：读端 `metricsFromRedis` 一直在，写入端从来没有 ⇒ 热层永远为空，
// 每次 `GET /v1/sessions/{id}/metrics` 都落到 `turns` + `usage_events` 的聚合回落上。
//
// 下面钉的是"写进去的 JSON 必须能被读端原样读懂"—— 这两端各写各的，
// 键名或类型一旦漂移，**不会报错**，只会表现为"缓存命中率永远是 0"
// （`aggregateMetrics` 用 `t["cache_hit"] == true` 比较：写成 1 或 "true" 都不成立）。

// TestSessionTurnMetricsJSONContract 钉住字段名与类型。
func TestSessionTurnMetricsJSONContract(t *testing.T) {
	payload, err := json.Marshal(SessionTurnMetrics{
		TurnID:       "turn-1",
		Model:        "gpt-4o",
		Status:       "completed",
		InputTokens:  100,
		OutputTokens: 50,
		CachedTokens: 20,
		CacheHit:     true,
		CostCents:    7,
		DurationMs:   1234,
		CreatedAt:    "2026-11-06T00:00:00Z",
	})
	if err != nil {
		t.Fatal(err)
	}

	var got map[string]any
	if err := json.Unmarshal(payload, &got); err != nil {
		t.Fatal(err)
	}

	// 键名：aggregateMetrics 按这些名字读，少一个就是静默少算一项
	for _, key := range []string{
		"turn_id", "model", "status", "input_tokens", "output_tokens",
		"cached_tokens", "cache_hit", "cost_cents", "duration_ms", "created_at",
	} {
		if _, ok := got[key]; !ok {
			t.Errorf("缺少键 %q（读端会静默跳过）", key)
		}
	}

	// cache_hit 必须是 bool：读端用 == true 比较，数字/字符串都不成立
	if _, isBool := got["cache_hit"].(bool); !isBool {
		t.Fatalf("cache_hit 必须是 JSON bool，实际 %T", got["cache_hit"])
	}
	// 数值必须是数字：读端用 double 解析
	if _, isNum := got["input_tokens"].(float64); !isNum {
		t.Fatalf("input_tokens 必须是 JSON number，实际 %T", got["input_tokens"])
	}
}

// TestRecordSessionMetricsIsNoopWithoutRedis 保证遥测写入绝不拖住回合收尾。
func TestRecordSessionMetricsIsNoopWithoutRedis(t *testing.T) {
	if db.Redis != nil {
		t.Skip("本用例只在无 Redis 的单元测试环境有意义")
	}
	// 不 panic、不阻塞即为通过
	RecordSessionMetrics(context.Background(), 1, "s1", SessionTurnMetrics{TurnID: "t1"})
}

// TestRecordSessionMetricsSkipsWithoutSessionOrTurn 空标识不该造出孤儿 key。
func TestRecordSessionMetricsSkipsWithoutSessionOrTurn(t *testing.T) {
	RecordSessionMetrics(context.Background(), 1, "", SessionTurnMetrics{TurnID: "t1"})
	RecordSessionMetrics(context.Background(), 1, "s1", SessionTurnMetrics{TurnID: ""})
}

// TestUsageCostCentsWithoutPricingData 取不到价格时必须返回 nil（"不知道"），
// 而不是 0（"免费"）—— 两者在汇总里都是 0，但在明细里不该互相冒充。
func TestUsageCostCentsWithoutPricingData(t *testing.T) {
	if db.GlobalDBManager != nil {
		t.Skip("本用例只在无数据库连接的单元测试环境有意义")
	}
	if got := usageCostCents(context.Background(), "gpt-4o", 100, 50, 10); got != nil {
		t.Fatalf("无价格数据时应返回 nil，实际 %d", *got)
	}
	// 空模型名连查都不该查
	if got := usageCostCents(context.Background(), "", 100, 50, 0); got != nil {
		t.Fatalf("空模型名应返回 nil，实际 %d", *got)
	}
}

// TestFloatOfAcceptsDriverShapes numeric 列经驱动回来的形态不定，任何一种
// 形态没覆盖到，单价就会被静默读成 0（成本恒为 0，且不报错）。
func TestFloatOfAcceptsDriverShapes(t *testing.T) {
	cases := []struct {
		in   any
		want float64
	}{
		{float64(0.0025), 0.0025},
		{int64(3), 3},
		{int(4), 4},
		{"0.001", 0.001},
		{" 0.5 ", 0.5},
		{nil, 0},
		{"abc", 0},
	}
	for _, c := range cases {
		if got := floatOf(c.in); got != c.want {
			t.Errorf("floatOf(%#v) = %v，期望 %v", c.in, got, c.want)
		}
	}
}
