package api

// ── 子 Agent 运行观测 API ──
//
// 三个只读端点，供前端画"递归层级树 + 侧边栏看运行中子 Agent 输出"：
//
//	GET /v1/subagent/runs?session_id=&parent_run_id=   整树/子节点列表
//	GET /v1/subagent/runs/{run_id}                     单 run 详情（状态/摘要/用量）
//	GET /v1/subagent/runs/{run_id}/events?limit=       输出过程（Redis Stream → DB steps 回落）
//
// 数据源分层：Redis = 运行期（TTL 1h，引擎写入，key 带 tenant 维度）；
// PostgreSQL = 权威。每个查询都"先 Redis 后 DB"，Redis 不可用或键过期时自动回落。
//
// 安全：全部要求鉴权；租户取自 claims（TenantID 回退 UserID）；DB 查询强制 tenant_id 过滤，
// 因此不存在跨租户读取子 Agent 输出的路径。

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

const (
	subagentListLimit    = 200
	subagentEventsLimit  = 200
	subagentContentChars = 8000
)

type SubagentHandler struct {
	rdb db.RedisClient
	// engine 只用于**写操作**（目前的 P0-2 重跑转发）：只读端点仍走 Redis/DB，
	// 不经过引擎（避免一次观测请求被引擎的可用性绑架）。
	engine *engine.PythonClient
	// store 是 DB 的窄接口（与 apiKeyStore 同一理由）：所有权判定必须能被单测
	// 覆盖（跨用户/跨租户 404），而仓库既有单测不连真实 PostgreSQL。
	store subagentStore
}

// subagentStore 是子 Agent 观测/取消所需的最小数据库能力（`db.DBManager` 天然满足）。
type subagentStore interface {
	FetchOne(ctx context.Context, sql string, args ...interface{}) (map[string]interface{}, error)
	FetchAll(ctx context.Context, sql string, args ...interface{}) ([]map[string]interface{}, error)
	Execute(ctx context.Context, sql string, args ...interface{}) (int64, error)
}

func NewSubagentHandler(rdb db.RedisClient, pc *engine.PythonClient) *SubagentHandler {
	var store subagentStore
	if db.GlobalDBManager != nil {
		store = db.GlobalDBManager
	}
	return &SubagentHandler{rdb: rdb, engine: pc, store: store}
}

// subagentTenant 与 trace_handler 保持同一套租户判定（TenantID 回退 UserID）。
func subagentTenant(claims *auth.Claims) string {
	if claims == nil {
		return ""
	}
	if claims.TenantID != 0 {
		return tenantStr(claims.TenantID)
	}
	return claims.UserID
}

// subagentKey 构造运行期缓存 key；必须与引擎 `app/subagent/runtime_cache.py` 逐字一致。
func subagentKey(parts ...string) string {
	return db.RedisKey("subagent:" + strings.Join(parts, ":"))
}

// ListRuns 返回整棵层级树（session_id）或某节点的直接子节点（parent_run_id）。
//
// H27：会话树与父节点两条入口都先校验**调用者所有权**（tenant + user；会话还要
// 校验会话归属），别人的会话/父 run 一律 404 —— 返回空列表会让越权与"确实没有"
// 无法区分，也挡不住"用别人的 session_id 探边界"。
func (h *SubagentHandler) ListRuns(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenant := subagentTenant(claims)
	sessionID := strings.TrimSpace(r.URL.Query().Get("session_id"))
	parentRunID := strings.TrimSpace(r.URL.Query().Get("parent_run_id"))
	if sessionID == "" && parentRunID == "" {
		BadRequest(w, "session_id or parent_run_id is required")
		return
	}
	if sessionID != "" && !h.sessionOwnedByCaller(r.Context(), sessionID, claims.UserID, claims.TenantID) {
		NotFound(w, "session not found")
		return
	}
	if sessionID == "" && parentRunID != "" {
		if _, _, ok := h.runAccessForCaller(r.Context(), tenant, claims.UserID, parentRunID); !ok {
			NotFound(w, "subagent run not found")
			return
		}
	}

	// 1) Redis 整树骨架（一次 HGETALL 拿到全部节点摘要）
	if sessionID != "" {
		if runs, err := h.treeFromRedis(r.Context(), tenant, sessionID); err == nil && len(runs) > 0 {
			OK(w, map[string]interface{}{"runs": runs, "source": "redis"})
			return
		}
	}
	// 2) Redis 子节点索引（懒加载场景）
	if parentRunID != "" {
		if runs, err := h.childrenFromRedis(r.Context(), tenant, parentRunID); err == nil && len(runs) > 0 {
			OK(w, map[string]interface{}{"runs": runs, "source": "redis"})
			return
		}
	}
	// 3) DB 回落（权威；历史 run 必然在这里）
	runs := h.runsFromDB(r.Context(), tenant, claims.UserID, sessionID, parentRunID)
	OK(w, map[string]interface{}{"runs": runs, "source": "db"})
}

