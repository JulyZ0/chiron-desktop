package api

// cost_guard.go —— 日费用熔断（R7）。
//
// 数据源是 `usage_events.cost_cents`（一行一次模型调用，按 model_prices 计价）：
// 这里只回答"今天已经花了多少、是否超过上限"，不参与计价本身。
//
// 判据与边界：
//   - 先看租户上限，再看用户上限；任何一层超限即拒绝，scope 告诉调用方是哪一层。
//   - 上限为 0 / 未配置 = 不限；读库失败 = **放行**（fail-open）。熔断是为了防失控，
//     不是为了在数据库抖动时把所有人挡在门外。
//   - 日界按 UTC 的 `date_trunc('day')`，与计费页的 UTC 口径一致，不随进程时区漂移。

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"

	"github.com/athenavi/chiron/internal/db"
)

const settingsCategoryQuota = "quota"

type dailyCostLimits struct {
	// 单位：分。0 = 不限。
	TenantCents int64
	UserCents   int64
	// 每租户可同时授权写回的 client 项目数；0 = 不限（R5 企业上限）。
	DeviceWriteMaxProjects int
}

func quotaLimitsFromConfig(cfg map[string]interface{}) dailyCostLimits {
	get := func(key string) int64 {
		v := intOf(cfg[key])
		if v < 0 {
			return 0
		}
		return int64(v)
	}
	maxProjects := int(get("device_write_max_projects"))
	return dailyCostLimits{
		TenantCents:            get("tenant_daily_cost_cents"),
		UserCents:              get("user_daily_cost_cents"),
		DeviceWriteMaxProjects: maxProjects,
	}
}

func loadDailyCostLimits(ctx context.Context) dailyCostLimits {
	if db.Pool == nil {
		return dailyCostLimits{}
	}
	rows, err := db.Pool.Query(ctx,
		`SELECT key, (value #>> '{}') AS raw FROM system_settings WHERE category = $1`,
		settingsCategoryQuota)
	if err != nil {
		slog.Debug("quota config load failed, treating as unlimited", "error", err)
		return dailyCostLimits{}
	}
	defer rows.Close()
	cfg := map[string]interface{}{}
	for rows.Next() {
		var key, raw string
		if err := rows.Scan(&key, &raw); err != nil {
			continue
		}
		cfg[key] = raw
	}
	return quotaLimitsFromConfig(cfg)
}

// dailyCostUsedCents 统计 UTC 当日已花费（分）。tenantID 为 0 时按所有租户汇总
// （不会发生在请求路径；保留给未来的平台视图）。userID 为空时按租户汇总。
func dailyCostUsedCents(ctx context.Context, tenantID int16, userID string) (int64, error) {
	if db.GlobalDBManager == nil {
		return 0, nil
	}
	row, err := db.GlobalDBManager.FetchOne(ctx,
		`SELECT COALESCE(SUM(cost_cents), 0)::bigint AS used
		   FROM usage_events
		  WHERE tenant_id = $1
		    AND ($2 = '' OR user_id = $2)
		    AND created_at >= (date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')`,
		tenantID, userID)
	if err != nil || row == nil {
		return 0, err
	}
	return int64(intOf(row["used"])), nil
}

// costBreakerExceeded 返回超限层级（tenant/user）与对应数值；未超限时 scope 为空。
// 读库失败按未超限处理（fail-open），只告警。
func costBreakerExceeded(ctx context.Context, tenantID int16, userID string) (scope string, used, limit int64) {
	lim := loadDailyCostLimits(ctx)
	if lim.TenantCents <= 0 && lim.UserCents <= 0 {
		return "", 0, 0
	}
	if lim.TenantCents > 0 {
		spent, err := dailyCostUsedCents(ctx, tenantID, "")
		if err != nil {
			slog.Warn("cost breaker: tenant usage query failed; allowing request", "error", err)
		} else if spent >= lim.TenantCents {
			return "tenant", spent, lim.TenantCents
		}
	}
	if lim.UserCents > 0 {
		spent, err := dailyCostUsedCents(ctx, tenantID, userID)
		if err != nil {
			slog.Warn("cost breaker: user usage query failed; allowing request", "error", err)
		} else if spent >= lim.UserCents {
			return "user", spent, lim.UserCents
		}
	}
	return "", 0, 0
}

// rejectIfOverDailyBudget 供 HTTP 入口在**开始昂贵链路之前**调用。
// 返回 true 表示已写出 429，调用方必须直接 return。
func rejectIfOverDailyBudget(w http.ResponseWriter, r *http.Request, tenantID int16, userID string) bool {
	scope, used, limit := costBreakerExceeded(r.Context(), tenantID, userID)
	if scope == "" {
		return false
	}
	JSONWithCode(w, http.StatusTooManyRequests, "daily_cost_limit_exceeded",
		map[string]interface{}{
			"scope":       scope,
			"used_cents":  used,
			"limit_cents": limit,
		},
		fmt.Sprintf("daily cost budget exceeded for this %s (today: %d / %d cents)", scope, used, limit))
	return true
}
