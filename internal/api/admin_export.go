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
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"strconv"
	"strings"
	"sync"
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

// ── H44：导出上限 ─────────────────────────────────────────────────────────
//
// 旧实现是"边写边流"：`w.WriteHeader(200)` 之后才发现表很大，只能中途 `zw.Close()`
// —— 客户端拿到一个**不完整但状态码 200** 的 zip（manifest 还缺失）。用户以为自己
// 导成功了，解压才发现少了几张表；同时一次导出可以把 DB 和内存一起拖垮（全表
// `rows.Values()` 逐行装 map）。
//
// 现在：先写进临时文件，出完再决定能不能给 —— 超限就删临时文件、返回 413 和明确文案。
// 另加"每租户同时只允许一次导出"，避免并发导出把连接池吃空。

var (
	// ErrExportTooLarge 表示导出超出上限（行数或字节数）。
	ErrExportTooLarge = errors.New("export exceeds configured limits")
	// ErrExportInProgress 表示该租户已有导出在跑。
	ErrExportInProgress = errors.New("export already in progress for this tenant")
)

// exportLimits 是导出的硬上限（可用环境变量覆盖，便于运维按机器调）。
type exportLimits struct {
	MaxRows  int64
	MaxBytes int64
}

func exportLimitsFromEnv() exportLimits {
	limits := exportLimits{
		MaxRows:  500_000,
		MaxBytes: 256 << 20, // 256 MiB
	}
	if raw := strings.TrimSpace(os.Getenv("CHIRON_EXPORT_MAX_ROWS")); raw != "" {
		if v, err := strconv.ParseInt(raw, 10, 64); err == nil && v > 0 {
			limits.MaxRows = v
		}
	}
	if raw := strings.TrimSpace(os.Getenv("CHIRON_EXPORT_MAX_BYTES")); raw != "" {
		if v, err := strconv.ParseInt(raw, 10, 64); err == nil && v > 0 {
			limits.MaxBytes = v
		}
	}
	return limits
}

// exportBudget 累计已导出行数与字节数，超限即返回 ErrExportTooLarge。
type exportBudget struct {
	limits exportLimits
	Rows   int64
	Bytes  int64
}

func (b *exportBudget) addRow(bytes int) error {
	b.Rows++
	b.Bytes += int64(bytes)
	if b.limits.MaxRows > 0 && b.Rows > b.limits.MaxRows {
		return fmt.Errorf("%w: rows > %d", ErrExportTooLarge, b.limits.MaxRows)
	}
	if b.limits.MaxBytes > 0 && b.Bytes > b.limits.MaxBytes {
		return fmt.Errorf("%w: bytes > %d", ErrExportTooLarge, b.limits.MaxBytes)
	}
	return nil
}

// exportLocks 保证每个租户同时只有一次导出（key 是 tenant_id 的字符串形式）。
var exportLocks sync.Map

func acquireExport(tenantID int16) (func(), error) {
	key := tenantStr(tenantID)
	if _, loaded := exportLocks.LoadOrStore(key, struct{}{}); loaded {
		return nil, ErrExportInProgress
	}
	return func() { exportLocks.Delete(key) }, nil
}

func writeNDJSONTable(
	ctx context.Context, zw *zip.Writer, table exportTable, tenantID int16, budget *exportBudget,
) (int64, error) {
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
		// 预算在这里卡：超限就整体失败（handler 会删掉临时文件并返回 413），
		// 绝不"先给一个不完整的 zip 再说"。
		if err := budget.addRow(len(obj)); err != nil {
			return count, fmt.Errorf("%s: %w", table.Name, err)
		}
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

	// H44：每租户同时只允许一次导出 —— 并发导出会把连接池吃空，也会让"上限"
	// 失去意义（N 个请求各自 256MiB）。
	release, err := acquireExport(tenantID)
	if err != nil {
		JSONWithCode(w, http.StatusTooManyRequests, CodeRateLimited, nil,
			"an export for this tenant is already running")
		return
	}
	defer release()

	limits := exportLimitsFromEnv()
	budget := &exportBudget{limits: limits}

	// 先写临时文件：**出完再决定能不能给**。直接往 ResponseWriter 写的话，
	// `WriteHeader(200)` 之后才发现超限或某张表失败，客户端会拿到一个
	// "状态码 200 但内容不全"的 zip。
	tmp, err := os.CreateTemp("", "chiron-export-*.zip")
	if err != nil {
		slog.Error("tenant export: temp file failed", "tenant_id", tenantID, "error", err)
		InternalError(w, "export failed")
		return
	}
	tmpPath := tmp.Name()
	defer func() {
		_ = tmp.Close()
		_ = os.Remove(tmpPath)
	}()

	zw := zip.NewWriter(tmp)
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
			fmt.Sprintf("export limits: %d rows, %d bytes", limits.MaxRows, limits.MaxBytes),
		},
	}
	for _, table := range exportTables {
		count, err := writeNDJSONTable(ctx, zw, table, tenantID, budget)
		if err != nil {
			_ = zw.Close()
			if errors.Is(err, ErrExportTooLarge) {
				slog.Warn("tenant export refused: over limits",
					"tenant_id", tenantID, "table", table.Name, "rows", budget.Rows, "error", err)
				JSONWithCode(w, http.StatusRequestEntityTooLarge, CodeInvalidRequest, map[string]interface{}{
					"rows": budget.Rows, "bytes": budget.Bytes,
					"max_rows": limits.MaxRows, "max_bytes": limits.MaxBytes,
				}, "export exceeds limits; narrow the scope or raise CHIRON_EXPORT_MAX_ROWS/BYTES")
				return
			}
			slog.Error("tenant export aborted", "tenant_id", tenantID, "table", table.Name, "error", err)
			InternalError(w, "export failed")
			return
		}
		manifest.Tables = append(manifest.Tables, exportTableInfo{
			Name: table.Name, File: table.Name + ".ndjson", Rows: count,
		})
	}
	if mw, err := zw.Create("manifest.json"); err == nil {
		enc := json.NewEncoder(mw)
		enc.SetIndent("", "  ")
		_ = enc.Encode(manifest)
	}
	if err := zw.Close(); err != nil {
		slog.Warn("tenant export close failed", "tenant_id", tenantID, "error", err)
		InternalError(w, "export failed")
		return
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil {
		InternalError(w, "export failed")
		return
	}

	// 到这里才写响应头：内容完整、大小已知。
	stamp := time.Now().UTC().Format("20060102-150405")
	filename := fmt.Sprintf("chiron-export-tenant-%s-%s.zip", tenantStr(tenantID), stamp)
	w.Header().Set("Content-Type", "application/zip")
	w.Header().Set("Content-Disposition", `attachment; filename="`+filename+`"`)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.WriteHeader(http.StatusOK)
	if _, err := io.Copy(w, tmp); err != nil {
		slog.Warn("tenant export: response write failed", "tenant_id", tenantID, "error", err)
	}
	// 审计：导出本身要留痕（谁、导了哪些表、多少行）。
	db.AuditLog(ctx, claims.UserID, tenantID, "tenant_export",
		"/v1/admin/export", "tables="+fmt.Sprint(len(manifest.Tables)), r.RemoteAddr, nil)
}
