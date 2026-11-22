package api

import (
	"encoding/csv"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// admin_audit.go —— 操作审计的读取面（列表 + CSV 导出）。
//
// 审计此前只有**写入链路**（middleware 异步写 Redis Stream → audit_sink 批量落
// audit_logs），没有读接口也没有管理台页面：出事时运维只能去翻库。这里补上
// "按租户分页查询 + 过滤 + 导出"，权限沿用 `audit:read`（owner / 租户 admin）。
//
// 不做跨租户聚合：审计可能包含用户与资源标识，跨租户读会让一个租户 admin
// 看到别家的操作面。平台级汇总若要做，应单列 owner-only 端点。

const (
	auditDefaultPerPage = 20
	auditMaxPerPage     = 100
)

type auditLogRow struct {
	ID           string          `json:"id"`
	UserID       string          `json:"user_id,omitempty"`
	Action       string          `json:"action,omitempty"`
	ResourceType string          `json:"resource_type,omitempty"`
	ResourceID   string          `json:"resource_id,omitempty"`
	Details      json.RawMessage `json:"details,omitempty"`
	IPAddress    string          `json:"ip_address,omitempty"`
	CreatedAt    time.Time       `json:"created_at"`
}

// auditFilters 收集过滤条件并拼出 WHERE 子句；args 从 $2 开始（$1 固定是租户）。
type auditFilters struct {
	where string
	args  []any
}

func parseAuditFilters(r *http.Request, tenantID int16) auditFilters {
	f := auditFilters{where: "tenant_id = $1", args: []any{int(tenantID)}}
	q := r.URL.Query()
	addEq := func(column, value string) {
		if value == "" {
			return
		}
		f.args = append(f.args, value)
		f.where += fmt.Sprintf(" AND %s = $%d", column, len(f.args))
	}
	addEq("action", strings.TrimSpace(q.Get("action")))
	addEq("user_id", strings.TrimSpace(q.Get("user_id")))
	addEq("resource_type", strings.TrimSpace(q.Get("resource_type")))

	if from, ok := parseAuditTime(q.Get("from")); ok {
		f.args = append(f.args, from)
		f.where += fmt.Sprintf(" AND created_at >= $%d", len(f.args))
	}
	if to, ok := parseAuditTime(q.Get("to")); ok {
		f.args = append(f.args, to)
		f.where += fmt.Sprintf(" AND created_at <= $%d", len(f.args))
	}
	return f
}

// parseAuditTime 接受 RFC3339 或纯日期（按 UTC 当天 00:00）。
func parseAuditTime(raw string) (time.Time, bool) {
	value := strings.TrimSpace(raw)
	if value == "" {
		return time.Time{}, false
	}
	if t, err := time.Parse(time.RFC3339, value); err == nil {
		return t, true
	}
	if t, err := time.Parse("2006-01-02", value); err == nil {
		return t, true
	}
	return time.Time{}, false
}

func auditPagination(r *http.Request) (page, perPage int) {
	page = 1
	if v, err := strconv.Atoi(strings.TrimSpace(r.URL.Query().Get("page"))); err == nil && v > 0 {
		page = v
	}
	perPage = auditDefaultPerPage
	if v, err := strconv.Atoi(strings.TrimSpace(r.URL.Query().Get("per_page"))); err == nil && v > 0 {
		perPage = v
	}
	if perPage > auditMaxPerPage {
		perPage = auditMaxPerPage
	}
	return page, perPage
}

// ListAuditLogs GET /v1/admin/audit-logs
func (h *AdminHandler) ListAuditLogs(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "audit store unavailable")
		return
	}

	page, perPage := auditPagination(r)
	filters := parseAuditFilters(r, claims.TenantID)

	var total int64
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		"SELECT COUNT(*) FROM audit_logs WHERE "+filters.where, filters.args...,
	).Scan(&total); err != nil {
		slog.Warn("audit list: count failed", "tenant", claims.TenantID, "error", err)
		InternalError(w, "audit query failed")
		return
	}

	listArgs := append(append([]any{}, filters.args...), perPage, (page-1)*perPage)
	query := "SELECT id, COALESCE(user_id, ''), COALESCE(action, ''), " +
		"COALESCE(resource_type, ''), COALESCE(resource_id, ''), details, " +
		"COALESCE(ip_address, ''), COALESCE(created_at, now()) FROM audit_logs WHERE " + filters.where +
		fmt.Sprintf(" ORDER BY created_at DESC LIMIT $%d OFFSET $%d", len(filters.args)+1, len(filters.args)+2)

	rows, err := db.GlobalDBManager.Query(r.Context(), query, listArgs...)
	if err != nil {
		slog.Warn("audit list: query failed", "tenant", claims.TenantID, "error", err)
		InternalError(w, "audit query failed")
		return
	}
	defer rows.Close()

	items := make([]auditLogRow, 0, perPage)
	for rows.Next() {
		var row auditLogRow
		var details []byte
		if err := rows.Scan(&row.ID, &row.UserID, &row.Action, &row.ResourceType,
			&row.ResourceID, &details, &row.IPAddress, &row.CreatedAt); err != nil {
			slog.Warn("audit list: scan failed", "error", err)
			InternalError(w, "audit query failed")
			return
		}
		if len(details) > 0 {
			row.Details = json.RawMessage(details)
		}
		items = append(items, row)
	}
	if err := rows.Err(); err != nil {
		slog.Warn("audit list: rows error", "error", err)
		InternalError(w, "audit query failed")
		return
	}

	OK(w, map[string]interface{}{
		"items":    items,
		"total":    total,
		"page":     page,
		"per_page": perPage,
	})
}