// GetRun 返回单个 run 的状态/摘要/用量。
//
// H27：Redis 运行期缓存只按 tenant 分键（引擎侧契约），因此**先**用 DB 权威行
// 证明该 run 属于调用者，再决定是否下发缓存内容。此前只要同租户就能读，
// 同租户内的普通用户可以直接按 run_id 读到别人的子 Agent 输出。
func (h *SubagentHandler) GetRun(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenant := subagentTenant(claims)
	runID := strings.TrimSpace(r.PathValue("run_id"))
	if runID == "" {
		BadRequest(w, "run_id is required")
		return
	}
	if _, _, ok := h.runAccessForCaller(r.Context(), tenant, claims.UserID, runID); !ok {
		NotFound(w, "subagent run not found")
		return
	}
	if view, err := h.runFromRedis(r.Context(), tenant, runID); err == nil && view != nil {
		OK(w, map[string]interface{}{"run": view, "source": "redis"})
		return
	}
	view := h.runFromDB(r.Context(), tenant, claims.UserID, runID)
	if view == nil {
		NotFound(w, "subagent run not found")
		return
	}
	OK(w, map[string]interface{}{"run": view, "source": "db"})
}

// GetRunEvents 返回输出过程：Redis Stream（实时/近实时）优先，DB steps 回落（历史/审计）。
func (h *SubagentHandler) GetRunEvents(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, ErrAuthRequired)
		return
	}
	tenant := subagentTenant(claims)
	runID := strings.TrimSpace(r.PathValue("run_id"))
	if runID == "" {
		BadRequest(w, "run_id is required")
		return
	}
	// H27：events 是子 Agent 的完整输出（可能含私有上下文），先证明所有权。
	if _, _, ok := h.runAccessForCaller(r.Context(), tenant, claims.UserID, runID); !ok {
		NotFound(w, "subagent run not found")
		return
	}
	limit := subagentEventsLimit
	if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil && v > 0 && v <= subagentEventsLimit {
		limit = v
	}

	if events, err := h.eventsFromRedis(r.Context(), tenant, runID, limit); err == nil && len(events) > 0 {
		OK(w, map[string]interface{}{"events": events, "source": "redis"})
		return
	}
	OK(w, map[string]interface{}{"events": h.stepsFromDB(r.Context(), runID, limit), "source": "db"})
}

// ── Redis 读取 ──

func (h *SubagentHandler) treeFromRedis(ctx context.Context, tenant, sessionID string) ([]map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	raw, err := h.hashAll(ctx, subagentKey(tenant, "tree", sessionID))
	if err != nil {
		return nil, err
	}
	out := make([]map[string]interface{}, 0, len(raw))
	for _, value := range raw {
		var item map[string]interface{}
		if json.Unmarshal([]byte(value), &item) == nil && item["run_id"] != nil {
			out = append(out, item)
		}
	}
	return out, nil
}

func (h *SubagentHandler) childrenFromRedis(ctx context.Context, tenant, parentRunID string) ([]map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	res := h.rdb.Do(ctx, "SMEMBERS", subagentKey(tenant, "children", parentRunID))
	if res.Err() != nil {
		return nil, res.Err()
	}
	ids, _ := res.Val().([]interface{})
	out := make([]map[string]interface{}, 0, len(ids))
	for _, id := range ids {
		runID, _ := id.(string)
		if runID == "" {
			continue
		}
		if view, err := h.runFromRedis(ctx, tenant, runID); err == nil && view != nil {
			out = append(out, view)
		}
	}
	return out, nil
}

