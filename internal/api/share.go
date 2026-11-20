package api

import (
	"context"
	"crypto/rand"
	"encoding/base32"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/session"
	"github.com/jackc/pgx/v5"
)

// ShareHandler manages public conversation shares (rendered at /s/{token}
// without authentication, like chat.deepseek.com/share/{id}).
type ShareHandler struct {
	authenticator *auth.Authenticator
	sessionMgr    *session.Manager
	// cfg 只用于两件事：PUBLIC_BASE_URL（分享链接的对外基址）与 TRUSTED_PROXY_CIDRS
	// （是否采信 X-Forwarded-*）。允许为 nil（测试）：此时按请求的 scheme+Host 推导。
	cfg *config.Config
	// store 是 `PublicGet` 的数据来源。默认取 `db.GlobalDBManager`（见 NewShareHandler），
	// 抽出接口只为一件事：让"行不存在"与"库连不上"两条分支可被单测区分 —— 这两个在
	// 免鉴权端点上必须给出**不同**的状态码（404 vs 503），否则库挂了会伪装成"链接不存在"。
	store shareQuerier
	// access 记录一次成功访问（见 share_access.go）。抽成函数类型是为了单测注入，
	// 且**不阻塞**响应：写入发生在响应之后的独立协程里。nil = 不统计（测试默认）。
	access shareAccessWriter
}

// shareQuerier 收拢 `PublicGet` 需要的两个方法；`*db.DBManager` 天然满足。
type shareQuerier interface {
	QueryRow(ctx context.Context, sql string, args ...interface{}) pgx.Row
	Query(ctx context.Context, sql string, args ...interface{}) (pgx.Rows, error)
}

func NewShareHandler(a *auth.Authenticator, sm *session.Manager, cfg *config.Config) *ShareHandler {
	return &ShareHandler{authenticator: a, sessionMgr: sm, cfg: cfg, store: db.GlobalDBManager, access: dbShareAccessWriter}
}

// shareToken generates a random unguessable share id (80 bits entropy, base32
// lowercase, no padding — 16 chars). Shares must not be enumerable, unlike
// snowflake ids, because the id is the whole access control.
func shareToken() (string, error) {
	var b [10]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("share: crypto/rand unavailable: %w", err)
	}
	return strings.ToLower(base32.HexEncoding.WithPadding(base32.NoPadding).EncodeToString(b[:])), nil
}

// userInputTagRe 匹配 InputSanitizer.Sanitize 添加的 <user_input> 包装
// （internal/api/security.go:67）。`(?s)` 让 `.` 跨行 —— 包装内容是整段用户输入。
var userInputTagRe = regexp.MustCompile(`(?s)^\s*<user_input>\s*(.*?)\s*</user_input>\s*$`)

// stripUserInputTag 剥掉上述包装；不符合该形态时只去首尾空白。
// 与前端 chat-types.ts 的 stripUserInputTag 同一口径。
//
// 只剥这一层：`[thinking]` 块**有意保留** —— 公开页前端会折叠展示它，
// 在这里一并剥掉会让访问者失去"这次回答想了什么"的信息。
func stripUserInputTag(content string) string {
	if m := userInputTagRe.FindStringSubmatch(content); m != nil {
		return strings.TrimSpace(m[1])
	}
	return strings.TrimSpace(content)
}

