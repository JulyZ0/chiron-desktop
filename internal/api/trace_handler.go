package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
)

// TraceSpan represents a single execution span from Redis Stream.
type TraceSpan struct {
	TraceID    string         `json:"trace_id"`
	SpanName   string         `json:"span_name"`
	DurationMs int            `json:"duration_ms"`
	Timestamp  time.Time      `json:"timestamp"`
	TenantID   string         `json:"tenant_id"`
	// UserID 是 span 的发起用户（H27）。按 tenant 分 stream 只能隔离租户；
	// 同租户内的普通用户此前可以按 trace_id 读到别人的完整调用链。
	UserID   string         `json:"user_id,omitempty"`
	Metadata map[string]any `json:"metadata,omitempty"`
	// 差距C（执行图）：稳定的节点 id、父指针与类别。
	// 有了 ParentSpanID，"某一步是从哪来的"才是**可查询**的 —— 此前只有扁平时间序列。
	SpanID       string `json:"span_id,omitempty"`
	ParentSpanID string `json:"parent_span_id,omitempty"`
	Kind         string `json:"kind,omitempty"`
}

// TraceGraphNode 是执行图里的一个节点（含其子节点，递归）。
type TraceGraphNode struct {
	TraceSpan
	Children []TraceGraphNode `json:"children,omitempty"`
}

// TraceGraph 是一次 run 的**执行图**：根节点 + 便于渲染的扁平统计。
type TraceGraph struct {
	TraceID  string           `json:"trace_id"`
	TenantID string           `json:"tenant_id"`
	Roots    []TraceGraphNode `json:"roots"`
	// NodeCount / OrphanCount 让"图是否完整"可判断：孤儿节点（父不在本 trace 内）
	// 说明 span 写入有缺口（例如批量路径漏写父指针），排障时一眼可见。
	NodeCount   int `json:"node_count"`
	OrphanCount int `json:"orphan_count"`
}

// TraceQuery aggregates all spans for a given trace_id.
type TraceQuery struct {
	TraceID         string      `json:"trace_id"`
	Tenant          string      `json:"tenant_id"`
	SpanCount       int         `json:"span_count"`
	TotalDurationMs int         `json:"total_duration_ms"`
	Spans           []TraceSpan `json:"spans"`
}

// TraceHandler provides trace query APIs with tenant isolation.
type TraceHandler struct {
	rdb db.RedisClient
}

// NewTraceHandler creates a new trace query handler.
func NewTraceHandler(rdb db.RedisClient) *TraceHandler {
	return &TraceHandler{rdb: rdb}
}

// GetTrace retrieves the complete call chain for a trace_id.
// GET /v1/traces/{trace_id}
func (h *TraceHandler) GetTrace(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	traceID := r.PathValue("trace_id")
	if traceID == "" {
		BadRequest(w, "trace_id is required")
		return
	}

	tenantID := tenantStr(claims.TenantID)
	if tenantID == "" {
		tenantID = claims.UserID
	}

	spans, err := h.queryTraces(traceID, tenantID)
	if err != nil {
		slog.Error("trace query failed", "trace_id", traceID, "tenant", tenantID, "error", err)
		InternalError(w, "trace query failed")
		return
	}
	spans = filterTraceSpansForCaller(spans, claims)
	if len(spans) == 0 {
		NotFound(w, "trace not found")
		return
	}

	for _, span := range spans {
		if span.TenantID != tenantID && span.TenantID != "" {
			slog.Warn("trace span tenant mismatch (possible cross-tenant leak)",
				"trace_id", traceID,
				"claim_tenant", tenantID,
				"span_tenant", span.TenantID,
			)
			InternalError(w, "trace data integrity check failed")
			return
		}
	}

	// Build aggregated response
	totalDuration := 0
	for _, s := range spans {
		totalDuration += s.DurationMs
	}

	result := TraceQuery{
		TraceID:         traceID,
		Tenant:          tenantID,
		SpanCount:       len(spans),
		TotalDurationMs: totalDuration,
		Spans:           spans,
	}

	OK(w, result)
}

// GetTraceGraph returns the **execution graph** for a trace_id.
//
// GET /v1/traces/{trace_id}/graph
//
// 与 GetTrace 的区别：GetTrace 返回**扁平的 span 列表**（"这次有哪些步骤"），
// 本端点返回**父子连通的图**（"某一步是从哪来的"）。企业审计与排障要的是后者。
func (h *TraceHandler) GetTraceGraph(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	traceID := r.PathValue("trace_id")
	if traceID == "" {
		BadRequest(w, "trace_id is required")
		return
	}
	tenantID := tenantStr(claims.TenantID)
	if tenantID == "" {
		tenantID = claims.UserID
	}

	spans, err := h.queryTraces(traceID, tenantID)
	if err != nil {
		slog.Error("trace graph query failed", "trace_id", traceID, "error", err)
		InternalError(w, "trace query failed")
		return
	}
	spans = filterTraceSpansForCaller(spans, claims)
	if len(spans) == 0 {
		NotFound(w, "trace not found")
		return
	}
	// 租户一致性：与 GetTrace 同一道校验（跨租户数据不得出现在响应里）。
	for _, span := range spans {
		if span.TenantID != tenantID && span.TenantID != "" {
			slog.Warn("trace graph tenant mismatch (possible cross-tenant leak)",
				"trace_id", traceID, "claim_tenant", tenantID, "span_tenant", span.TenantID)
			InternalError(w, "trace data integrity check failed")
			return
		}
	}

	OK(w, BuildTraceGraph(traceID, tenantID, spans))
}

