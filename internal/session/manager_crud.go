package session

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/model"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── Session CRUD ──────────────────────────────────────────────────────────

// GetSession retrieves a session by ID. Checks Redis first, falls back to PG.
func (m *Manager) GetSession(ctx context.Context, id string) (*model.Session, error) {
	if id == "" {
		return nil, fmt.Errorf("session id is required")
	}

	// 1. Redis hot path
	if m.rdb != nil {
		data, err := m.rdb.Get(ctx, redisKeyPrefix+id).Bytes()
		if err == nil {
			var entry sessionCacheEntry
			if json.Unmarshal(data, &entry) == nil && entry.D != nil {
				return entry.D, nil
			}
			// 旧格式（升级前的裸 Session JSON）或损坏条目：删除后回退 PG 自愈
			m.rdb.Del(ctx, redisKeyPrefix+id)
		}
	}

	// 2. PG cold path
	if m.pool == nil {
		return nil, fmt.Errorf("database not available")
	}

	var s model.Session
	err := m.pool.QueryRow(ctx,
		`SELECT id, COALESCE(user_id::text, ''), COALESCE(title, ''), COALESCE(pinned, false), COALESCE(tag, ''), COALESCE(alias, ''), COALESCE(parent_session_id::text, ''), COALESCE(branch_from_seq, 0), created_at, updated_at
		 FROM sessions WHERE id = $1`, id).
		Scan(&s.ID, &s.UserID, &s.Title, &s.Pinned, &s.Tag, &s.Alias, &s.ParentSessionID, &s.BranchFromSeq, &s.CreatedAt, &s.UpdatedAt)
	if err == pgx.ErrNoRows {
		return nil, fmt.Errorf("%w: %s", ErrSessionNotFound, id)
	} else if err != nil {
		// 非法 uuid 格式（如前端旧版 fallback id "session_xxx"）：会话必然不存在，
		// 按 not found 处理（否则 SSE 端点会 500、前端触发"连接已断开"）
		var pgErr *pgconn.PgError
		if errors.As(err, &pgErr) && pgErr.Code == "22P02" {
			return nil, fmt.Errorf("%w: %s", ErrSessionNotFound, id)
		}
		return nil, fmt.Errorf("query session: %w", err)
	}

	// Warm Redis cache
	m.cacheSession(ctx, &s)
	return &s, nil
}

// DefaultTenantID is the default tenant for single-tenant deployments.
// 单一来源见 internal/db/seed.go。
const DefaultTenantID = db.DefaultTenantID

// CreateSession inserts a new session into PG and caches in Redis.
// If id is empty, returns an error.
func (m *Manager) CreateSession(ctx context.Context, id, userID, title string) (*model.Session, error) {
	if id == "" {
		return nil, fmt.Errorf("session id is required")
	}

	if title == "" {
		title = "New Chat"
	}

	now := time.Now()
	s := &model.Session{
		ID:        id,
		UserID:    userID,
		Title:     title,
		CreatedAt: now,
		UpdatedAt: now,
	}

	var uid *string
	if userID != "" {
		uid = &userID
	}

	if m.pool != nil {
		// 新会话：id 由调用方生成（UUID），撞车几乎不可能。仍带归属条件并检查影响行数 ——
		// 否则"撞上别人的 id"会静默覆盖对方标题（NULL 归属用 IS NOT DISTINCT FROM 匹配）。
		tag, err := m.pool.Exec(ctx,
			`INSERT INTO sessions (id, tenant_id, user_id, title, created_at, updated_at)
			 VALUES ($1, $2, $3::uuid, $4, $5, $6)
			 ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, updated_at = EXCLUDED.updated_at
			  WHERE sessions.user_id IS NOT DISTINCT FROM EXCLUDED.user_id`,
			id, DefaultTenantID, uid, title, now, now)
		if err != nil {
			return nil, fmt.Errorf("create session: %w", err)
		}
		if tag.RowsAffected() == 0 {
			return nil, fmt.Errorf("create session: %w: %s", ErrSessionForbidden, id)
		}
	}

	m.cacheSession(ctx, s)
	return s, nil
}

// ListSessions returns sessions for a given user, newest first.
// page 从 1 开始，perPage 默认为 100（最大 200）。
func (m *Manager) ListSessions(ctx context.Context, userID string, page, perPage int) ([]model.Session, error) {
	if m.pool == nil {
		return nil, nil
	}
	if page < 1 {
		page = 1
	}
	if perPage < 1 || perPage > 200 {
		perPage = 100
	}
	offset := (page - 1) * perPage

	rows, err := m.pool.Query(ctx,
		`SELECT id, COALESCE(user_id::text, ''), COALESCE(title, ''), COALESCE(pinned, false), COALESCE(tag, ''), COALESCE(alias, ''), COALESCE(parent_session_id::text, ''), COALESCE(branch_from_seq, 0), created_at, updated_at
		 FROM sessions
		 WHERE user_id = $1
		 ORDER BY pinned DESC, updated_at DESC
		 LIMIT $2 OFFSET $3`, userID, perPage, offset)
	if err != nil {
		return nil, fmt.Errorf("list sessions: %w", err)
	}
	defer rows.Close()

	var sessions []model.Session
	for rows.Next() {
		var s model.Session
		if err := rows.Scan(&s.ID, &s.UserID, &s.Title, &s.Pinned, &s.Tag, &s.Alias, &s.ParentSessionID, &s.BranchFromSeq, &s.CreatedAt, &s.UpdatedAt); err != nil {
			slog.Warn("scan session row", "error", err)
			continue
		}
		sessions = append(sessions, s)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("iterate sessions: %w", err)
	}

	if sessions == nil {
		sessions = []model.Session{}
	}
	return sessions, nil
}