// Create shares a conversation (POST /v1/conversations/{id}/share).
// The body lists the message ids to expose; only text messages are rendered.
// Idempotent: an active share for the session is returned as-is.
func (h *ShareHandler) Create(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	claims := auth.GetClaims(r.Context())

	sess, err := h.sessionMgr.GetSession(r.Context(), id)
	if err != nil {
		NotFound(w, "conversation not found")
		return
	}
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	var body struct {
		MessageIDs []string `json:"message_ids"`
		// Password 可选：非空即设访问密码（bcrypt 摘要落库，明文不落盘、不进日志）。
		Password string `json:"password"`
		// MaxViews 可选：访问次数上限；nil = 不限次。
		MaxViews *int `json:"max_views"`
		// ExpiresInDays 可选：有效期（天）。nil = 默认 shareTTL（30 天）。
		ExpiresInDays *int `json:"expires_in_days"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	messageIDs := dedupe(body.MessageIDs)
	if len(messageIDs) == 0 {
		BadRequest(w, "message_ids is required")
		return
	}
	// 限制单次分享最多 500 条消息，防止滥用
	if len(messageIDs) > 500 {
		BadRequest(w, "message_ids exceeds maximum of 500")
		return
	}

	// 保护参数先校验再落库：下面的 bcrypt 是**有意**慢的，参数非法时不该先付一次成本。
	password := strings.TrimSpace(body.Password)
	if password != "" && (len(password) < sharePasswordMinLen || len(password) > sharePasswordMaxLen) {
		BadRequest(w, "password must be 4-72 bytes")
		return
	}
	if body.MaxViews != nil && (*body.MaxViews < 1 || *body.MaxViews > 1_000_000) {
		BadRequest(w, "max_views must be between 1 and 1000000")
		return
	}
	// 有效期：默认 30 天；显式指定时限制在 1 天 - 1 年（更长等于"永久"，与撤销语义混淆）。
	ttl := shareTTL
	if body.ExpiresInDays != nil {
		if *body.ExpiresInDays < 1 || *body.ExpiresInDays > 365 {
			BadRequest(w, "expires_in_days must be between 1 and 365")
			return
		}
		ttl = time.Duration(*body.ExpiresInDays) * 24 * time.Hour
	}

	// 消息归属校验：只允许分享本会话的消息
	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id FROM messages WHERE session_id = $1 AND id = ANY($2::text[])`, id, messageIDs)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "validate messages failed")
		return
	}
	owned := make(map[string]bool)
	for rows.Next() {
		var mid string
		if rows.Scan(&mid) == nil {
			owned[mid] = true
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "validate messages failed")
		return
	}
	valid := make([]string, 0, len(messageIDs))
	for _, mid := range messageIDs {
		if owned[mid] {
			valid = append(valid, mid)
		}
	}
	if len(valid) == 0 {
		BadRequest(w, "message_ids does not match any message in this conversation")
		return
	}

	// 幂等：已有活跃分享直接返回。
	//
	// 保护参数（password / max_views）**不会**改写到已有链接上：一条已经发出去的链接
	// 中途改密码会让拿到旧链接的人突然进不去，而调用方以为"只是又点了一次分享"。
	// 因此把该链接**实际**的保护状态一并回传，前端据此提示"已存在一条受保护的分享"，
	// 想换保护就只能撤销后重建。
	existing, err := h.activeShare(r, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query share failed")
		return
	}
	if existing != nil {
		OK(w, map[string]interface{}{
			"share_id":     existing.ID,
			"created_at":   existing.CreatedAt,
			"url":          sharePublicURL(h.cfg, r, existing.ID),
			"has_password": existing.HasPassword,
			"max_views":    existing.MaxViews,
		})
		return
	}

	token, err := shareToken()
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create share failed")
		return
	}
	// 密码摘要：bcrypt（与登录口令同一算法）。只把摘要交给数据库，明文出函数即弃。
	var passwordHash string
	if password != "" {
		passwordHash, err = hashSharePassword(password)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "hash share password failed")
			return
		}
	}
	var maxViews interface{} // NULL = 不限次
	if body.MaxViews != nil {
		maxViews = *body.MaxViews
	}
	// user_id 列是 varchar(36)（见 0001 基线建表），直接传字符串与列类型一致。
	// 原写法 $3::uuid 会让 PG 走一次 I/O 转换而非类型化赋值 —— 在当前列类型下侥幸
	// 能过，但列类型一旦变动就会静默变形。
	// 真正致命的是 message_ids：[]string 被 pgx 编码成数组字面量写进 varchar(255)，
	// 超过 255 字符即报 value too long（列已在迁移 0016 转为 text[]）。
	// 分享链接带 TTL（此前只有 revoked_at，链接一经生成即永久有效 —— 会话内容会一直
	// 挂在一个 token 上）。默认 30 天，见 shareTTL；存量行的回填与列默认值见迁移 0017。
	_, err = db.GlobalDBManager.Exec(r.Context(),
		`INSERT INTO conversation_shares
		   (id, session_id, user_id, title, message_ids, created_at, expires_at, password_hash, max_views)
		 VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
		token, id, claims.UserID, sess.Title, valid, time.Now(), time.Now().Add(ttl),
		nullIfEmpty(passwordHash), maxViews)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "create share failed")
		return
	}

	OK(w, map[string]interface{}{
		"share_id":     token,
		"created_at":   time.Now(),
		"url":          sharePublicURL(h.cfg, r, token),
		"has_password": passwordHash != "",
		"max_views":    maxViews,
	})
}

// GetActive returns the active share for a conversation (GET /v1/conversations/{id}/share).
func (h *ShareHandler) GetActive(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	claims := auth.GetClaims(r.Context())

	sess, err := h.sessionMgr.GetSession(r.Context(), id)
	if err != nil {
		NotFound(w, "conversation not found")
		return
	}
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	share, err := h.activeShare(r, id)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query share failed")
		return
	}
	if share == nil {
		NotFound(w, "no active share")
		return
	}
	OK(w, map[string]interface{}{
		"share_id":     share.ID,
		"created_at":   share.CreatedAt,
		"url":          sharePublicURL(h.cfg, r, share.ID),
		"has_password": share.HasPassword,
		"max_views":    share.MaxViews,
	})
}

// Revoke cancels the active share (DELETE /v1/conversations/{id}/share).
// The public link stops resolving afterwards.
func (h *ShareHandler) Revoke(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if id == "" {
		BadRequest(w, "id is required")
		return
	}

	claims := auth.GetClaims(r.Context())

	sess, err := h.sessionMgr.GetSession(r.Context(), id)
	if err != nil {
		NotFound(w, "conversation not found")
		return
	}
	if sess.UserID != claims.UserID {
		Forbidden(w, "access denied")
		return
	}

	if _, err := db.GlobalDBManager.Exec(r.Context(),
		`UPDATE conversation_shares SET revoked_at = NOW() WHERE session_id = $1 AND revoked_at IS NULL`, id); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "revoke share failed")
		return
	}
	OK(w, map[string]string{"status": "revoked"})
}

// List lists the caller's usable shares (GET /v1/shares).
//
// 「数据管理 → 分享管理」需要它：现有接口都是按会话操作（Create/GetActive/Revoke），
// 要列出全部分享只能对每个会话各发一次请求，既慢又发现不了"哪个会话被分享过"。
// 已撤销、已过期的分享都不返回 —— 在用户视角里它们已经不存在（链接点开是 410）。
//
// 不分页：分享数天然有限，200 条上限兜底，避免为它引入一套分页参数。
func (h *ShareHandler) List(w http.ResponseWriter, r *http.Request) {
	claims := auth.GetClaims(r.Context())
	if claims == nil {
		Unauthorized(w, "not authenticated")
		return
	}

	rows, err := db.GlobalDBManager.Query(r.Context(),
		`SELECT id, session_id, COALESCE(title, ''), COALESCE(array_length(message_ids, 1), 0), created_at
		 FROM conversation_shares
		 WHERE user_id = $1 AND revoked_at IS NULL
		   AND expires_at > NOW()
		 ORDER BY created_at DESC
		 LIMIT 200`, claims.UserID)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "list shares failed")
		return
	}
	defer rows.Close()

	items := make([]map[string]interface{}, 0, 16)
	for rows.Next() {
		var (
			shareID   string
			sessionID string
			title     string
			msgCount  int
			createdAt time.Time
		)
		if err := rows.Scan(&shareID, &sessionID, &title, &msgCount, &createdAt); err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "scan share failed")
			return
		}
		items = append(items, map[string]interface{}{
			"share_id":      shareID,
			"session_id":    sessionID,
			"title":         title,
			"message_count": msgCount,
			"created_at":    createdAt,
			// url 与 Create/GetActive 同一口径：前端不再自己用 window.location.origin
			// 拼（桌面端那是 file://，拼出来的链接谁也打不开）。
			"url": sharePublicURL(h.cfg, r, shareID),
		})
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate shares failed")
		return
	}
	OK(w, map[string]interface{}{"items": items})
}

// PublicGet renders a share without authentication (GET /v1/share/{id}).
// Returns 410 Gone once the owner revokes the share, the TTL passes, or the
// view limit is reached; 401 with `share_password_required` when a password is
// set and missing/wrong.
//
// 成功打开会**异步**记一行访问日志（见 share_access.go）：统计是旁路，绝不阻塞
// 匿名首屏，也绝不因为写日志失败而让访客看到错误。
func (h *ShareHandler) PublicGet(w http.ResponseWriter, r *http.Request) {
	token := r.PathValue("id")
	if token == "" {
		NotFound(w, "share not found")
		return
	}

	var shareID, sessionID, title string
	var messageIDs []string
	var createdAt time.Time
	var revokedAt *time.Time
	// expires_at 允许为 NULL（回填前的极端历史行），但读侧一律按"已过期"处理：
	// 免鉴权路径上的默认值必须是拒绝，见迁移 0017。
	var expiresAt *time.Time
	// 保护字段：COALESCE 把 NULL 密码压成空串；max_views 保持 NULL 语义（不限次）。
	var passwordHash string
	var maxViews *int
	// views 是**已成功打开**的次数（限额的计数来源，与本表的追加日志同一事实）。
	var views int64
	err := h.store.QueryRow(r.Context(),
		`SELECT s.id, s.session_id, s.title, s.message_ids, s.created_at, s.revoked_at, s.expires_at,
		        COALESCE(s.password_hash, ''), s.max_views,
		        (SELECT count(*) FROM share_access_log l WHERE l.share_id = s.id)
		 FROM conversation_shares s WHERE s.id = $1`, token).
		Scan(&shareID, &sessionID, &title, &messageIDs, &createdAt, &revokedAt, &expiresAt,
			&passwordHash, &maxViews, &views)
	if err != nil {
		// 「行不存在」与「库连不上」必须分开：前者是用户事实（404，链接无效），
		// 后者是服务端故障（503，稍后重试就好）。此前一律 404，库抖一下会让所有
		// 分享链接看起来"被删了"，前端也就只能显示"链接不存在或已失效"。
		if errors.Is(err, pgx.ErrNoRows) {
			NotFound(w, "share not found")
			return
		}
		logAndRespond(w, err, http.StatusServiceUnavailable, ErrDBUnavailable)
		return
	}
	if revokedAt != nil {
		// code 与状态码分层：410 表达"永久失效"，code 区分"谁让它失效的"，
		// 分享页据此选文案（见 frontend-vue/src/views/ShareView.vue）。
		JSONWithCode(w, http.StatusGone, CodeShareRevoked, nil, "share revoked")
		return
	}
	if expiresAt == nil || time.Now().After(*expiresAt) {
		JSONWithCode(w, http.StatusGone, CodeShareExpired, nil, "share expired")
		return
	}
	// 密码在失效判断之后、取内容之前：撤销/过期的链接不该再暴露"它有密码"这一层
	// （那是给仍然有效的链接才需要的提示）。
	if passwordHash != "" && !sharePasswordMatches(passwordHash, r.Header.Get(sharePasswordHeader)) {
		// 401 而不是 403：访客是**未通过这道门**，前端据此渲染密码输入框。
		// 文案与响应体都不回显任何密码相关片段，避免错误的密码被反射进页面/日志。
		JSONWithCode(w, http.StatusUnauthorized, CodeSharePasswordRequired,
			map[string]interface{}{"password_required": true}, "share password required")
		return
	}
	if maxViews != nil && views >= int64(*maxViews) {
		JSONWithCode(w, http.StatusGone, CodeShareViewLimit,
			map[string]interface{}{"max_views": *maxViews}, "share view limit reached")
		return
	}

	// 只暴露用户选中的文本消息（user/assistant），按时间正序
	rows, err := h.store.Query(r.Context(),
		`SELECT role, content, created_at FROM messages
		 WHERE session_id = $1 AND id = ANY($2::text[]) AND role IN ('user', 'assistant')
		 ORDER BY created_at ASC, id ASC`, sessionID, messageIDs)
	if err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "query messages failed")
		return
	}
	defer rows.Close()

	type sharedMessage struct {
		Role      string    `json:"role"`
		Content   string    `json:"content"`
		CreatedAt time.Time `json:"created_at"`
	}
	messages := make([]sharedMessage, 0, 16)
	for rows.Next() {
		var m sharedMessage
		if err := rows.Scan(&m.Role, &m.Content, &m.CreatedAt); err != nil {
			continue
		}
		// 剥离 InputSanitizer 添加的 <user_input> 包装：它是提示注入防线的内部标记，
		// 没有展示价值，不该随公开链接进到响应体里（前端再剥一层只是兜底）。
		m.Content = stripUserInputTag(m.Content)
		messages = append(messages, m)
	}
	if err := rows.Err(); err != nil {
		logAndRespond(w, err, http.StatusInternalServerError, "iterate messages failed")
		return
	}

	OK(w, map[string]interface{}{
		"id":         shareID,
		"title":      title,
		"created_at": createdAt,
		"messages":   messages,
	})

	// 统计放在响应之后：只有走到这里才是"真的被看到了一次"（密码/限额/失效分支
	// 都在上面提前返回，不会被记成访问）。
	h.recordAccessAsync(r, shareID)
}

// activeShare returns the session's non-revoked, non-expired share, or nil.
//
// shareTTL 是新建分享链接的有效期。conversation_shares 早期只有 revoked_at，分享一经
// 创建便永久有效；0013 加列、0017 回填并把列默认值也设成 30 天，读侧从此不再接受
// expires_at IS NULL —— 有效期缺失一律按不可用处理（fail-closed）。
const shareTTL = 30 * 24 * time.Hour

// activeShareInfo 是"当前活跃分享"的只读快照（用户态接口的回包形状）。
//
// 保护状态一并带出：客户端需要知道这条**已存在**的链接是否设了密码 / 限额，
// 否则"我又点了一次分享"与"这条链接受保护"两件事在界面上无法区分。
type activeShareInfo struct {
	ID          string
	CreatedAt   time.Time
	HasPassword bool
	MaxViews    *int
}

func (h *ShareHandler) activeShare(r *http.Request, sessionID string) (*activeShareInfo, error) {
	info := &activeShareInfo{}
	// COALESCE(password_hash, '') <> '' 直接把 NULL 归一成 false，省掉一次指针判空。
	err := db.GlobalDBManager.QueryRow(r.Context(),
		`SELECT id, created_at, COALESCE(password_hash, '') <> '', max_views
		 FROM conversation_shares
		 WHERE session_id = $1 AND revoked_at IS NULL
		   AND expires_at > NOW()
		 LIMIT 1`, sessionID).
		Scan(&info.ID, &info.CreatedAt, &info.HasPassword, &info.MaxViews)
	if err != nil {
		return nil, nil // no active share (or DB row missing)
	}
	return info, nil
}

func dedupe(ids []string) []string {
	seen := make(map[string]bool, len(ids))
	out := make([]string, 0, len(ids))
	for _, id := range ids {
		if id == "" || seen[id] {
			continue
		}
		seen[id] = true
		out = append(out, id)
	}
	return out
}
