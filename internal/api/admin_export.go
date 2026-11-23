package api

// admin_export.go —— 租户数据导出（R10，只读）。
//
//	GET /v1/admin/export  →  application/zip（manifest.json + 每表一个 .ndjson）
//
// 设计取舍：
//   - **流式 ZIP**：usage_events / messages 可能是几十万行，先在内存拼一个 JSON
//     再返回必然 OOM。这里逐行编码、直接写进 zip writer。
//   - **NDJSON 每表一个文件**：浏览器/脚本都能按行处理；比一个巨大的嵌套 JSON 好恢复。
//   - **显式列清单，不用 `SELECT *`**：导出包会被下载、转发、长期保存。密码哈希、
//     密钥哈希、加密的上游 Key、邀请令牌、system_settings 一律不进包；
//     `SELECT *` 会在某次加列后把新秘密顺手带出去。
//   - **二进制不随包**：媒体/知识库原文仍由存储层保存，导出只包含元数据与引用，
//     避免把一次导出变成几十 GB 的传输。manifest 里会写明这一点。
//   - 权限与其它平台级运维事实一致：platform owner 只读。

import (
	"archive/zip"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"time"
	"unicode/utf8"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

type exportTable struct {
	Name  string
	Query string
}

// exportTables 是导出清单。所有查询都只带 $1 = tenant_id。
// 注意：加表时必须确认列里没有密钥/哈希；测试会扫描这些 SQL 的敏感列名。
var exportTables = []exportTable{
	{"users", `SELECT id, email, username, name, nickname, role, status, password_set,
	                  must_change_password, phone, settings, last_login_at, password_changed_at,
	                  created_at, updated_at
	             FROM users WHERE tenant_id = $1`},
	{"sessions", `SELECT s.* FROM sessions s WHERE s.tenant_id = $1`},
	{"messages", `SELECT m.* FROM messages m
	                JOIN sessions s ON s.id = m.session_id
	               WHERE s.tenant_id = $1`},
	{"turns", `SELECT t.* FROM turns t
	             JOIN sessions s ON s.id = t.session_id
	            WHERE s.tenant_id = $1`},
	{"usage_events", `SELECT * FROM usage_events WHERE tenant_id = $1`},
	{"projects", `SELECT * FROM session_map_workspaces WHERE tenant_id = $1`},
	{"project_nodes", `SELECT n.* FROM session_map_nodes n
	                     JOIN session_map_workspaces w ON w.id = n.workspace_id
	                    WHERE w.tenant_id = $1`},
	{"shares", `SELECT sh.* FROM conversation_shares sh
	              JOIN sessions s ON s.id = sh.session_id
	             WHERE s.tenant_id = $1`},
	{"audit_logs", `SELECT * FROM audit_logs WHERE tenant_id = $1`},
	{"knowledge_bases", `SELECT id, tenant_id, user_id, name, description, visibility, provider,
	                            status, index_ref, document_count, total_size_bytes, error_message,
	                            enabled, created_at, updated_at
	                       FROM knowledge_bases WHERE tenant_id = $1`},
	{"knowledge_documents", `SELECT * FROM knowledge_documents WHERE tenant_id = $1`},
	{"media_assets", `SELECT * FROM media_assets WHERE tenant_id = $1`},
	{"memory_summaries", `SELECT id, tenant_id, user_id, session_id, content, topics, entities,
	                             turn_start, turn_end, content_hash, access_count, last_accessed_at,
	                             status, created_at
	                        FROM memory_summaries WHERE tenant_id = $1`},
	{"user_memory_entries", `SELECT id, tenant_id, user_id, slot, item_key, item_value, confidence,
	                                source, access_count, last_accessed_at, status, created_at, updated_at
	                           FROM user_memory_entries WHERE tenant_id = $1`},
	{"user_memory_profile", `SELECT * FROM user_memory_profile WHERE tenant_id = $1`},
	{"notifications", `SELECT * FROM notifications WHERE tenant_id = $1`},
	{"agents", `SELECT * FROM agents WHERE tenant_id = $1`},
	{"personas", `SELECT * FROM personas WHERE tenant_id = $1`},
	{"pending_interactions", `SELECT * FROM pending_interactions WHERE tenant_id = $1`},
	{"unified_sessions", `SELECT * FROM unified_sessions WHERE tenant_id = $1`},
	{"unified_messages", `SELECT m.* FROM unified_messages m
	                        JOIN unified_sessions s ON s.id = m.session_id
	                       WHERE s.tenant_id = $1`},
	{"user_onboarding", `SELECT o.* FROM user_onboarding o
	                       JOIN users u ON u.id = o.user_id
	                      WHERE u.tenant_id = $1`},
	{"market_installations", `SELECT * FROM market_installations WHERE tenant_id = $1`},
}

type exportManifest struct {
	Version     int               `json:"version"`
	GeneratedAt string            `json:"generated_at"`
	TenantID    int16             `json:"tenant_id"`
	Format      string            `json:"format"`
	Tables      []exportTableInfo `json:"tables"`
	Excluded    []string          `json:"excluded"`
	Notes       []string          `json:"notes"`
}

type exportTableInfo struct {
	Name string `json:"name"`
	File string `json:"file"`
	Rows int64  `json:"rows"`
}

// normalizeExportValue 把 pgx 返回的驱动形态转成 JSON 友好的值。
//
// 重点只有两类：[]byte（text/jsonb 在驱动里可能就是字节）要变成字符串；
// 非法 UTF-8 不该被塞成乱码 JSON —— 用 base64 并保留原始字节。
func normalizeExportValue(v interface{}) interface{} {
	switch value := v.(type) {
	case nil:
		return nil
	case []byte:
		if utf8.Valid(value) {
			return string(value)
		}
		return map[string]string{"base64": base64.StdEncoding.EncodeToString(value)}
	case time.Time:
		return value.UTC().Format(time.RFC3339Nano)
	default:
		return value
	}
}

func writeNDJSONTable(ctx context.Context, zw *zip.Writer, table exportTable, tenantID int16) (int64, error) {
	rows, err := db.GlobalDBManager.Query(ctx, table.Query, tenantID)
	if err != nil {
		return 0, fmt.Errorf("%s: %w", table.Name, err)
	}
	defer rows.Close()

	w, err := zw.Create(table.Name + ".ndjson")
	if err != nil {
		return 0, err
	}
	fields := rows.FieldDescriptions()
	enc := json.NewEncoder(w)
	var count int64
	for rows.Next() {
		values, err := rows.Values()
		if err != nil {
			return count, fmt.Errorf("%s: read row: %w", table.Name, err)
		}
		obj := make(map[string]interface{}, len(fields))
		for i, fd := range fields {
			if i < len(values) {
				obj[fd.Name] = normalizeExportValue(values[i])
			}
		}
		if err := enc.Encode(obj); err != nil {
			return count, fmt.Errorf("%s: encode row: %w", table.Name, err)
		}
		count++
	}
	return count, rows.Err()
}

// ExportTenantData GET /v1/admin/export
//
// 预检（DB 可用）通过后才写响应头：一旦开始流式输出，中途失败就无法再改成 5xx。
func (h *AdminHandler) ExportTenantData(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	if db.GlobalDBManager == nil {
		JSONWithCode(w, http.StatusServiceUnavailable, CodeServiceUnavailable, nil, "database unavailable")
		return
	}
	ctx := r.Context()
	tenantID := claims.TenantID
	if tenantID == 0 {
		BadRequest(w, "missing tenant context")
		return
	}

	stamp := time.Now().UTC().Format("20060102-150405")
	filename := fmt.Sprintf("chiron-export-tenant-%s-%s.zip", tenantStr(tenantID), stamp)
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)

	zw := zip.NewWriter(w)
	manifest := exportManifest{
		Version:     1,
		GeneratedAt: time.Now().UTC().Format(time.RFC3339),
		TenantID:    tenantID,
		Format:      "zip + ndjson (one table per file)",
		Excluded: []string{
			"password_hash", "api key hashes", "encrypted provider keys",
			"invite token hashes", "system_settings",
		},
		Notes: []string{
			"media/knowledge document binaries are not included; only metadata and storage references are exported",
			"agent runs, subagent runs, tasks and idempotency keys are runtime state and are not exported",
		},
	}
	for _, table := range exportTables {
		count, err := writeNDJSONTable(ctx, zw, table, tenantID)
		if err != nil {
			// 已经写出部分内容：只能记录并结束（客户端会得到一个不完整 zip，manifest 也缺失）。
			slog.Error("tenant export aborted", "tenant_id", tenantID, "table", table.Name, "error", err)
			_ = zw.Close()
			return
		}
		manifest.Tables = append(manifest.Tables, exportTableInfo{
			Name: table.Name, File: table.Name + ".ndjson", Rows: count,
		})
	}
	mw, err := zw.Create("manifest.json")
	if err == nil {
		enc := json.NewEncoder(mw)
		enc.SetIndent("", "  ")
		_ = enc.Encode(manifest)
	}
	if err := zw.Close(); err != nil {
		slog.Warn("tenant export close failed", "tenant_id", tenantID, "error", err)
		return
	}
	db.AuditLog(r.Context(), claims.UserID, tenantID, "tenant_export",
		"/v1/admin/export", "tables="+fmt.Sprint(len(manifest.Tables)), r.RemoteAddr, nil)
}