// deletedSessionTTL 是「已删除会话」墓碑的存活时间。
//
// 必须覆盖"正在跑的回合结束后回写消息"的时间窗：删除接口返回后，后台 run 可能
// 还有一轮没走完；它收尾时走 `ensureSessionOwnedWithTitle` 的 upsert，会把刚删掉的
// sessions 行**重新建出来**（表现为"删掉的会话又回来了"）。墓碑让那条写入被拒绝。
// 24h 远大于任何单轮上限，也不会永久占用 key 空间。
const deletedSessionTTL = 24 * time.Hour

// deletedSessionPrefix 与 `session:` 同源（REDIS_KEY_PREFIX 由 db.RedisKey 注入）。
var deletedSessionPrefix = db.RedisKey("session:deleted:")

func (m *Manager) sessionDeleted(ctx context.Context, id string) bool {
	if m.rdb == nil || id == "" {
		return false
	}
	// 短超时 + fail-open：墓碑是防复活的增强，Redis 抖动时不该把正常的消息写入挡掉。
	wctx, cancel := context.WithTimeout(ctx, 500*time.Millisecond)
	defer cancel()
	n, err := m.rdb.Exists(wctx, deletedSessionPrefix+id).Result()
	return err == nil && n > 0
}

func (m *Manager) markSessionDeleted(ctx context.Context, id string) bool {
	if m.rdb == nil || id == "" {
		return false
	}
	wctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
	defer cancel()
	if err := m.rdb.Set(wctx, deletedSessionPrefix+id, "1", deletedSessionTTL).Err(); err != nil {
		slog.Warn("session deletion tombstone not written", "session", id, "error", err)
		return false
	}
	return true
}

func (m *Manager) unmarkSessionDeleted(ctx context.Context, id string) {
	if m.rdb == nil || id == "" {
		return
	}
	wctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*time.Second)
	defer cancel()
	_ = m.rdb.Del(wctx, deletedSessionPrefix+id).Err()
}

