package api

// notifications.go —— 站内通知（通知中心）的网关半边。
//
// ## 为什么这里只有"读"和"标已读"

// 通知的**产生**不经过 HTTP：它来自系统自己看到的事件（子 Agent 运行失败、分享被
// 管理员撤销）。让客户端 POST 造通知，只会造出一个没人写的公开入口 —— 谁都能给
// 自己灌一条"支付成功"，那正是 SaaS 分支残留文案给人的错觉。所以：
//
//	GET  /v1/notifications          我的通知（?unread=1&limit=）
//	GET  /v1/notifications/unread   未读数（角标）
//	POST /v1/notifications/read     标已读（{ids:[...]} 或 {all:true}）
//
// 写入是包内函数 `insertNotification`，由 `subagent_events_relay.go`（子 Agent 终态）
// 与 `admin_shares.go`（管理员撤销分享）调用。
//
// ## 身份只来自 claims
//
// 三个端点都按 `claims.UserID` + `claims.TenantID` 过滤，query/body 里的任何 user_id
// 一律忽略。通知正文含会话标题与子任务失败原因，按 id 猜测别人的通知是真实风险
// （id 是 uuid，但不必赌随机性 —— 谓词本来就该写）。
//
// ## 未读数为什么单独一条端点
//
// 角标要轮询（用户通道的 SSE 只是加速）。把 `unread_count` 塞进列表响应会让空列表
// 也要拉全量行；单独一条 `COUNT(*)` 走部分索引 `notifications_user_unread_idx`，
// 在历史通知增长时仍是常数级。

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5/pgconn"
)

// 通知种类。与前端 `locales/*/notifications.ts` 的 `kind` 一一对应 —— 新增一种
// 必须同时加三份文案，否则界面会显示原始 key（服务端只存 kind + payload）。
const (
	// NotifSubagentFailed 子 Agent 以 failed / partial 收尾。
	NotifSubagentFailed = "subagent_failed"
	// NotifShareRevoked 管理员撤销了某个会话的分享链接。
	NotifShareRevoked = "share_revoked"
)

// notificationKinds 是合法取值集合（供测试与将来做筛选白名单）。
var notificationKinds = map[string]bool{
	NotifSubagentFailed: true,
	NotifShareRevoked:   true,
}

// NotificationItem 是列表里的一行。**不含 tenant_id** —— 它只是内部隔离维度。
type NotificationItem struct {
	ID        string                 `json:"id"`
	Kind      string                 `json:"kind"`
	Payload   map[string]interface{} `json:"payload"`
	SessionID string                 `json:"session_id,omitempty"`
	Read      bool                   `json:"read"`
	ReadAt    *time.Time             `json:"read_at,omitempty"`
	CreatedAt time.Time              `json:"created_at"`
}

// insertNotification 落一条通知。
//
// `dedupeKey` 非空时走 `ON CONFLICT DO NOTHING`：写入口之一是 Redis pub/sub 中继，
// 而**每个网关副本**都订阅同一条通道 ⇒ N 个副本同时看到同一次子 Agent 失败。
// 去重键让其中一个副本成功、其余静默跳过（见迁移 0002 的 `ux_notifications_dedupe`）。
//
// 所有失败都只记日志、不返回给调用方：通知是**旁路**。一条通知写不进去，
// 不该让"子 Agent 已经失败"这件事再失败一次，也不该让"分享已撤销"这个动作回滚。
func insertNotification(ctx context.Context, n NotificationItem, tenantID int16, userID, dedupeKey string) {
	if db.GlobalDBManager == nil || userID == "" || tenantID == 0 || n.Kind == "" {
		return
	}
	payloadJSON, err := json.Marshal(n.Payload)
	if err != nil {
		slog.Warn("notification payload marshal failed", "kind", n.Kind, "error", err)
		return
	}
	if n.ID == "" {
		n.ID = newUUID()
	}
	_, err = db.GlobalDBManager.Exec(ctx,
		`INSERT INTO notifications (id, tenant_id, user_id, kind, payload, session_id, dedupe_key)
		 VALUES ($1, $2, $3, $4, $5::jsonb, NULLIF($6, ''), NULLIF($7, ''))
		 ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL DO NOTHING`,
		n.ID, tenantID, userID, n.Kind, string(payloadJSON), n.SessionID, dedupeKey)
	if err != nil {
		slog.Warn("notification insert failed", "kind", n.Kind, "user", userID, "error", err)
	}
}

// notificationIdentity 取"我是谁"。与 `interactionIdentity` 同口径，但返回数值租户
// （notice 表的 tenant_id 是 smallint，与 pending_interactions 一致）。
func notificationIdentity(w http.ResponseWriter, r *http.Request) (string, int16, bool) {
	claims := auth.GetClaims(r.Context())
	if claims == nil || claims.UserID == "" {
		Unauthorized(w, ErrAuthRequired)
		return "", 0, false
	}
	if claims.TenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return "", 0, false
	}
	return claims.UserID, claims.TenantID, true
}

// notificationLimit 解析并夹紧 limit。
//
// 越界回落到默认而不是报错：这只是显示条数，为它返回 400 会让一个无关的手滑
// 把整页打成错误态。上限 200 与 `GET /v1/shares` 同量级。
func notificationLimit(r *http.Request) int {
	const def, max = 50, 200
	raw := strings.TrimSpace(r.URL.Query().Get("limit"))
	if raw == "" {
		return def
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return def
	}
	if n > max {
		return max
	}
	return n
}

