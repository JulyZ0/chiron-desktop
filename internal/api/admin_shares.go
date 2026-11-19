package api

import (
	"errors"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// admin_shares.go —— 后台「分享管理」的两个端点。
//
//	GET    /v1/admin/shares            列出本租户内全部有效分享（跨用户）
//	DELETE /v1/admin/shares/{id}       按分享 id 直接撤销（不必知道会话 id）
//
// ## 为什么不能在用户态接口上凑
//
// 用户态只有 `GET /v1/shares`（**按 user_id 过滤**）与
// `DELETE /v1/conversations/{id}/share`（按会话过滤且校验归属）。
// 管理员的诉求正相反：**看到别人分享出去了什么**，并在必要时直接掐断
// —— 这是运营处置动作（泄露了内部会话、被举报等），不是"我自己的分享清单"。
// 所以单开两个租户级端点，权限走平台/租户 admin 读与写。
//
// ## 只列有效分享
//
// 已撤销与已过期的链接点开就是 410，它们在管理员视角里同样没有处置价值
// （留着只会让列表越长越难扫）。需要审计历史时查 conversation_shares 全表。

// AdminShareItem 是后台分享列表的一行。
type AdminShareItem struct {
	ShareID      string    `json:"share_id"`
	SessionID    string    `json:"session_id"`
	UserID       string    `json:"user_id"`
	UserEmail    string    `json:"user_email"`
	Title        string    `json:"title"`
	MessageCount int       `json:"message_count"`
	CreatedAt    time.Time `json:"created_at"`
	ExpiresAt    time.Time `json:"expires_at"`
}

// ListShares 列出本租户内的有效分享（GET /v1/admin/shares）。
//
// 限定租户：分享行本身带用户 id，用户带 tenant_id —— 不按租户过滤会让
// 一个租户的 admin 看见另一个租户的会话标题，那是跨租户泄露。
func (h *AdminHandler) ListShares(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT s.id, COALESCE(s.session_id, ''), COALESCE(s.user_id, ''),
		        COALESCE(u.email, ''), COALESCE(s.title, ''),
		        COALESCE(array_length(s.message_ids, 1), 0),
		        s.created_at, s.expires_at
		 FROM conversation_shares s
		 LEFT JOIN users u ON u.id = s.user_id
		 WHERE s.revoked_at IS NULL AND s.expires_at > NOW()
		   AND (u.tenant_id = $1 OR (u.id IS NULL AND $1 = 1))
		 ORDER BY s.created_at DESC
		 LIMIT 500`, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list admin shares failed")
		return
	}
	defer rows.Close()

	items := make([]AdminShareItem, 0, 32)
	for rows.Next() {
		var it AdminShareItem
		if err := rows.Scan(&it.ShareID, &it.SessionID, &it.UserID, &it.UserEmail,
			&it.Title, &it.MessageCount, &it.CreatedAt, &it.ExpiresAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan admin share failed")
			return
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate admin shares failed")
		return
	}
	OK(w, map[string]interface{}{"items": items})
}

// RevokeShare 按分享 id 撤销（DELETE /v1/admin/shares/{id}）。
//
// 幂等：已撤销的再撤一次也返回成功 —— 管理员的意图是"这条链接必须不可用"，
// 而不是"我要看到状态从未撤销变成已撤销"。
func (h *AdminHandler) RevokeShare(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	shareID := r.PathValue("id")
	if shareID == "" {
		BadRequest(w, "id is required")
		return
	}

	// 租户校验写在 UPDATE 的条件里：先查再改会留下"查完到改之前被别的请求改掉"
	// 的窗口，而且多一次往返。
	//
	// `RETURNING` 一并取回所有者与标题：撤销之后要给**该所有者**写一条站内通知
	// （"你的分享被管理员撤了"）。放在同一条语句里而不是先 SELECT 再 UPDATE ——
	// 那样在"查完到改之前"若已被本人撤销，就会给一条其实没被我们撤掉的链接发通知。
	var (
		ownerID   string
		sessionID string
		title     string
	)
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`UPDATE conversation_shares s SET revoked_at = NOW()
		 WHERE s.id = $1 AND s.revoked_at IS NULL
		   AND EXISTS (
		     SELECT 1 FROM users u WHERE u.id = s.user_id AND u.tenant_id = $2
		   )
		 RETURNING COALESCE(s.user_id, ''), COALESCE(s.session_id, ''), COALESCE(s.title, '')`,
		shareID, tenantID).Scan(&ownerID, &sessionID, &title)
	if err != nil {
		if errors.Is(err, pgx.ErrNoRows) {
			// 三种情况合并成同一个响应：不存在 / 不属于本租户 / 已撤销。
			// 区分开等于把"这个 id 存在但不在你这儿"告诉调用方，是多余的信息泄露。
			NotFound(w, "share not found or already revoked")
			return
		}
		logAndRespond(w, err, http.StatusInternalServerError, "revoke admin share failed")
		return
	}

	// 通知所有者。旁路：写不进去不影响撤销结果（insertNotification 自己吞日志）。
	//
	// dedupeKey 留空：同一个会话可以被撤销多次（撤销后重新分享、再被撤销），
	// 每次都该有一条通知，去重键会把后面几次吃掉。
	if ownerID != "" {
		insertNotification(r.Context(), NotificationItem{
			Kind:      NotifShareRevoked,
			SessionID: sessionID,
			Payload: map[string]interface{}{
				"share_id": shareID,
				"title":    title,
			},
		}, tenantID, ownerID, "")
	}
	OK(w, map[string]string{"status": "revoked"})
}
