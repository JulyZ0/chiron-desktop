package api

import (
	"net/http/httptest"
	"testing"
)

func TestParseAuditTime(t *testing.T) {
	if _, ok := parseAuditTime(""); ok {
		t.Fatal("空串不应被解析成功")
	}
	if _, ok := parseAuditTime("not-a-time"); ok {
		t.Fatal("非法时间不应被解析成功")
	}
	if got, ok := parseAuditTime("2026-11-22"); !ok || got.Format("2006-01-02") != "2026-11-22" {
		t.Fatalf("纯日期解析失败：%v %v", got, ok)
	}
	if got, ok := parseAuditTime("2026-11-22T10:30:00Z"); !ok || got.UTC().Hour() != 10 {
		t.Fatalf("RFC3339 解析失败：%v %v", got, ok)
	}
}

func TestAuditPaginationDefaultsAndCap(t *testing.T) {
	page, perPage := auditPagination(httptest.NewRequest("GET", "/v1/admin/audit-logs", nil))
	if page != 1 || perPage != auditDefaultPerPage {
		t.Fatalf("默认分页 = %d/%d", page, perPage)
	}

	req := httptest.NewRequest("GET", "/v1/admin/audit-logs?page=3&per_page=1000", nil)
	page, perPage = auditPagination(req)
	if page != 3 || perPage != auditMaxPerPage {
		t.Fatalf("上限收敛失败：page=%d per_page=%d", page, perPage)
	}
}

func TestParseAuditFiltersBindsTenantFirst(t *testing.T) {
	req := httptest.NewRequest(
		"GET",
		"/v1/admin/audit-logs?action=user.create&user_id=u1&resource_type=user&from=2026-11-01",
		nil,
	)
	filters := parseAuditFilters(req, 7)

	// 租户永远绑定在 $1：跨租户读审计会把别家的操作面暴露给租户 admin。
	if len(filters.args) == 0 || filters.args[0] != 7 {
		t.Fatalf("第一个参数必须是租户：%v", filters.args)
	}
	if filters.where == "" {
		t.Fatal("where 不应为空")
	}
	wantCount := 1 + 4 // tenant + action + user_id + resource_type + from
	if len(filters.args) != wantCount {
		t.Fatalf("参数数量 = %d, want %d", len(filters.args), wantCount)
	}
}
