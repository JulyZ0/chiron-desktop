package api

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── `GET /v1/share/{id}`（匿名分享读取）的**状态码契约** ──
//
// 守两件真实事故形态：
//
//  1. 「库连不上」被报成 404 —— 免鉴权端点上所有错误此前都走 `NotFound`，于是
//     PostgreSQL/连接池一抖，**每一条**分享链接都变成"不存在或已失效"，用户以为
//     分享被删了，运维也拿不到任何信号。行不存在（404）与服务端故障（503）必须分开。
//  2. 410 只有状态码、没有 code —— 撤销与过期同码，前端只能猜文案，实测把
//     "过期"显示成"已被创建者删除"。现在两者各带 `share_revoked` / `share_expired`。

// shareRowData 是 conversation_shares 那一行的替身数据（列顺序与 PublicGet 的 SELECT 一致）。
type shareRowData struct {
	id         string
	sessionID  string
	title      string
	messageIDs []string
	createdAt  time.Time
	revokedAt  *time.Time
	expiresAt  *time.Time
	// passwordHash 空串 = 未设密码（SQL 侧由 COALESCE 把 NULL 压成 ''）。
	passwordHash string
	// maxViews nil = 不限次。
	maxViews *int
	// views 是访问统计子查询的返回值（已成功打开的次数）。
	views int64
	// err 非空时 Scan 直接返回它（模拟行不存在 / 连接失败）
	err error
}

func (d shareRowData) row() pgx.Row {
	return shareFakeRow{scan: func(dest ...any) error {
		if d.err != nil {
			return d.err
		}
		*dest[0].(*string) = d.id
		*dest[1].(*string) = d.sessionID
		*dest[2].(*string) = d.title
		*dest[3].(*[]string) = d.messageIDs
		*dest[4].(*time.Time) = d.createdAt
		*dest[5].(**time.Time) = d.revokedAt
		*dest[6].(**time.Time) = d.expiresAt
		*dest[7].(*string) = d.passwordHash
		*dest[8].(**int) = d.maxViews
		*dest[9].(*int64) = d.views
		return nil
	}}
}

type shareFakeRow struct {
	scan func(dest ...any) error
}

func (r shareFakeRow) Scan(dest ...any) error {
	if r.scan == nil {
		return pgx.ErrNoRows
	}
	return r.scan(dest...)
}

// shareFakeRows 只认 role/content/created_at 三列 —— 正是 PublicGet 的 messages 查询。
type shareFakeRows struct {
	rows [][]any
	idx  int
}

func (r *shareFakeRows) Close()                                       {}
func (r *shareFakeRows) Err() error                                   { return nil }
func (r *shareFakeRows) CommandTag() pgconn.CommandTag                { return pgconn.CommandTag{} }
func (r *shareFakeRows) FieldDescriptions() []pgconn.FieldDescription { return nil }
func (r *shareFakeRows) Values() ([]any, error)                       { return nil, nil }
func (r *shareFakeRows) RawValues() [][]byte                          { return nil }
func (r *shareFakeRows) Conn() *pgx.Conn                              { return nil }

func (r *shareFakeRows) Next() bool {
	if r.idx >= len(r.rows) {
		return false
	}
	r.idx++
	return true
}

func (r *shareFakeRows) Scan(dest ...any) error {
	row := r.rows[r.idx-1]
	if len(dest) != len(row) {
		return errors.New("shareFakeRows: column count mismatch")
	}
	for i, d := range dest {
		switch p := d.(type) {
		case *string:
			*p, _ = row[i].(string)
		case *time.Time:
			*p, _ = row[i].(time.Time)
		// 后台分享列表的聚合列：count(*) 是 bigint（int64），array_length 是 int。
		case *int64:
			*p, _ = row[i].(int64)
		case *int:
			*p, _ = row[i].(int)
		// last_viewed_at 可为 NULL（从未被访问）。
		case **time.Time:
			*p, _ = row[i].(*time.Time)
		default:
			return errors.New("shareFakeRows: unsupported dest type")
		}
	}
	return nil
}

type fakeShareStore struct {
	row      pgx.Row
	rows     pgx.Rows
	queryErr error
	// rowSets 按调用顺序消费，用于"一次请求里查多次"的端点（后台列表：列表 + 趋势）。
	rowSets []pgx.Rows
}

func (s *fakeShareStore) QueryRow(context.Context, string, ...interface{}) pgx.Row { return s.row }

func (s *fakeShareStore) Query(context.Context, string, ...interface{}) (pgx.Rows, error) {
	if s.queryErr != nil {
		return nil, s.queryErr
	}
	if len(s.rowSets) > 0 {
		next := s.rowSets[0]
		s.rowSets = s.rowSets[1:]
		return next, nil
	}
	if s.rows == nil {
		return &shareFakeRows{}, nil
	}
	return s.rows, nil
}

func publicShareRequest() *http.Request {
	req := httptest.NewRequest(http.MethodGet, "http://gateway.local/v1/share/tok123", nil)
	req.SetPathValue("id", "tok123")
	return req
}

