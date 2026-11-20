package api

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/jackc/pgx/v5"
)

// admin_shares.go —— 后台「分享管理」的三个端点。
//
//	GET    /v1/admin/shares            列出本租户内全部有效分享（跨用户）+ 访问统计
//	DELETE /v1/admin/shares/{id}       按分享 id 直接撤销（不必知道会话 id）
//	DELETE /v1/admin/shares?ids=a,b,c  批量撤销（最多 200 条，一次审计）
//
// ## 为什么不能在用户态接口上凑
//
// 用户态只有 `GET /v1/shares`（**按 user_id 过滤**）与
// `DELETE /v1/conversations/{id}/share`（按会话过滤且校验归属）。
// 管理员的诉求正相反：**看到别人分享出去了什么**，并在必要时直接掐断
// —— 这是运营处置动作（泄露了内部会话、被举报等），不是"我自己的分享清单"。
// 所以单开租户级端点，权限走平台/租户 admin 读与写。
//
// ## 只列有效分享
//
// 已撤销与已过期的链接点开就是 410，它们在管理员视角里同样没有处置价值
// （留着只会让列表越长越难扫）。需要审计历史时查 conversation_shares 全表。
//
// ## 访问统计从哪来
//
// 全部来自 `share_access_log`（迁移 0005）的聚合，**不另设计数器列**：计数器与日志
// 双写必然漂移，而日志本身就是"谁在什么时候看过"的唯一事实。列表用 LATERAL 做
// 逐条聚合（走 `(share_id, at DESC)` 索引），趋势用一次 grouped 查询补齐 —— 避免
// 500 行 × 7 天的 N+1。

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
	// URL 是绝对分享地址（PUBLIC_BASE_URL 或按请求推导）。桌面端页面在 file:// 下
	// 自己拼 origin 得到的是坏链，所以链接必须以服务端产出为准。
	URL string `json:"url"`
	// 访问统计：成功打开的次数（不含 404/410/密码错误）。
	Views24h     int64      `json:"views_24h"`
	ViewsTotal   int64      `json:"views_total"`
	LastViewedAt *time.Time `json:"last_viewed_at"`
	// Daily 是最近 7 天（含今天，UTC）每天一次计数，缺的日期补 0 —— 前端 sparkline
	// 直接按固定长度画，不必再关心空洞。
	Daily []AdminShareDay `json:"daily"`
}

// AdminShareDay 是趋势图的一个点。
type AdminShareDay struct {
	Date  string `json:"date"`
	Count int64  `json:"count"`
}

// shareQueryStore 是分享管理端点用到的数据库能力（`*db.DBManager` 天然满足）。
// 抽成接口只为单测能覆盖"部分失败"与聚合分支。
type shareQueryStore interface {
	Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error)
}

func (h *AdminHandler) sharesDB() shareQueryStore {
	if h.shares != nil {
		return h.shares
	}
	return db.GlobalDBManager
}