func (h *SubagentHandler) runFromRedis(ctx context.Context, tenant, runID string) (map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	// go-redis v9 的 HGETALL 返回 map（非 v8 的扁平数组）：统一走 db.HashAll，
	// 否则这里静默拿不到任何字段，运行期摘要会一直回落 DB。
	fields, err := db.HashAll(ctx, h.rdb, subagentKey(tenant, "run", runID))
	if err != nil {
		return nil, err
	}
	if len(fields) == 0 {
		return nil, nil
	}
	view := map[string]interface{}{"run_id": runID}
	for field, value := range fields {
		switch field {
		case "depth":
			if n, err := strconv.Atoi(value); err == nil {
				view["depth"] = n
			}
		case "usage":
			var usage map[string]interface{}
			if json.Unmarshal([]byte(value), &usage) == nil {
				view["usage"] = usage
			}
		case "run_id":
			// 保持 path 参数为准
		default:
			if value != "" {
				view[field] = value
			}
		}
	}
	return view, nil
}

func (h *SubagentHandler) eventsFromRedis(ctx context.Context, tenant, runID string, limit int) ([]map[string]interface{}, error) {
	if h.rdb == nil || tenant == "" {
		return nil, nil
	}
	// start="+" 表示倒序（取最近 limit 条），返回后再按时间正序，便于前端顺序渲染
	msgs, err := h.rdb.XRange(ctx, subagentKey(tenant, "ev", runID), "+", "-", int64(limit)).Result()
	if err != nil {
		return nil, err
	}
	out := make([]map[string]interface{}, 0, len(msgs))
	for i := len(msgs) - 1; i >= 0; i-- {
		payload, ok := msgs[i].Values["data"].(string)
		if !ok {
			continue
		}
		var event map[string]interface{}
		if json.Unmarshal([]byte(payload), &event) == nil {
			event["id"] = msgs[i].ID
			out = append(out, event)
		}
	}
	return out, nil
}

// hashAll 读取 Hash 全部字段（用 Do 以兼容 Cluster/单机两种实现）。
func (h *SubagentHandler) hashAll(ctx context.Context, key string) (map[string]string, error) {
	// go-redis v9 的 HGETALL 返回 map（非 v8 的扁平数组）：统一走 db.HashAll，
	// 否则静默得到空 map（子 Agent 的运行期摘要会一直回落 DB）。
	return db.HashAll(ctx, h.rdb, key)
}

// ── DB 回落 ──

func (h *SubagentHandler) runsFromDB(ctx context.Context, tenant, userID, sessionID, parentRunID string) []map[string]interface{} {
	out := []map[string]interface{}{}
	if tenant == "" || h.store == nil {
		return out
	}
	// H27：user_id 谓词与引擎侧 app/subagent/access.py 的 RUN_ACCESS_CLAUSE 同口径
	// （历史行 user_id 为空时保留同租户可见性）。少了它，同一租户内任何用户
	// 都能按 session/父节点列出别人的子 Agent 树。
	rows, err := h.store.FetchAll(ctx, `
		SELECT id, COALESCE(parent_run_id, '') AS parent_run_id, depth,
		       COALESCE(profile_name, '') AS profile_name, status,
		       COALESCE(summary, '') AS summary, input_tokens, output_tokens, steps,
		       redacted_count, COALESCE(error, '') AS error,
		       artifacts, write_paths, created_at
		  FROM subagent_runs
		 WHERE tenant_id = $1
		   AND (user_id = $2 OR user_id IS NULL)
		   AND ($3 = '' OR root_session_id = $3)
		   AND ($4 = '' OR parent_run_id = $4)
		 ORDER BY created_at DESC
		 LIMIT $5`, tenant, userID, sessionID, parentRunID, subagentListLimit)
	if err != nil {
		slog.Warn("subagent runs query failed", "tenant", tenant, "error", err)
		return out
	}
	for _, row := range rows {
		out = append(out, normalizeRunRow(row))
	}
	return out
}