// ExportAuditLogs GET /v1/admin/audit-logs/export.csv
func (h *AdminHandler) ExportAuditLogs(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		ServiceUnavailable(w, "audit store unavailable")
		return
	}

	filters := parseAuditFilters(r, claims.TenantID)
	query := "SELECT id, COALESCE(user_id, ''), COALESCE(action, ''), " +
		"COALESCE(resource_type, ''), COALESCE(resource_id, ''), details, " +
		"COALESCE(ip_address, ''), COALESCE(created_at, now()) FROM audit_logs WHERE " + filters.where +
		" ORDER BY created_at DESC LIMIT 50000"

	rows, err := db.GlobalDBManager.Query(r.Context(), query, filters.args...)
	if err != nil {
		slog.Warn("audit export: query failed", "tenant", claims.TenantID, "error", err)
		InternalError(w, "audit query failed")
		return
	}
	defer rows.Close()

	w.Header().Set("Content-Type", "text/csv; charset=utf-8")
	w.Header().Set("Content-Disposition",
		`attachment; filename="chiron-audit-`+time.Now().UTC().Format("20060102")+`.csv"`)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	// Excel 识别 UTF-8 需要 BOM，否则中文列名会变乱码。
	_, _ = w.Write([]byte{0xEF, 0xBB, 0xBF})

	writer := csv.NewWriter(w)
	_ = writer.Write([]string{
		"created_at", "user_id", "action", "resource_type", "resource_id", "ip_address", "details",
	})
	for rows.Next() {
		var (
			id, userID, action, resourceType, resourceID, ip string
			details                                          []byte
			createdAt                                        time.Time
		)
		if err := rows.Scan(&id, &userID, &action, &resourceType, &resourceID,
			&details, &ip, &createdAt); err != nil {
			slog.Warn("audit export: scan failed", "error", err)
			break
		}
		_ = writer.Write([]string{
			createdAt.UTC().Format(time.RFC3339),
			userID, action, resourceType, resourceID, ip, string(details),
		})
	}
	writer.Flush()
	if err := writer.Error(); err != nil {
		slog.Warn("audit export: csv write failed", "error", err)
	}
}