// BuildTraceGraph 把扁平的 span 列表组装成执行图。
//
// **纯函数**（无 IO、无并发、不改输入），因此可单测 —— 图的正确性是审计结论的前提。
// 规则：
//   - SpanID 为空（历史记录 / 未接入）⇒ 作为根节点（无父子信息，按扁平处理）；
//   - ParentSpanID 为空 ⇒ 根节点；
//   - ParentSpanID 非空但父不在本 trace 内 ⇒ 计入 OrphanCount 并提升为根
//     （写入缺口 —— 例如批量路径漏写父指针 —— 排障时一眼可见）。
// 同层按时间排序，保证渲染稳定。
func BuildTraceGraph(traceID, tenantID string, spans []TraceSpan) TraceGraph {
	byID := make(map[string]int, len(spans))
	for i, s := range spans {
		if s.SpanID != "" {
			byID[s.SpanID] = i
		}
	}
	children := make(map[string][]int)
	var roots []int
	orphans := 0
	for i, s := range spans {
		if s.ParentSpanID == "" {
			roots = append(roots, i)
			continue
		}
		if _, ok := byID[s.ParentSpanID]; ok {
			children[s.ParentSpanID] = append(children[s.ParentSpanID], i)
			continue
		}
		orphans++
		roots = append(roots, i)
	}

	var build func(idx int) TraceGraphNode
	build = func(idx int) TraceGraphNode {
		s := spans[idx]
		node := TraceGraphNode{TraceSpan: s}
		kids := children[s.SpanID]
		sort.SliceStable(kids, func(a, b int) bool {
			return spans[kids[a]].Timestamp.Before(spans[kids[b]].Timestamp)
		})
		for _, k := range kids {
			node.Children = append(node.Children, build(k))
		}
		return node
	}

	sort.SliceStable(roots, func(a, b int) bool {
		return spans[roots[a]].Timestamp.Before(spans[roots[b]].Timestamp)
	})
	graph := TraceGraph{
		TraceID:     traceID,
		TenantID:    tenantID,
		NodeCount:   len(spans),
		OrphanCount: orphans,
	}
	for _, r := range roots {
		graph.Roots = append(graph.Roots, build(r))
	}
	return graph
}

// ListTraces lists recent trace IDs for a tenant.
// GET /v1/traces?limit=20&tenant_id=xxx
func (h *TraceHandler) ListTraces(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}

	limitStr := r.URL.Query().Get("limit")
	limit, _ := strconv.Atoi(limitStr)
	if limit <= 0 || limit > 100 {
		limit = 20
	}

	tenantID := tenantStr(claims.TenantID)
	if tenantID == "" {
		tenantID = claims.UserID
	}

	// Scan latest trace entries from Redis Stream (tenant isolated)
	// 统一键前缀（与引擎侧 rkey("chiron:traces:") 对齐，多环境隔离）
	streamKey := db.RedisKey("chiron:traces:" + tenantID)

	// 使用 XRevRange 读取最新的 N 条记录（避免 XRANGE 全量扫描）
	rawEntries, err := h.rdb.XRange(r.Context(), streamKey, "+", "-", int64(limit*10)).Result()
	if err != nil {
		slog.Warn("trace list scan failed", "tenant", tenantID, "error", err)
		OK(w, map[string]any{"traces": []any{}, "count": 0})
		return
	}

	// Aggregate unique trace_ids with metadata
	traceMap := make(map[string]*TraceSpan)
	for _, entry := range rawEntries {
		var span TraceSpan
		metaRaw, _ := entry.Values["metadata"].(string)
		if err := json.Unmarshal([]byte(metaRaw), &span.Metadata); err != nil {
			continue
		}

		span.TraceID, _ = entry.Values["trace_id"].(string)
		span.SpanName, _ = entry.Values["span_name"].(string)
		durationMs, _ := entry.Values["duration_ms"].(string)
		span.DurationMs, _ = strconv.Atoi(durationMs)
		timestamp, _ := entry.Values["timestamp"].(string)
		span.Timestamp, _ = time.Parse(time.RFC3339, timestamp)

		// Only keep the first occurrence of each trace_id (latest)
		if _, exists := traceMap[span.TraceID]; !exists {
			traceMap[span.TraceID] = &span
		}
	}

	// Convert to list
	traces := make([]any, 0, len(traceMap))
	for _, span := range traceMap {
		if span.TenantID != "" && span.TenantID != tenantID {
			slog.Warn("skipping cross-tenant trace entry",
				"trace_id", span.TraceID,
				"expected_tenant", tenantID,
				"actual_tenant", span.TenantID,
			)
			continue
		}

		traces = append(traces, map[string]any{
			"trace_id":    span.TraceID,
			"span_name":   span.SpanName,
			"duration_ms": span.DurationMs,
			"timestamp":   span.Timestamp.Format(time.RFC3339),
			"metadata":    span.Metadata,
		})
	}

	OK(w, map[string]any{
		"traces": traces,
		"count":  len(traces),
	})
}