func decodeShareResponse(t *testing.T, rec *httptest.ResponseRecorder) APIResponse {
	t.Helper()
	var resp APIResponse
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode response: %v (body=%s)", err, rec.Body.String())
	}
	return resp
}

func futureTime() *time.Time {
	tm := time.Now().Add(24 * time.Hour)
	return &tm
}

// 行不存在 = 用户事实：404。
func TestSharePublicGet_MissingRowIs404(t *testing.T) {
	h := &ShareHandler{store: &fakeShareStore{row: shareRowData{err: pgx.ErrNoRows}.row()}}
	rec := httptest.NewRecorder()

	h.PublicGet(rec, publicShareRequest())

	if rec.Code != http.StatusNotFound {
		t.Fatalf("行不存在应为 404, got %d", rec.Code)
	}
}

// 库连不上 = 服务端故障：503，**不是** 404。
func TestSharePublicGet_DBFailureIs503(t *testing.T) {
	h := &ShareHandler{store: &fakeShareStore{row: shareRowData{err: errors.New("connection refused")}.row()}}
	rec := httptest.NewRecorder()

	h.PublicGet(rec, publicShareRequest())

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("库不可用应为 503（不能伪装成 404）, got %d", rec.Code)
	}
	if resp := decodeShareResponse(t, rec); resp.Code != CodeServiceUnavailable {
		t.Fatalf("库不可用的 code 应为 %q, got %q", CodeServiceUnavailable, resp.Code)
	}
}

// 撤销：410 + share_revoked。
func TestSharePublicGet_RevokedIs410WithRevokedCode(t *testing.T) {
	now := time.Now()
	h := &ShareHandler{store: &fakeShareStore{row: shareRowData{
		id: "tok123", sessionID: "s1", title: "T", messageIDs: []string{"m1"},
		createdAt: now, revokedAt: &now, expiresAt: futureTime(),
	}.row()}}
	rec := httptest.NewRecorder()

	h.PublicGet(rec, publicShareRequest())

	if rec.Code != http.StatusGone {
		t.Fatalf("已撤销应为 410, got %d", rec.Code)
	}
	if resp := decodeShareResponse(t, rec); resp.Code != CodeShareRevoked {
		t.Fatalf("撤销的 code 应为 %q, got %q", CodeShareRevoked, resp.Code)
	}
}

// 过期（含 expires_at 为 NULL 的历史行）：410 + share_expired，不能与撤销混为一谈。
func TestSharePublicGet_ExpiredIs410WithExpiredCode(t *testing.T) {
	past := time.Now().Add(-time.Hour)
	cases := map[string]*time.Time{
		"超过有效期":         &past,
		"expires_at 为空": nil,
	}
	for name, expiresAt := range cases {
		t.Run(name, func(t *testing.T) {
			h := &ShareHandler{store: &fakeShareStore{row: shareRowData{
				id: "tok123", sessionID: "s1", title: "T", messageIDs: []string{"m1"},
				createdAt: past, expiresAt: expiresAt,
			}.row()}}
			rec := httptest.NewRecorder()

			h.PublicGet(rec, publicShareRequest())

			if rec.Code != http.StatusGone {
				t.Fatalf("已过期应为 410, got %d", rec.Code)
			}
			if resp := decodeShareResponse(t, rec); resp.Code != CodeShareExpired {
				t.Fatalf("过期的 code 应为 %q, got %q", CodeShareExpired, resp.Code)
			}
		})
	}
}

// 正常路径：只回文本消息，且剥掉 InputSanitizer 的 <user_input> 包装。
func TestSharePublicGet_OKStripsUserInputWrapper(t *testing.T) {
	created := time.Now().Add(-time.Minute)
	h := &ShareHandler{store: &fakeShareStore{
		row: shareRowData{
			id: "tok123", sessionID: "s1", title: "标题", messageIDs: []string{"m1", "m2"},
			createdAt: created, expiresAt: futureTime(),
		}.row(),
		rows: &shareFakeRows{rows: [][]any{
			{"user", "<user_input>你好</user_input>", created},
			{"assistant", "[thinking]想一想\n\n回答正文", created},
		}},
	}}
	rec := httptest.NewRecorder()

	h.PublicGet(rec, publicShareRequest())

	if rec.Code != http.StatusOK {
		t.Fatalf("有效分享应为 200, got %d", rec.Code)
	}
	var resp struct {
		Success bool `json:"success"`
		Data    struct {
			ID       string `json:"id"`
			Title    string `json:"title"`
			Messages []struct {
				Role    string `json:"role"`
				Content string `json:"content"`
			} `json:"messages"`
		} `json:"data"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !resp.Success || resp.Data.ID != "tok123" || len(resp.Data.Messages) != 2 {
		t.Fatalf("响应形状不符: %+v", resp)
	}
	if got := resp.Data.Messages[0].Content; got != "你好" {
		t.Fatalf("<user_input> 未被剥离: %q", got)
	}
	// 思考块保留（公开页折叠展示），不能在这里被一并剥掉
	if got := resp.Data.Messages[1].Content; got != "[thinking]想一想\n\n回答正文" {
		t.Fatalf("[thinking] 不该被剥离: %q", got)
	}
}
