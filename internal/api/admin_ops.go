package api

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/id"
)

// ─────────────────────────────────────────────────────────────
// /admin 全栈实装：租户 / 域名 / 数据库 / Redis / 模型 / 定时任务
// 所有数据均来自真实存储（无 mock），读写经 admin 权限路由（adminReadMW/adminWriteMW）。
// ─────────────────────────────────────────────────────────────

// registerOpsRoutes 挂载运维类管理端点（在 adminMux 内，经 StripPrefix /v1/admin）。
func (h *AdminHandler) registerOpsRoutes(r *http.ServeMux) {
	// 数据库管理
	r.HandleFunc("GET /database/configs", h.DatabaseConfigs)
	r.HandleFunc("GET /database/status", h.DatabaseStatus)
	r.HandleFunc("POST /database/query", h.DatabaseQuery)
	r.HandleFunc("POST /database/optimize/{action}", h.DatabaseOptimize)

	// Redis 管理（单实例真实操作）
	r.HandleFunc("GET /redis/slow-log", h.RedisSlowLog)
	r.HandleFunc("POST /redis/flush-all", h.RedisFlushAll)

	// 模型注册表
	r.HandleFunc("GET /models", h.ListModels)
	r.HandleFunc("POST /models", h.CreateModel)
	// 批量开关（/models 页的"组内全选 → 启用/禁用"）：字面量段比 /models/{id} 更具体，
	// Go 1.22+ 的 ServeMux 据此优先匹配，两者可共存。
	r.HandleFunc("PUT /models/batch", h.BatchUpdateModels)
	r.HandleFunc("PUT /models/{id}", h.UpdateModel)
	r.HandleFunc("DELETE /models/{id}", h.DeleteModel)

}

// ── 数据库管理 ──

func (h *AdminHandler) DatabaseConfigs(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT name, setting FROM pg_settings
		 WHERE name IN ('max_connections','shared_buffers','work_mem','maintenance_work_mem','effective_cache_size',
		   'wal_level','max_worker_processes','max_parallel_workers','statement_timeout','idle_in_transaction_session_timeout')
		 ORDER BY name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "read pg settings failed")
		return
	}
	defer rows.Close()
	configs := map[string]string{}
	for rows.Next() {
		var k, v string
		if rows.Scan(&k, &v) == nil {
			configs[k] = v
		}
	}
	OK(w, map[string]interface{}{"configs": configs})
}

func (h *AdminHandler) DatabaseStatus(w http.ResponseWriter, r *http.Request) {
	var version string
	err := db.GlobalDBManager.QueryRow(r.Context(), `SELECT version()`).Scan(&version)
	OK(w, map[string]interface{}{"version": version, "connected": err == nil && version != ""})
}

var selectOnlyRe = regexp.MustCompile(`(?i)^\s*select\b`)
var dangerousQueryRe = regexp.MustCompile(`(?i)\b(pg_read_file|lo_import|lo_export|lo_unlink|copy\s|copy\b|pg_write_file|pg_logdir_ls|pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_sleep|generate_series)\b`)

func (h *AdminHandler) DatabaseQuery(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Query string `json:"query"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || strings.TrimSpace(body.Query) == "" {
		BadRequest(w, "query is required")
		return
	}
	// 只读校验统一走 validateReadOnlySQL（与 /v1/internal/db/* 同一套）：
	// 它额外拦 `SELECT ... INTO`、`WITH` 内嵌 DML 的 CTE、以及 `EXPLAIN ANALYZE
	// DELETE ...` 这类"首关键字是只读、实际会写"的形态 —— 本地这四段手工正则
	// 全都放行过（前面那版只校验 select 前缀 + 危险函数 + 分号）。
	if msg := validateReadOnlySQL(body.Query); msg != "" {
		BadRequest(w, msg)
		return
	}
	// 禁止危险函数调用
	if dangerousQueryRe.MatchString(body.Query) {
		BadRequest(w, "query not allowed")
		return
	}
	// P0 安全：添加查询超时，防止慢查询耗尽连接池
	queryCtx, queryCancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer queryCancel()
	rows, err := db.GlobalDBManager.Query(queryCtx, body.Query)
	if err != nil {
		BadRequest(w, "query failed")
		return
	}
	defer rows.Close()
	cols := make([]string, 0)
	for _, fd := range rows.FieldDescriptions() {
		cols = append(cols, string(fd.Name))
	}
	results := []map[string]interface{}{}
	count := 0
	for rows.Next() && count < 200 {
		vals := make([]interface{}, len(cols))
		ptrs := make([]interface{}, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if rows.Scan(ptrs...) != nil {
			break
		}
		row := map[string]interface{}{}
		for i, c := range cols {
			switch v := vals[i].(type) {
			case []byte:
				row[c] = string(v)
			default:
				row[c] = v
			}
		}
		results = append(results, row)
		count++
	}
	OK(w, map[string]interface{}{"columns": cols, "rows": results, "count": count, "truncated": count >= 200})
}

func (h *AdminHandler) DatabaseOptimize(w http.ResponseWriter, r *http.Request) {
	action := strings.ToLower(r.PathValue("action"))
	if action != "analyze" && action != "vacuum" {
		BadRequest(w, "action must be analyze or vacuum")
		return
	}
	var body struct {
		Table string `json:"table"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Table == "" {
		BadRequest(w, "table is required")
		return
	}
	// 表名白名单校验：仅允许 public schema 的常规表
	var exists bool
	if err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=$1 AND table_type='BASE TABLE')`,
		body.Table).Scan(&exists); err != nil || !exists {
		BadRequest(w, "unknown table: "+body.Table)
		return
	}
	stmt := "ANALYZE " + quoteIdent(body.Table)
	if action == "vacuum" {
		stmt = "VACUUM " + quoteIdent(body.Table)
	}
	if _, err := db.GlobalDBManager.Exec(r.Context(), stmt); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, action+" failed")
		return
	}
	OK(w, map[string]interface{}{"status": "completed", "action": action, "table": body.Table})
}