// DeleteSession removes a session from PG and Redis cache.
// Evict cache first so a subsequent GetSession falls through to PG (source of truth)
// even if Redis eviction fails silently.
//
// 依赖表清理：messages / turns / tool_calls 均以 session_id 外键引用 sessions，
// 且基线未配置 ON DELETE CASCADE，直接 `DELETE FROM sessions` 会触发外键违反
// → 接口 500 "服务器错误"。故在同一事务内按依赖顺序清理：先删引用行，再删会话本身。
//
// 运行期数据（agent_runs / subagent_runs / pending_interactions / project_checkpoints）
// 只有 `session_id` 列、**没有**外键：不显式删就会留下孤儿行，reconciler 还会把
// checkpointed 的 run 自动续跑 —— 用户看到的是"会话删了，后台还在干活"。
// 表可能尚未迁移到基线（老库），所以先查 `to_regclass` 再决定要不要拼这条 DELETE。
//
// usage_events 不做处理：它是只读观测数据，保留会话 id 便于事后归因。
func (m *Manager) DeleteSession(ctx context.Context, id string) error {
	if id == "" {
		return fmt.Errorf("session id is required")
	}

	// 墓碑先于删除落地：删除提交与"在途回合回写消息"之间没有锁，
	// 只有墓碑能保证回写被拒绝。删除失败时再撤掉，避免把仍存在的会话锁死。
	marked := m.markSessionDeleted(ctx, id)
	success := false
	defer func() {
		if marked && !success {
			m.unmarkSessionDeleted(ctx, id)
		}
	}()

	m.evictCache(ctx, id)

	if m.pool == nil {
		success = true
		return nil
	}

	tx, err := m.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("delete session: begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	extras := m.sessionRuntimeTables(ctx)
	stmts := []string{
		`DELETE FROM turns WHERE session_id = $1`,
		`DELETE FROM messages WHERE session_id = $1`,
		`DELETE FROM tool_calls WHERE session_id = $1`,
	}
	if extras["agent_runs"] {
		stmts = append(stmts, `DELETE FROM agent_runs WHERE session_id = $1`)
	}
	if extras["subagent_runs"] {
		stmts = append(stmts, `DELETE FROM subagent_runs WHERE root_session_id = $1`)
	}
	if extras["pending_interactions"] {
		stmts = append(stmts, `DELETE FROM pending_interactions WHERE session_id = $1`)
	}
	if extras["project_checkpoints"] {
		// checkpoint_untracked 通过外键级联删除，不必单独处理。
		stmts = append(stmts, `DELETE FROM project_checkpoints WHERE session_id = $1`)
	}
	stmts = append(stmts, `DELETE FROM sessions WHERE id = $1`)
	for _, q := range stmts {
		if _, err := tx.Exec(ctx, q, id); err != nil {
			return fmt.Errorf("delete session: %w", err)
		}
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("delete session: commit: %w", err)
	}
	success = true

	return nil
}

// sessionRuntimeTables 一次性判断四张运行期表是否存在。
//
// 为什么不直接 `DELETE` 再容忍 42P01：PG 中任一语句失败会让**整个事务**进入
// aborted 状态，后续 DELETE 全部报 "current transaction is aborted" ——
// 容错反而让删除整个失败。先查 `to_regclass` 能把"老库缺表"降级成跳过。
func (m *Manager) sessionRuntimeTables(ctx context.Context) map[string]bool {
	out := map[string]bool{}
	if m.pool == nil {
		return out
	}
	const q = `SELECT to_regclass('public.agent_runs') IS NOT NULL,
	                  to_regclass('public.subagent_runs') IS NOT NULL,
	                  to_regclass('public.pending_interactions') IS NOT NULL,
	                  to_regclass('public.project_checkpoints') IS NOT NULL`
	var agentRuns, subagentRuns, pending, checkpoints bool
	if err := m.pool.QueryRow(ctx, q).Scan(&agentRuns, &subagentRuns, &pending, &checkpoints); err != nil {
		// 查不到（权限/连接）时保守按"都存在"处理：缺表会在删除时报出来，
		// 而漏删运行期数据会让后台任务继续跑 —— 后者更难被发现。
		return map[string]bool{
			"agent_runs": true, "subagent_runs": true,
			"pending_interactions": true, "project_checkpoints": true,
		}
	}
	out["agent_runs"] = agentRuns
	out["subagent_runs"] = subagentRuns
	out["pending_interactions"] = pending
	out["project_checkpoints"] = checkpoints
	return out
}

// SessionUpdate 描述一次会话属性更新：nil 表示不修改该字段。
// 单独抽出类型是为了让 SQL 组装（buildSessionUpdate）可脱离 DB 单测。
type SessionUpdate struct {
	Title  *string
	Pinned *bool
	Tag    *string
	Alias  *string
}

// empty 表示没有任何字段需要更新。
func (u SessionUpdate) empty() bool {
	return u.Title == nil && u.Pinned == nil && u.Tag == nil && u.Alias == nil
}

// buildSessionUpdate 组装 UPDATE sessions 的语句与参数。
// 只有 title 变更才推进 updated_at（置顶/标签/别名是列表偏好，不算会话活动）；
// tag / alias 传空串表示清除（写 NULL，避免留下空串）。
func buildSessionUpdate(id string, u SessionUpdate, now time.Time) (string, []interface{}) {
	var sets []string
	var args []interface{}
	if u.Title != nil {
		sets = append(sets, fmt.Sprintf("title = $%d", len(args)+1))
		args = append(args, *u.Title)
		sets = append(sets, fmt.Sprintf("updated_at = $%d", len(args)+1))
		args = append(args, now)
	}
	if u.Pinned != nil {
		sets = append(sets, fmt.Sprintf("pinned = $%d", len(args)+1))
		args = append(args, *u.Pinned)
	}
	if u.Tag != nil {
		sets = append(sets, fmt.Sprintf("tag = NULLIF($%d, '')", len(args)+1))
		args = append(args, *u.Tag)
	}
	if u.Alias != nil {
		sets = append(sets, fmt.Sprintf("alias = NULLIF($%d, '')", len(args)+1))
		args = append(args, *u.Alias)
	}
	args = append(args, id)
	return `UPDATE sessions SET ` + strings.Join(sets, ", ") + ` WHERE id = $` + strconv.Itoa(len(args)), args
}

// UpdateSession updates a session's title / pinned flag / tag / alias, then refreshes
// the Redis cache so a subsequent GetSession returns the fresh row.
// At least one field must be non-nil.
func (m *Manager) UpdateSession(ctx context.Context, id string, upd SessionUpdate) (*model.Session, error) {
	if id == "" {
		return nil, fmt.Errorf("session id is required")
	}
	if upd.empty() {
		return nil, fmt.Errorf("nothing to update")
	}

	if m.pool != nil {
		query, args := buildSessionUpdate(id, upd, time.Now())
		if _, err := m.pool.Exec(ctx, query, args...); err != nil {
			return nil, fmt.Errorf("update session: %w", err)
		}
	}

	m.evictCache(ctx, id)
	return m.GetSession(ctx, id)
}