// filterTraceSpansForCaller 按用户归属收窄 span 列表（H27）。
//
// 规则（fail-closed）：
//   - span 带 user_id：只有发起者本人可见；
//   - span 不带 user_id（引擎升级前的历史记录 / 无法归属）：只有具备 admin:read
//     的租户管理员可见 —— 普通用户拿到的是"看不见"，不是"猜不到"。
//
// 为什么不能像以前那样只看 tenant：trace stream 的键是 `chiron:traces:{tenant}`，
// 同一租户内所有用户的 span 混在一条流里；trace_id 会在事件流/日志里出现，
// 只按 tenant 过滤等于"猜到别人的 trace_id 就能读到完整调用链"。
func filterTraceSpansForCaller(spans []TraceSpan, claims *auth.Claims) []TraceSpan {
	if claims == nil || len(spans) == 0 {
		return nil
	}
	out := make([]TraceSpan, 0, len(spans))
	for _, span := range spans {
		if traceOwnerVisible(span.UserID, claims) {
			out = append(out, span)
		}
	}
	return out
}

// traceOwnerVisible 是单条 span 的可见性判定（纯函数，便于单测）。
//
// 规则：
//   - 本人发起 → 可见；
//   - 具备 `admin:read` → 可见（跨用户排查/审计是这条权限的用途，否则线上
//     "某个用户的链路为什么慢"根本查不了）；
//   - 无法归属（升级前的老记录没有 user_id）→ 只给管理员；普通用户 fail-closed。
func traceOwnerVisible(owner string, claims *auth.Claims) bool {
	if claims == nil {
		return false
	}
	owner = strings.TrimSpace(owner)
	caller := strings.TrimSpace(claims.UserID)
	if owner != "" && caller != "" && owner == caller {
		return true
	}
	return auth.HasPermission(claims, auth.PermAdminRead)
}

// traceSpanOwner 读取 span 的发起用户：优先 Stream 字段，回落到 metadata.user_id
// （引擎在不同版本里可能只写其一，两条都要收，否则升级期会出现"自己的 span 看不见"）。
func traceSpanOwner(values map[string]interface{}, metadata map[string]any) string {
	if raw, ok := values["user_id"].(string); ok && strings.TrimSpace(raw) != "" {
		return strings.TrimSpace(raw)
	}
	if metadata != nil {
		if raw, ok := metadata["user_id"].(string); ok {
			return strings.TrimSpace(raw)
		}
	}
	return ""
}

// queryTraces fetches all spans for a trace_id from Redis Stream.
func (h *TraceHandler) queryTraces(traceID, tenantID string) ([]TraceSpan, error) {
	if h.rdb == nil {
		return nil, nil // Redis unavailable 鈫?return empty
	}

	streamKey := db.RedisKey("chiron:traces:" + tenantID)

	// 使用 XRevRange 读取最近 1000 条记录（避免 XRANGE 全量扫描）
	queryCtx, queryCancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer queryCancel()
	entries, err := h.rdb.XRange(queryCtx, streamKey, "+", "-", 1000).Result()
	if err != nil {
		return nil, err
	}

	// Filter by trace_id and build ordered list
	var spans []TraceSpan
	for _, entry := range entries {
		entryTraceID, _ := entry.Values["trace_id"].(string)
		if entryTraceID != traceID {
			continue
		}

		var span TraceSpan
		metaRaw, _ := entry.Values["metadata"].(string)
		if err := json.Unmarshal([]byte(metaRaw), &span.Metadata); err != nil {
			continue
		}

		span.TraceID = entryTraceID
		span.SpanName, _ = entry.Values["span_name"].(string)
		durationMs, _ := entry.Values["duration_ms"].(string)
		span.DurationMs, _ = strconv.Atoi(durationMs)
		span.TenantID, _ = entry.Values["tenant_id"].(string)
		// H27：span 的发起用户。引擎在 stream 里显式写 `user_id`（新版本），
		// 老记录只能从 metadata 里取；两条都取不到就是"无法归属"，
		// 由 `filterTraceSpansForCaller` 按 fail-closed 处理（仅管理员可见）。
		span.UserID = traceSpanOwner(entry.Values, span.Metadata)
		timestamp, _ := entry.Values["timestamp"].(string)
		span.Timestamp, _ = time.Parse(time.RFC3339, timestamp)
		// 差距C：执行图字段（旧记录没有这些键 ⇒ 保持零值，按扁平时间线处理）。
		span.SpanID, _ = entry.Values["span_id"].(string)
		span.ParentSpanID, _ = entry.Values["parent_span_id"].(string)
		span.Kind, _ = entry.Values["kind"].(string)

		spans = append(spans, span)
	}

	return spans, nil
}