func quoteIdent(s string) string {
	return `"` + strings.ReplaceAll(s, `"`, `""`) + `"`
}

// ── 模型注册表 ──

type modelRow struct {
	ID              string    `json:"id"`
	Provider        string    `json:"provider"`
	Name            string    `json:"name"`
	DisplayName     string    `json:"display_name"`
	Enabled         bool      `json:"enabled"`
	ContextWindow   int       `json:"context_window"`
	MaxOutputTokens *int      `json:"max_output_tokens"` // NULL = 未知 → 引擎回落默认值
	CreatedAt       time.Time `json:"created_at"`
}

func (h *AdminHandler) ListModels(w http.ResponseWriter, r *http.Request) {
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id::text, provider, name, display_name, enabled, context_window, max_output_tokens, created_at FROM llm_models ORDER BY provider, name`)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list models failed")
		return
	}
	defer rows.Close()
	out := []modelRow{}
	for rows.Next() {
		var m modelRow
		if rows.Scan(&m.ID, &m.Provider, &m.Name, &m.DisplayName, &m.Enabled, &m.ContextWindow, &m.MaxOutputTokens, &m.CreatedAt) == nil {
			out = append(out, m)
		}
	}
	OK(w, map[string]interface{}{"models": out, "total": len(out)})
}

func (h *AdminHandler) CreateModel(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Provider        string `json:"provider"`
		Name            string `json:"name"`
		DisplayName     string `json:"display_name"`
		Enabled         bool   `json:"enabled"`
		ContextWindow   int    `json:"context_window"`
		MaxOutputTokens *int   `json:"max_output_tokens"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Provider == "" || body.Name == "" {
		BadRequest(w, "provider and name are required")
		return
	}
	if body.ContextWindow <= 0 {
		body.ContextWindow = 8192
	}
	if body.MaxOutputTokens != nil && *body.MaxOutputTokens <= 0 {
		// 0 / 负数在这里落成 NULL（= 未知），而不是写进库让引擎把它当成上限。
		body.MaxOutputTokens = nil
	}
	id, _ := id.UUID()
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO llm_models (id, provider, name, display_name, enabled, context_window, max_output_tokens)
		 VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (provider, name) DO UPDATE
		 SET display_name = EXCLUDED.display_name, enabled = EXCLUDED.enabled,
		     context_window = EXCLUDED.context_window,
		     max_output_tokens = COALESCE(EXCLUDED.max_output_tokens, llm_models.max_output_tokens),
		     updated_at = NOW()`,
		id, body.Provider, body.Name, body.DisplayName, body.Enabled, body.ContextWindow, body.MaxOutputTokens); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create model failed")
		return
	}
	OK(w, map[string]interface{}{"id": id, "provider": body.Provider, "name": body.Name})
}

func (h *AdminHandler) UpdateModel(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	var body struct {
		DisplayName     *string `json:"display_name"`
		Enabled         *bool   `json:"enabled"`
		ContextWindow   *int    `json:"context_window"`
		MaxOutputTokens *int    `json:"max_output_tokens"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	// S 安全修复：列名白名单，防止 SQL 注入
	modelColumnMap := map[string]string{
		"display_name":      "display_name",
		"enabled":           "enabled",
		"context_window":    "context_window",
		"max_output_tokens": "max_output_tokens",
	}
	sets, args := []string{}, []interface{}{}
	idx := 1
	if body.DisplayName != nil {
		if col, ok := modelColumnMap["display_name"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *body.DisplayName)
			idx++
		}
	}
	if body.Enabled != nil {
		if col, ok := modelColumnMap["enabled"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *body.Enabled)
			idx++
		}
	}
	if body.ContextWindow != nil {
		if col, ok := modelColumnMap["context_window"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			args = append(args, *body.ContextWindow)
			idx++
		}
	}
	if body.MaxOutputTokens != nil {
		if col, ok := modelColumnMap["max_output_tokens"]; ok {
			sets = append(sets, fmt.Sprintf("%s = $%d", col, idx))
			// <=0 显式落成 NULL：管理端要能"改回未知"，否则填错一次就再也回不去。
			if *body.MaxOutputTokens > 0 {
				args = append(args, *body.MaxOutputTokens)
			} else {
				args = append(args, nil)
			}
			idx++
		}
	}
	if len(sets) == 0 {
		BadRequest(w, "nothing to update")
		return
	}
	args = append(args, id)
	if _, err := db.GlobalDBManager.Exec(r.Context(),
		fmt.Sprintf("UPDATE llm_models SET %s, updated_at = NOW() WHERE id = $%d", strings.Join(sets, ", "), idx), args...); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "update model failed")
		return
	}
	OK(w, map[string]string{"status": "updated"})
}