// adminShareDailyWindow 是趋势图的窗口（天）。7 天足够看出"昨天突然被转发"，
// 又不至于让每条分享多传一大串零。
const adminShareDailyWindow = 7

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

	rows, err := h.sharesDB().Query(r.Context(),
		`SELECT s.id, COALESCE(s.session_id, ''), COALESCE(s.user_id, ''),
		        COALESCE(u.email, ''), COALESCE(s.title, ''),
		        COALESCE(array_length(s.message_ids, 1), 0),
		        s.created_at, s.expires_at,
		        COALESCE(v.views_24h, 0), COALESCE(v.views_total, 0), v.last_viewed_at
		 FROM conversation_shares s
		 LEFT JOIN users u ON u.id = s.user_id
		 LEFT JOIN LATERAL (
		     SELECT count(*) AS views_total,
		            count(*) FILTER (WHERE l.at > NOW() - interval '24 hours') AS views_24h,
		            max(l.at) AS last_viewed_at
		     FROM share_access_log l
		     WHERE l.share_id = s.id
		 ) v ON TRUE
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
	ids := make([]string, 0, 32)
	for rows.Next() {
		var it AdminShareItem
		if err := rows.Scan(&it.ShareID, &it.SessionID, &it.UserID, &it.UserEmail,
			&it.Title, &it.MessageCount, &it.CreatedAt, &it.ExpiresAt,
			&it.Views24h, &it.ViewsTotal, &it.LastViewedAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan admin share failed")
			return
		}
		it.URL = sharePublicURL(h.cfg, r, it.ShareID)
		items = append(items, it)
		ids = append(ids, it.ShareID)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate admin shares failed")
		return
	}

	// 趋势：一次查询覆盖整页。失败只让趋势为空 —— 统计是增强信息，不该把整页打成错误态。
	if len(ids) > 0 {
		if daily, err := h.shareDailyTrend(r.Context(), ids); err != nil {
			slog.Warn("admin shares daily trend failed", "error", err)
		} else {
			now := time.Now().UTC()
			for i := range items {
				items[i].Daily = shareDailyBuckets(daily[items[i].ShareID], now, adminShareDailyWindow)
			}
		}
	}
	// 没有访问数据的行也要给固定长度的零序列（前端按长度画图，短数组会画歪）。
	now := time.Now().UTC()
	for i := range items {
		if items[i].Daily == nil {
			items[i].Daily = shareDailyBuckets(nil, now, adminShareDailyWindow)
		}
	}

	OK(w, map[string]interface{}{"items": items})
}

// shareDailyTrend 返回 share_id → ("YYYY-MM-DD" → 次数)，窗口为最近 7 天（含今天）。
func (h *AdminHandler) shareDailyTrend(ctx context.Context, ids []string) (map[string]map[string]int64, error) {
	rows, err := h.sharesDB().Query(ctx,
		`SELECT share_id, to_char((at AT TIME ZONE 'UTC')::date, 'YYYY-MM-DD'), count(*)
		 FROM share_access_log
		 WHERE share_id = ANY($1::text[])
		   AND at > NOW() - interval '7 days'
		 GROUP BY 1, 2`, ids)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make(map[string]map[string]int64, len(ids))
	for rows.Next() {
		var (
			shareID string
			day     string
			count   int64
		)
		if err := rows.Scan(&shareID, &day, &count); err != nil {
			return nil, err
		}
		if out[shareID] == nil {
			out[shareID] = make(map[string]int64, adminShareDailyWindow)
		}
		out[shareID][day] = count
	}
	return out, rows.Err()
}

// shareDailyBuckets 把稀疏的按天计数铺成**固定长度**的序列（缺的补 0），
// 日期用 UTC（与库侧 `at AT TIME ZONE 'UTC'` 同一口径）。
func shareDailyBuckets(counts map[string]int64, now time.Time, days int) []AdminShareDay {
	if days <= 0 {
		return []AdminShareDay{}
	}
	today := time.Date(now.Year(), now.Month(), now.Day(), 0, 0, 0, 0, time.UTC)
	out := make([]AdminShareDay, 0, days)
	for i := days - 1; i >= 0; i-- {
		day := today.AddDate(0, 0, -i).Format("2006-01-02")
		out = append(out, AdminShareDay{Date: day, Count: counts[day]})
	}
	return out
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

// adminShareBulkMax 是单次批量撤销的上限。
//
// 为什么要有上限：一条 UPDATE ... WHERE id = ANY(...) 的**锁范围**与参数大小成正比，
// 而管理后台的"全选"很容易把整页（500 行）全勾上。200 条一次既覆盖真实处置场景
// （一次泄露通常是同一批链接），又不会让一条语句长时间持有大量行锁。
const adminShareBulkMax = 200

// AdminShareBulkResult 是批量撤销的结果。
//
// 刻意**不**是"全成功/全失败"：一批里混有已被本人撤销或不属于本租户的 id 是常态，
// 调用方需要知道"哪些没撤成"才能决定是否手工复查，而不是拿到一个笼统的 500。
type AdminShareBulkResult struct {
	Requested int `json:"requested"`
	Revoked   int `json:"revoked"`
	// RevokedIDs 是真正被本次请求改成 revoked_at 的 id（幂等重放时为空）。
	RevokedIDs []string `json:"revoked_ids"`
	Failed     []struct {
		ID     string `json:"id"`
		Reason string `json:"reason"`
	} `json:"failed"`
}

// RevokeShares 批量撤销（DELETE /v1/admin/shares?ids=a,b,c）。
//
// 与单条撤销同一权限口径（platformWriteMW）与同一租户隔离谓词。审计只写**一条**
// 汇总记录：批量动作在审计里是一条"谁一次性关掉了 N 条链接"，展开成 N 条反而
// 埋掉了"这是一次批量处置"的事实。
func (h *AdminHandler) RevokeShares(w http.ResponseWriter, r *http.Request) {
	tenantID := GetTenantID(r)
	if tenantID == 0 {
		Unauthorized(w, "missing tenant context")
		return
	}
	ids := splitShareIDs(r.URL.Query().Get("ids"))
	if len(ids) == 0 {
		BadRequest(w, "ids is required")
		return
	}
	if len(ids) > adminShareBulkMax {
		BadRequest(w, "ids exceeds maximum of "+strconv.Itoa(adminShareBulkMax))
		return
	}

	rows, err := h.sharesDB().Query(r.Context(),
		`UPDATE conversation_shares s SET revoked_at = NOW()
		 WHERE s.id = ANY($1::text[]) AND s.revoked_at IS NULL
		   AND EXISTS (
		     SELECT 1 FROM users u WHERE u.id = s.user_id AND u.tenant_id = $2
		   )
		 RETURNING s.id, COALESCE(s.user_id, ''), COALESCE(s.session_id, ''), COALESCE(s.title, '')`,
		ids, tenantID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "bulk revoke admin shares failed")
		return
	}
	defer rows.Close()

	type revokedShare struct{ id, ownerID, sessionID, title string }
	revoked := make([]revokedShare, 0, len(ids))
	for rows.Next() {
		var rs revokedShare
		if err := rows.Scan(&rs.id, &rs.ownerID, &rs.sessionID, &rs.title); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan revoked admin share failed")
			return
		}
		revoked = append(revoked, rs)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate revoked admin shares failed")
		return
	}

	// 逐个通知所有者（与单条撤销同一语气）。旁路写入，不影响结果。
	for _, rs := range revoked {
		if rs.ownerID == "" {
			continue
		}
		insertNotification(r.Context(), NotificationItem{
			Kind:      NotifShareRevoked,
			SessionID: rs.sessionID,
			Payload: map[string]interface{}{
				"share_id": rs.id,
				"title":    rs.title,
			},
		}, tenantID, rs.ownerID, "")
	}

	result := AdminShareBulkResult{
		Requested:  len(ids),
		Revoked:    len(revoked),
		RevokedIDs: make([]string, 0, len(revoked)),
	}
	done := make(map[string]bool, len(revoked))
	for _, rs := range revoked {
		result.RevokedIDs = append(result.RevokedIDs, rs.id)
		done[rs.id] = true
	}
	result.Failed = make([]struct {
		ID     string `json:"id"`
		Reason string `json:"reason"`
	}, 0, len(ids)-len(revoked))
	for _, id := range ids {
		if done[id] {
			continue
		}
		// 原因合并成一句：不存在 / 不属于本租户 / 已被撤销。区分开等于泄露
		// "这个 id 存在但不在你这儿"（与单条撤销同一取舍）。
		result.Failed = append(result.Failed, struct {
			ID     string `json:"id"`
			Reason string `json:"reason"`
		}{ID: id, Reason: "not_found_or_already_revoked"})
	}

	// 一条汇总审计：谁、一次关掉多少条、其中多少条没成功。
	var auditorID string
	var auditorTenant int16
	if claims := auth.GetClaims(r.Context()); claims != nil {
		auditorID = claims.UserID
		auditorTenant = claims.TenantID
	}
	db.AuditLog(r.Context(), auditorID, auditorTenant, "admin.share.bulk_revoke", "conversation_shares",
		"bulk revoke "+strconv.Itoa(len(revoked))+"/"+strconv.Itoa(len(ids))+" shares", r.RemoteAddr,
		map[string]interface{}{
			"requested": len(ids),
			"revoked":   len(result.RevokedIDs),
			"failed":    len(result.Failed),
		})
	OK(w, result)
}

// splitShareIDs 解析 `?ids=a,b,c`：去空白、丢空串、去重并保序。
// 去重是必要的 —— 重复 id 会让"requested"虚高，也让失败清单出现同一条两次。
func splitShareIDs(raw string) []string {
	parts := strings.Split(raw, ",")
	seen := make(map[string]bool, len(parts))
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		id := strings.TrimSpace(p)
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	return out
}
