package api

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
)

func TestGetUsageBudgetRequiresAuth(t *testing.T) {
	rec := httptest.NewRecorder()
	GetUsageBudget(rec, httptest.NewRequest(http.MethodGet, "/v1/usage/budget", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("未登录状态=%d，want 401", rec.Code)
	}
}

func TestGetUsageBudgetReturnsZeroesWithoutConfiguredLimits(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/v1/usage/budget", nil)
	req = req.WithContext(auth.WithClaims(req.Context(), &auth.Claims{UserID: "u1", TenantID: 1}))
	rec := httptest.NewRecorder()
	GetUsageBudget(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("状态=%d，want 200；body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), `"user_limit_cents":0`) ||
		!strings.Contains(rec.Body.String(), `"tenant_limit_cents":0`) {
		t.Fatalf("未配置上限时应返回 0: %s", rec.Body.String())
	}
}