func (h *SubagentHandler) runFromDB(ctx context.Context, tenant, userID, runID string) map[string]interface{} {
	if tenant == "" || h.store == nil {
		return nil
	}
	row, err := h.store.FetchOne(ctx, `
		SELECT id, COALESCE(parent_run_id, '') AS parent_run_id, depth,
		       COALESCE(profile_name, '') AS profile_name, status,
		       COALESCE(summary, '') AS summary, input_tokens, output_tokens, steps,
		       redacted_count, COALESCE(error, '') AS error,
		       artifacts, write_paths, created_at
		  FROM subagent_runs
		 WHERE id = $1 AND tenant_id = $2 AND (user_id = $3 OR user_id IS NULL)`,
		runID, tenant, userID)
	if err != nil || row == nil {
		return nil
	}
	return normalizeRunRow(row)
}

// runAccessForCaller 回答"这个 run 我能碰吗"，返回状态与根会话（H27）。
//
// 三条判据与引擎侧 access.py 逐条对应：tenant、user（历史空 user_id 保留可见）、
// run 存在性。未知与越权都走同一个 404（不泄露存在性）。root session 由调用方
// 在带 session 的入口（ListRuns/CancelSessionRuns）另行校验；run 维度的入口
// 没有可信 session 入参，就只用 run 自身的归属作答，而不是"没传就跳过"。
func (h *SubagentHandler) runAccessForCaller(
	ctx context.Context, tenant, userID, runID string,
) (status, rootSession string, ok bool) {
	if tenant == "" || userID == "" || runID == "" || h.store == nil {
		return "", "", false
	}
	row, err := h.store.FetchOne(ctx, `
		SELECT status, COALESCE(root_session_id, '') AS root_session_id
		  FROM subagent_runs
		 WHERE id = $1 AND tenant_id = $2 AND (user_id = $3 OR user_id IS NULL)`,
		runID, tenant, userID)
	if err != nil || row == nil {
		return "", "", false
	}
	status, _ = row["status"].(string)
	rootSession, _ = row["root_session_id"].(string)
	return status, rootSession, true
}

// sessionOwnedByCaller 判断会话是否属于该调用者（H27：user + tenant 双判据）。
// 只比 user_id 会让"同用户跨租户"（同一账号被迁租户后的历史 token）读到旧租户数据。
func (h *SubagentHandler) sessionOwnedByCaller(ctx context.Context, sessionID, userID string, tenantID int16) bool {
	if sessionID == "" || userID == "" || h.store == nil {
		return false
	}
	row, err := h.store.FetchOne(ctx,
		`SELECT 1 AS ok FROM sessions
		  WHERE id = $1 AND user_id = $2 AND ($3 = 0 OR tenant_id = $3)`,
		sessionID, userID, tenantID)
	return err == nil && row != nil
}

func (h *SubagentHandler) stepsFromDB(ctx context.Context, runID string, limit int) []map[string]interface{} {
	out := []map[string]interface{}{}
	if h.store == nil {
		return out
	}
	rows, err := h.store.FetchAll(ctx, `
		SELECT seq, kind, COALESCE(role, '') AS role, COALESCE(tool_name, '') AS tool_name,
		       COALESCE(tool_call_id, '') AS tool_call_id, COALESCE(content, '') AS content,
		       truncated, input_tokens, output_tokens
		  FROM subagent_run_steps
		 WHERE run_id = $1
		 ORDER BY seq
		 LIMIT $2`, runID, limit)
	if err != nil {
		slog.Warn("subagent steps query failed", "run_id", runID, "error", err)
		return out
	}
	for _, row := range rows {
		content, _ := row["content"].(string)
		truncated, _ := row["truncated"].(bool)
		if len(content) > subagentContentChars {
			content = content[:subagentContentChars]
			truncated = true
		}
		row["content"] = content
		row["truncated"] = truncated
		// DB 的列叫 kind，前端契约却是 SSE 的事件名 type —— 不映射的话，历史回放
		// （Redis Stream 过了 TTL 只剩 DB）一条都命中不到渲染分支，输出页恒空白。
		kind := stringOf(row["kind"])
		row["type"] = stepKindToEventType(kind)
		row["run_id"] = runID
		if kind == "tool_call" {
			// 落库时 `tool_call` 的 content 存的是**参数**（见 python-engine 的
			// subagent_runner 落库点）。前端契约里参数叫 `tool_arguments` —— 不改名的话
			// 回放出来的调用卡只有工具名、没有参数，和实时那条长得不一样。
			row["tool_arguments"] = stringOf(row["content"])
			row["content"] = ""
			row["status"] = "tool"
		}
		if kind == "tool_result" {
			// 结果的正文留在 content；工具名照旧挂着，便于前端把结果块挂到对应调用卡下。
			row["status"] = "tool"
		}
		out = append(out, row)
	}
	return out
}

