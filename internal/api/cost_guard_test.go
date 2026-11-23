package api

// 费用熔断的纯逻辑：配置解析与"未配置/库不可用"的 fail-open 语义。
// 真正按 usage_events 汇总需要 live PG，由部署联调覆盖。

import (
	"context"
	"testing"
)

func TestQuotaLimitsFromConfig(t *testing.T) {
	cfg := map[string]interface{}{
		"tenant_daily_cost_cents":   "5000", // settings 表 value #>> '{}' 给的是文本
		"user_daily_cost_cents":     float64(300),
		"device_write_max_projects": "3",
	}
	got := quotaLimitsFromConfig(cfg)
	if got.TenantCents != 5000 || got.UserCents != 300 || got.DeviceWriteMaxProjects != 3 {
		t.Fatalf("unexpected limits: %+v", got)
	}

	// 0 / 负数 / 缺失 = 不限，不能被当成"额度为负"而永久拒绝。
	for _, cfg := range []map[string]interface{}{
		{},
		{"tenant_daily_cost_cents": "0", "user_daily_cost_cents": "0"},
		{"tenant_daily_cost_cents": "-1"},
		{"tenant_daily_cost_cents": "abc"},
	} {
		if got := quotaLimitsFromConfig(cfg); got.TenantCents != 0 || got.UserCents != 0 {
			t.Fatalf("non-positive/invalid limits must normalize to unlimited: %#v -> %+v", cfg, got)
		}
	}
}

func TestCostBreakerFailOpenWithoutLimitsOrDB(t *testing.T) {
	// 未配置（测试环境无 system_settings）时必须放行；库不可用同样放行。
	scope, used, limit := costBreakerExceeded(context.Background(), 1, "u1")
	if scope != "" || used != 0 || limit != 0 {
		t.Fatalf("expected fail-open, got scope=%q used=%d limit=%d", scope, used, limit)
	}
	// 汇总查询本身可以把"库不可用"作为错误返回；调用方的契约是**放行**。
	if spent, _ := dailyCostUsedCents(context.Background(), 1, "u1"); spent != 0 {
		t.Fatalf("db unavailable should not invent spend, got %d", spent)
	}
}
