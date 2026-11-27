package api

// F12：用户可见的日费用预算读数。
//
// 管理端已能设置 tenant/user 日上限，但**用户自己在花到 80% 时看不到任何信号**：
// 只会在请求被 429 拦下时才知道超了。这里把"今天花了多少 / 上限多少"作为只读
// 接口暴露给当前登录者（不含其他用户与平台级数据）。

import (
	"net/http"

	"github.com/athenavi/chiron/internal/auth"
)

type usageBudgetView struct {
	UserUsedCents    int64 `json:"user_used_cents"`
	UserLimitCents   int64 `json:"user_limit_cents"`
	TenantUsedCents  int64 `json:"tenant_used_cents"`
	TenantLimitCents int64 `json:"tenant_limit_cents"`
}

// GetUsageBudget 返回当前用户的当日费用与配置上限（0 = 未设置/不限）。
func GetUsageBudget(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	limits := loadDailyCostLimits(r.Context())
	view := usageBudgetView{
		UserLimitCents:   limits.UserCents,
		TenantLimitCents: limits.TenantCents,
	}
	if used, err := dailyCostUsedCents(r.Context(), claims.TenantID, claims.UserID); err == nil {
		view.UserUsedCents = used
	}
	if used, err := dailyCostUsedCents(r.Context(), claims.TenantID, ""); err == nil {
		view.TenantUsedCents = used
	}
	OK(w, view)
}