// stepKindToEventType 把 subagent_run_steps.kind 映射成前端契约里的事件名（subagent.*）。
// 未知 kind 一律当 notice —— 前端对 notice 会渲染 content，不会把内容丢掉。
func stepKindToEventType(kind string) string {
	switch kind {
	case "message":
		return "subagent.text"
	case "tool_call":
		return "subagent.tool_call"
	case "tool_result":
		return "subagent.tool_result"
	case "approval":
		// 审批必须有独立事件名：回放时前端要能重建**可点击**的审批卡片，
		// 归到 notice 就只剩一行文字，用户看得到却批不了（等于没修）。
		return "subagent.approval"
	case "ask":
		// 提问同理：回放要能还原成**可回答**的卡片（前端复用 AskCard）
		return "subagent.ask"
	default:
		return "subagent.notice"
	}
}

// runHitBudgetWall 判定这条 run 是否**撞上了预算墙**（面板据此高亮「预算超限」）。
//
// 依据只能是引擎写下的**权威证据**，不能靠前端猜一个上限去比较 usage ——
// 上限由部署环境的 `SUBAGENT_MAX_*` 决定，网关与前端都看不到；按一个写死的默认值
// 比较，在高上限部署里会误报、在低上限部署里会漏报，两种都让标记变成噪声。
//
// 两条证据（见 python-engine/app/agent/task_budget.py 与 subagent_runner 的收尾）：
//
//	status=partial          有产出但不完整，典型就是撞上 wall/token 上限；
//	error=budget_exceeded:* 预算越界（`<轴>` = tokens / steps / wall / cost）。
func runHitBudgetWall(status, errText string) bool {
	if strings.EqualFold(strings.TrimSpace(status), "partial") {
		return true
	}
	return strings.HasPrefix(strings.TrimSpace(errText), "budget_exceeded")
}

// normalizeRunRow 统一 Redis/DB 两条来源的字段名与类型（前端只认这一份契约）。
func normalizeRunRow(row map[string]interface{}) map[string]interface{} {
	view := map[string]interface{}{"source": "db"}
	view["run_id"] = stringOf(row["id"])
	view["status"] = stringOf(row["status"])
	// 字段名必须与 Redis 那条来源**逐字一致**（前端只认一份契约）：DB 列叫 profile_name，
	// 输出的键必须是 profile —— 否则 DB 回落时前端拿不到 profile，树行退化成 run_id 显示。
	if v := stringOf(row["profile_name"]); v != "" {
		view["profile"] = v
	}
	for _, field := range []string{"parent_run_id", "summary", "error"} {
		if v := stringOf(row[field]); v != "" {
			view[field] = v
		}
	}
	view["depth"] = intOf(row["depth"])
	view["usage"] = map[string]interface{}{
		"input_tokens":  intOf(row["input_tokens"]),
		"output_tokens": intOf(row["output_tokens"]),
		"steps":         intOf(row["steps"]),
	}
	if n := intOf(row["redacted_count"]); n > 0 {
		view["redacted_count"] = n
	}
	if ts := stringOf(row["created_at"]); ts != "" {
		view["created_at"] = ts
	}
	// 产物：`artifacts` / `write_paths` 都是 jsonb，**原样透传为字符串**交给前端解析 ——
	// 后端不对形状做假设，避免与引擎侧的字段演进耦合（这里只保证数据能到前端）。
	if v := stringOf(row["artifacts"]); v != "" && v != "[]" {
		view["artifacts"] = v
	}
	if v := stringOf(row["write_paths"]); v != "" && v != "[]" {
		view["write_paths"] = v
	}
	// 预算超限标记：面板据此把卡片高亮。Redis 来源没有 error 字段时，
	// status=partial 这一条仍然成立（见 runHitBudgetWall 的说明）。
	if runHitBudgetWall(view["status"].(string), stringOf(row["error"])) {
		view["budget_exceeded"] = true
	}
	return view
}