// List GET /v1/notifications
//
// `?unread=1` 只回未读。响应同时带 `unread_count`：页面切换筛选时角标不必再拉一次，
// 而它本来就是同一次扫描里顺手能拿到的事实。
func (h *NotificationHandler) List(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}

	unreadOnly := r.URL.Query().Get("unread") == "1" || r.URL.Query().Get("unread") == "true"
	limit := notificationLimit(r)

	query := `SELECT id, kind, payload, COALESCE(session_id, ''), read_at, created_at
		 FROM notifications
		 WHERE user_id = $1 AND tenant_id = $2`
	if unreadOnly {
		query += ` AND read_at IS NULL`
	}
	query += ` ORDER BY created_at DESC LIMIT $3`

	rows, err := db.GlobalDBManager.Query(r.Context(), query, userID, tenantID, limit)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list notifications failed")
		return
	}
	defer rows.Close()

	items := make([]NotificationItem, 0, 16)
	for rows.Next() {
		var (
			it        NotificationItem
			rawJSON   []byte
			readAt    *time.Time
			createdAt time.Time
		)
		if err := rows.Scan(&it.ID, &it.Kind, &rawJSON, &it.SessionID, &readAt, &createdAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan notification failed")
			return
		}
		it.ReadAt = readAt
		it.Read = readAt != nil
		it.CreatedAt = createdAt
		// payload 读不出来不该让整页失败：给空对象，前端模板会用零值渲染。
		if len(rawJSON) > 0 {
			_ = json.Unmarshal(rawJSON, &it.Payload)
		}
		if it.Payload == nil {
			it.Payload = map[string]interface{}{}
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate notifications failed")
		return
	}

	count, err := unreadCount(r.Context(), userID, tenantID)
	if err != nil {
		// 计数失败不影响列表本身（角标是增强，不是页面可用的条件）。
		slog.Warn("notification unread count failed", "user", userID, "error", err)
		count = 0
	}
	OK(w, map[string]interface{}{
		"items":        items,
		"unread_count": count,
	})
}

// UnreadCount GET /v1/notifications/unread
func (h *NotificationHandler) UnreadCount(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}
	count, err := unreadCount(r.Context(), userID, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "count unread notifications failed")
		return
	}
	OK(w, map[string]interface{}{"unread_count": count})
}

func unreadCount(ctx context.Context, userID string, tenantID int16) (int, error) {
	var count int
	err := db.GlobalDBManager.QueryRow(ctx,
		`SELECT COUNT(*) FROM notifications
		 WHERE user_id = $1 AND tenant_id = $2 AND read_at IS NULL`,
		userID, tenantID).Scan(&count)
	return count, err
}

// MarkRead POST /v1/notifications/read
//
// 两种形态：`{"ids": ["..."]}` 指定几条，或 `{"all": true}` 一把清空。
// 幂等 —— 已读的再标一次仍返回成功（用户的意图是"这些不要再亮了"，
// 而不是"我要看到状态从 0 变 1"）。
//
// 返回 `updated` 条数：界面用它决定要不要弹"已标为 N 条已读"。
func (h *NotificationHandler) MarkRead(w http.ResponseWriter, r *http.Request) {
	userID, tenantID, ok := notificationIdentity(w, r)
	if !ok {
		return
	}
	if db.GlobalDBManager == nil {
		InternalError(w, ErrDBUnavailable)
		return
	}

	var body struct {
		IDs []string `json:"ids"`
		All bool     `json:"all"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if !body.All && len(body.IDs) == 0 {
		BadRequest(w, "ids or all is required")
		return
	}
	// 上限与列表一致：客户端手滑传一万个 id 时不该拼出一条巨型语句。
	if len(body.IDs) > 500 {
		body.IDs = body.IDs[:500]
	}

	var (
		tag pgconn.CommandTag
		err error
	)
	if body.All {
		tag, err = db.GlobalDBManager.Exec(r.Context(),
			`UPDATE notifications SET read_at = NOW()
			 WHERE user_id = $1 AND tenant_id = $2 AND read_at IS NULL`,
			userID, tenantID)
	} else {
		tag, err = db.GlobalDBManager.Exec(r.Context(),
			`UPDATE notifications SET read_at = NOW()
			 WHERE user_id = $1 AND tenant_id = $2 AND read_at IS NULL
			   AND id = ANY($3::text[])`,
			userID, tenantID, body.IDs)
	}
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "mark notifications read failed")
		return
	}
	OK(w, map[string]interface{}{"updated": tag.RowsAffected()})
}

// NotificationHandler 三个端点的宿主。没有状态：身份取自请求，存储是全局 DB 管理器。
type NotificationHandler struct{}

// NewNotificationHandler 与其它 handler 的构造形式保持一致（便于将来注入依赖）。
func NewNotificationHandler() *NotificationHandler { return &NotificationHandler{} }

// RegisterRoutes 注册通知端点。
func (h *NotificationHandler) RegisterRoutes(mux *http.ServeMux, authMW, rlMW routeMiddleware) {
	mux.Handle("GET /v1/notifications", authMW(rlMW(http.HandlerFunc(h.List))))
	mux.Handle("GET /v1/notifications/unread", authMW(rlMW(http.HandlerFunc(h.UnreadCount))))
	mux.Handle("POST /v1/notifications/read", authMW(rlMW(http.HandlerFunc(h.MarkRead))))
}