// maxBatchModelIDs 单次批量的 id 上限。存在的意义是防呆而非性能：这个端点是给
// "整组开关"用的，一次上千个 id 只可能来自前端选择逻辑出错或脚本滥用，
// 而 `unnest($2::text[])` 的参数量也不该无界。
const maxBatchModelIDs = 500

// normalizeBatchModelIDs 归一化批量模型 id：去空白、丢空串、去重。
//
// 去重是必须的：两级分组的"跨组全选"很容易把同一个 id 选两次（同一模型既在
// provider 组又在子组里），重复不影响 UPDATE 的结果，但会让返回的 updated
// 计数虚高 —— 前端拿它提示就会显示"已更新 12 个"而实际只有 10 个。
func normalizeBatchModelIDs(raw []string) ([]string, error) {
	seen := make(map[string]struct{}, len(raw))
	out := make([]string, 0, len(raw))
	for _, id := range raw {
		id = strings.TrimSpace(id)
		if id == "" {
			continue
		}
		if _, dup := seen[id]; dup {
			continue
		}
		seen[id] = struct{}{}
		out = append(out, id)
	}
	if len(out) == 0 {
		return nil, errors.New("ids is empty")
	}
	if len(out) > maxBatchModelIDs {
		return nil, fmt.Errorf("too many ids (max %d)", maxBatchModelIDs)
	}
	return out, nil
}

// BatchUpdateModels 批量启用/禁用：`PUT /admin/models/batch`。
//
// 为什么需要它：在 /models 页把一个 provider 下的几十个模型一起关掉，逐条打
// `PUT /models/{id}` 会发出同样数量的请求，且中途失败会留下"一半开一半关"的
// 中间态、无从回滚。这里一条 `UPDATE ... WHERE id IN (...)` 原子完成。
func (h *AdminHandler) BatchUpdateModels(w http.ResponseWriter, r *http.Request) {
	var body struct {
		IDs []string `json:"ids"`
		// 用指针区分"没传 enabled"与"传了 false"——后者是合法且最常用的请求
		Enabled *bool `json:"enabled"`
	}
	if err := DecodeJSON(w, r, &body); err != nil || body.Enabled == nil {
		BadRequest(w, "ids and enabled are required")
		return
	}
	ids, err := normalizeBatchModelIDs(body.IDs)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	// 参数写成 `$2::text[]`：`llm_models.id` 是 `character varying(36)`（见 0001 基线建表），
	// **不是** uuid 类型。真库用例 TestLiveBatchUpdateModels 在最初写成
	// `unnest($2::text[])::uuid` 时直接报 `operator does not exist: character varying = uuid`。
	// id 里存的就是 UUID 字符串，按文本数组比较即可，且 `= ANY(...)` 仍走主键索引。
	tag, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE llm_models SET enabled = $1, updated_at = NOW() WHERE id = ANY($2::text[])`,
		*body.Enabled, ids)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "batch update models failed")
		return
	}
	OK(w, map[string]interface{}{"updated": tag.RowsAffected(), "enabled": *body.Enabled})
}

func (h *AdminHandler) DeleteModel(w http.ResponseWriter, r *http.Request) {
	if _, err := db.GlobalDBManager.Exec(r.Context(), `DELETE FROM llm_models WHERE id = $1`, r.PathValue("id")); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "delete model failed")
		return
	}
	OK(w, map[string]string{"status": "deleted"})
}

// ListUserModels 用户侧可用模型（仅 enabled）：GET /v1/models
// 动态发现实现见 model_discovery.go（按已配置 provider 的 keyset 实时拉取并缓存到 llm_models）。
func ListUserModels(w http.ResponseWriter, r *http.Request) {
	ListModelsForUser(w, r)
}