func stringOf(v interface{}) string {
	switch value := v.(type) {
	case string:
		return value
	case []byte:
		// jsonb 列经驱动返回的可能是 []byte。若落进 default 分支，会被格式化成
		// "[91 123 93]" 这种字节序列 —— 前端拿到就无从 parse 了。
		return string(value)
	case nil:
		return ""
	case time.Time:
		return value.UTC().Format(time.RFC3339)
	default:
		return fmt.Sprintf("%v", value)
	}
}

func intOf(v interface{}) int {
	switch value := v.(type) {
	case int:
		return value
	// `int16` = Postgres `smallint`。**不能省**：`FetchOne`/`FetchAll` 把行扫进
	// `*interface{}`，pgx 走 `DecodeValue`，int2 给的就是 int16（int4→int32、
	// int8→int64）。漏掉它时唯一出路是 default 分支的 0 —— 而 `tenant_id` 正是
	// smallint：落库时租户被写成 0，随后按真实租户查就一行也查不到。
	// 实测症状：检查点上报"成功"，但时间线永远是空的。
	case int8:
		return int(value)
	case int16:
		return int(value)
	case int32:
		return int(value)
	case int64:
		return int(value)
	case uint16:
		return int(value)
	case float64:
		return int(value)
	// `pgtype.Numeric` = Postgres `numeric`。与上面的 int16 是同一类"驱动形态没被认"
	// 的缺陷，只是触发条件更隐蔽：**`SUM(bigint)` / `AVG(bigint)` 在 PG 里返回
	// numeric**（`SUM(int4)` 才返回 bigint）。`usage_events.input_tokens` 是 bigint，
	// 于是计费页的 `SUM(input_tokens)` 解出来是 numeric，落进 default 返回 0 ——
	// 用户实测形态：`usage_events` 有 30 行、token 真实非零，而 `/v1/billing/usage`
	// 的 daily/total 全是 0，**只有 `count(*)`（int8）是对的**。
	//
	// 修法有两道：SQL 侧统一 `COALESCE(SUM(x),0)::bigint`（首选，类型明确），
	// 这里再兜一层 —— 任何人新写一条 SUM 忘了 cast，也不会静默变成 0。
	case pgtype.Numeric:
		return numericToInt(value)
	case *pgtype.Numeric:
		if value == nil {
			return 0
		}
		return numericToInt(*value)
	case string:
		if n, err := strconv.Atoi(value); err == nil {
			return n
		}
		// numeric 也可能以十进制串的形态出现（"43620.0000"）：Atoi 会失败，
		// 但这正是最需要救回来的那种值。
		if f, err := strconv.ParseFloat(strings.TrimSpace(value), 64); err == nil {
			return int(math.Round(f))
		}
	}
	return 0
}

// numericToInt 把 PG `numeric` 转成 int（截断前四舍五入）。
//
// 走 `Int64Value()` 而不是 `Float64Value()`：金额/计数的精度在 int64 范围内，
// 绕一趟 float64 会在超大值上丢位；`Int64Value` 内部用 big.Int 精确换算，
// 只在超出 int64 时才报错（那时返回 0 也胜过返回一个错得离谱的数）。
func numericToInt(n pgtype.Numeric) int {
	if !n.Valid {
		return 0
	}
	if v, err := n.Int64Value(); err == nil && v.Valid {
		return int(v.Int64)
	}
	if f, err := n.Float64Value(); err == nil && f.Valid {
		return int(math.Round(f.Float64))
	}
	return 0
}
