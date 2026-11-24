package api

// share_public_http_test.go —— 分享页的**匿名 HTTP 集成**测试。
//
// 与 share_public_test.go 的分工：那边直接调 handler，钉状态码/文案/统计；
// 这里从 httptest.Server 发真实 HTTP 请求，走 registerPublicEndpoints 注册的
// 同一张路由表（同伴 share_link_test.go 覆盖 URL 拼装）。只有走链路才能钉住两件事：
//
//  1. 匿名可达不是"认证恰好放行"：`GET /v1/share/{id}` 不在 authMW 之后。
//     注入一个"被调用就 401 并记账"的鉴权中间件，成功读取时它必须一次都没被调用。
//  2. 公开响应只含创建者**选中**的文本消息：不带附件、工具事件、知识库元数据，
//     也不带未选中的会话消息 —— 这是分享页最核心的隐私契约（选几条给人看，不是
//     导出整个会话）。
//
// 保护类行为（密码/过期/撤销/上限）在这里也各有一条 HTTP 用例：它们必须原样
// 出现在真实响应里，而不是只存在于 handler 内部的 if 分支。

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

// ── 匿名 HTTP 测试台 ──────────────────────────────────────────────────

// shareAuthProbe 扮演公开路由表里的 authMW：任何经过鉴权中间件的请求都会被记账并拒绝。
// 分享页要 200 且 hits == 0，才说明它真的绕开了鉴权（而不是"认证恰好没拦住"）。
type shareAuthProbe struct{ hits int32 }

func (p *shareAuthProbe) middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&p.hits, 1)
		Unauthorized(w, "auth required")
	})
}

func (p *shareAuthProbe) called() bool { return atomic.LoadInt32(&p.hits) > 0 }

// passthroughMW 透传（限流/公开外层的替身：它们不参与本组用例的断言）。
func passthroughMW(next http.Handler) http.Handler { return next }

// newAnonymousShareServer 用**生产路由注册函数**搭一台只含公开端点的服务。
// 关键点：注册逻辑本身就是被测对象 —— 测试里另写一份 `mux.Handle` 会让
// "某天把分享路由挪到 authMW 后面"这类回归完全测不出来。
func newAnonymousShareServer(t *testing.T, h *ShareHandler) (*httptest.Server, *shareAuthProbe) {
	t.Helper()
	probe := &shareAuthProbe{}
	mux := http.NewServeMux()
	registerPublicEndpoints(mux, probe.middleware, passthroughMW, passthroughMW,
		&SearchHandler{}, h, &SystemHandler{}, &MediaHandler{}, &config.Config{}, nil)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, probe
}

// sharedSessionMessage 是 messages 表一行的替身，**故意带上**公开页不该看到的列。
// 真实表里没有 attachments/metadata 两列，但它们代表的是同一类风险：
// 只要消息查询的投影变宽（把附件引用、知识库/工具元数据一起 SELECT 出来），
// 公开页就会原样吐给匿名访客。这里把它们放在数据里，再要求响应里一个字都不出现。
type sharedSessionMessage struct {
	id          string
	role        string
	content     string
	createdAt   time.Time
	attachments string
	metadata    string
}

// anonymousShareStore 是 shareQuerier 的内存实现：分享行按 shareRowData 给出，
// 消息查询按 handler 传来的 (session_id, message_ids) 在本地"表"上求值。
type anonymousShareStore struct {
	row      shareRowData
	messages []sharedSessionMessage

	mu      sync.Mutex
	queries []string
	args    [][]interface{}
}

func (s *anonymousShareStore) QueryRow(context.Context, string, ...interface{}) pgx.Row {
	return s.row.row()
}

func (s *anonymousShareStore) Query(_ context.Context, sql string, args ...interface{}) (pgx.Rows, error) {
	s.mu.Lock()
	s.queries = append(s.queries, sql)
	s.args = append(s.args, args)
	s.mu.Unlock()

	// 公开页只允许发一条消息查询，且**必须**同时带 session_id 与 message_ids：
	// 少任何一个，这条查询就退化成了"按会话全量导出"。
	if len(args) != 2 {
		return nil, fmt.Errorf("公开分享的消息查询应带 session_id + message_ids 两个参数，实际 %d 个", len(args))
	}
	sessionID, _ := args[0].(string)
	ids, ok := args[1].([]string)
	if !ok || len(ids) == 0 {
		return nil, fmt.Errorf("message_ids 缺失或类型不符（%T）", args[1])
	}
	if sessionID == "" {
		return nil, fmt.Errorf("session_id 为空")
	}
	wanted := make(map[string]bool, len(ids))
	for _, id := range ids {
		wanted[id] = true
	}
	rows := make([][]any, 0, len(ids))
	for _, m := range s.messages {
		if !wanted[m.id] {
			continue
		}
		// 与 SQL 的 `role IN ('user','assistant')` 同一口径：工具/系统消息不属于访客视图。
		if m.role != "user" && m.role != "assistant" {
			continue
		}
		rows = append(rows, []any{m.role, m.content, m.createdAt})
	}
	// shareFakeRows 的 Scan 只认 3 个目标列：一旦投影变宽（加了 attachments 等），
	// Scan 会报错、消息数归零，用例随即失败。
	return &shareFakeRows{rows: rows}, nil
}

func (s *anonymousShareStore) lastQuery(t *testing.T) string {
	t.Helper()
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.queries) == 0 {
		t.Fatal("没有发出任何消息查询")
	}
	return s.queries[len(s.queries)-1]
}

// ── HTTP 小工具 ──────────────────────────────────────────────────────

type anonShareBody struct {
	Success bool   `json:"success"`
	Code    string `json:"code"`
	Error   string `json:"error"`
	Data    struct {
		ID       string                       `json:"id"`
		Title    string                       `json:"title"`
		Messages []map[string]json.RawMessage `json:"messages"`
	} `json:"data"`
}

func getAnonymousShare(t *testing.T, url, password string) (*http.Response, string) {
	t.Helper()
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		t.Fatalf("构造请求: %v", err)
	}
	// 匿名：不带 Authorization / Cookie。
	if password != "" {
		req.Header.Set(sharePasswordHeader, password)
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("请求 %s: %v", url, err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("读响应体: %v", err)
	}
	return resp, string(raw)
}

func decodeAnonShare(t *testing.T, body string) anonShareBody {
	t.Helper()
	var out anonShareBody
	if err := json.Unmarshal([]byte(body), &out); err != nil {
		t.Fatalf("解析响应体: %v (body=%s)", err, body)
	}
	return out
}

func activeShareRow(id string, expiresAt *time.Time) shareRowData {
	return shareRowData{
		id: id, sessionID: "sess-1", title: "公开会话",
		messageIDs: []string{"m-user", "m-ai"},
		createdAt:  time.Now().Add(-time.Hour), expiresAt: expiresAt,
	}
}

// ── 1. 未登录访问成功 ────────────────────────────────────────────────

func TestSharePublicHTTP_AnonymousReadSucceedsWithoutAuth(t *testing.T) {
	base := time.Now().Add(-30 * time.Minute)
	store := &anonymousShareStore{
		row: activeShareRow("anon-token", futureTime()),
		messages: []sharedSessionMessage{
			{id: "m-user", role: "user", content: "<user_input>你好</user_input>", createdAt: base},
			{id: "m-ai", role: "assistant", content: "[thinking]想一想\n\n回答正文", createdAt: base.Add(time.Second)},
		},
	}
	srv, probe := newAnonymousShareServer(t, &ShareHandler{store: store})

	resp, body := getAnonymousShare(t, srv.URL+"/v1/share/anon-token", "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("匿名读取应为 200, got %d (body=%s)", resp.StatusCode, body)
	}
	// 走的必须是公开路由：鉴权中间件一次都不该被调用。
	if probe.called() {
		t.Fatal("分享读取经过了鉴权中间件 —— 匿名访客会被 401 拦下")
	}
	payload := decodeAnonShare(t, body)
	if !payload.Success || payload.Data.ID != "anon-token" {
		t.Fatalf("响应形状不符: %s", body)
	}
	if len(payload.Data.Messages) != 2 {
		t.Fatalf("应返回 2 条消息, got %d (body=%s)", len(payload.Data.Messages), body)
	}
	var content string
	if err := json.Unmarshal(payload.Data.Messages[0]["content"], &content); err != nil {
		t.Fatalf("解析消息正文: %v", err)
	}
	// <user_input> 是提示注入防线的内部标记，不能随公开响应出去。
	if content != "你好" {
		t.Fatalf("<user_input> 未被剥离: %q", content)
	}

	// 只暴露 GET：其它方法不该落进 handler（也就没有"随手 POST 一下"的入口）。
	postResp, err := http.Post(srv.URL+"/v1/share/anon-token", "application/json", strings.NewReader("{}"))
	if err != nil {
		t.Fatalf("POST: %v", err)
	}
	postResp.Body.Close()
	if postResp.StatusCode == http.StatusOK {
		t.Fatalf("POST /v1/share/{id} 不应可用, got %d", postResp.StatusCode)
	}
}

// ── 2. 只返回创建者选中的消息，什么都不夹带 ──────────────────────────

func TestSharePublicHTTP_ReturnsOnlySelectedMessages(t *testing.T) {
	base := time.Now().Add(-time.Hour)
	now := base
	store := &anonymousShareStore{
		row: shareRowData{
			id: "tok-leak", sessionID: "sess-leak", title: "只分享两条",
			// 选中：一条提问、一条回答，外加一条**工具事件**和一条 system
			//（后两条即便被创建者勾中也绝不该出现在公开页）。
			messageIDs: []string{"m-user", "m-ai", "m-tool", "m-sys"},
			createdAt:  base, expiresAt: futureTime(),
		},
		messages: []sharedSessionMessage{
			{id: "m-user", role: "user", content: "选中的提问", createdAt: now},
			{id: "m-ai", role: "assistant", content: "选中的回答", createdAt: now.Add(time.Second)},
			{id: "m-tool", role: "tool", content: "TOOL_EVENT: 读取 /etc/shadow", createdAt: now.Add(2 * time.Second),
				metadata: `{"tool":"exec","args":{"path":"/etc/shadow"}}`},
			{id: "m-sys", role: "system", content: "SYSTEM_PROMPT: 你是内部助手", createdAt: now.Add(3 * time.Second)},
			// 未选中：整段会话里的其它消息，一条都不该出现。
			{id: "m-secret", role: "user", content: "未选中的隐私消息", createdAt: now.Add(4 * time.Second)},
			{id: "m-attach", role: "assistant", content: "附件已上传", createdAt: now.Add(5 * time.Second),
				attachments: "s3://chiron-private/shadow.png"},
			{id: "m-kb", role: "assistant", content: "知识库检索结果", createdAt: now.Add(6 * time.Second),
				metadata: `{"knowledge_base":"内部库","status":403}`},
		},
	}
	srv, probe := newAnonymousShareServer(t, &ShareHandler{store: store})

	resp, body := getAnonymousShare(t, srv.URL+"/v1/share/tok-leak", "")
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("应为 200, got %d (body=%s)", resp.StatusCode, body)
	}
	if probe.called() {
		t.Fatal("分享读取经过了鉴权中间件")
	}
	payload := decodeAnonShare(t, body)
	if len(payload.Data.Messages) != 2 {
		t.Fatalf("只应返回选中的 2 条文本消息, got %d (body=%s)", len(payload.Data.Messages), body)
	}
	for i, raw := range payload.Data.Messages {
		// 每条消息的字段集必须恰好是 role/content/created_at：
		// 多一个字段就是一次泄漏（附件、工具调用、知识库元数据都曾挂在这里）。
		keys := make([]string, 0, len(raw))
		for k := range raw {
			keys = append(keys, k)
		}
		if len(keys) != 3 {
			t.Fatalf("第 %d 条消息字段超出 {role,content,created_at}: %v", i, keys)
		}
		for _, allowed := range []string{"role", "content", "created_at"} {
			if _, ok := raw[allowed]; !ok {
				t.Fatalf("第 %d 条消息缺字段 %q: %v", i, allowed, keys)
			}
		}
		var m struct{ Role string }
		if err := json.Unmarshal(raw["role"], &m.Role); err != nil {
			t.Fatalf("解析 role: %v", err)
		}
		if m.Role != "user" && m.Role != "assistant" {
			t.Fatalf("公开页出现了 %q 角色消息", m.Role)
		}
	}

	// 泄露面：工具事件、系统提示、未选中消息、附件地址、知识库元数据、会话 id、消息 id。
	for _, forbidden := range []string{
		"TOOL_EVENT", "/etc/shadow", "SYSTEM_PROMPT", "未选中的隐私消息",
		"附件已上传", "s3://", "知识库检索结果", "内部库",
		"sess-leak", "m-user", "m-ai", "m-tool", "m-sys",
		"attachments", "metadata", "tool_calls", "knowledge_base",
	} {
		if strings.Contains(body, forbidden) {
			t.Fatalf("公开响应泄露了 %q: %s", forbidden, body)
		}
	}

	// SQL 层再钉一道：投影不能变宽，过滤条件不能消失。
	sqlText := store.lastQuery(t)
	for _, needle := range []string{"role IN ('user', 'assistant')", "id = ANY", "session_id = $1"} {
		if !strings.Contains(sqlText, needle) {
			t.Fatalf("消息查询缺少过滤条件 %q: %s", needle, sqlText)
		}
	}
	if strings.Contains(strings.ToUpper(sqlText), "SELECT *") {
		t.Fatalf("消息查询用了 SELECT *（会话全量导出）: %s", sqlText)
	}
	for _, forbiddenCol := range []string{"attachments", "metadata", "tool_calls", "knowledge"} {
		if strings.Contains(sqlText, forbiddenCol) {
			t.Fatalf("消息查询投影里出现了 %q: %s", forbiddenCol, sqlText)
		}
	}
}

// ── 3. 访问密码：缺失/错误/正确 ──────────────────────────────────────

func TestSharePublicHTTP_PasswordGate(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	base := time.Now().Add(-time.Hour)

	t.Run("缺密码与错密码都是 401 且不回显", func(t *testing.T) {
		for name, provided := range map[string]string{"未提供": "", "错误": "not-it"} {
			t.Run(name, func(t *testing.T) {
				row := activeShareRow("tok-pw", futureTime())
				row.passwordHash = string(hash)
				store := &anonymousShareStore{
					row:      row,
					messages: []sharedSessionMessage{{id: "m-user", role: "user", content: "机密正文", createdAt: base}},
				}
				srv, probe := newAnonymousShareServer(t, &ShareHandler{store: store})

				resp, body := getAnonymousShare(t, srv.URL+"/v1/share/tok-pw", provided)
				if resp.StatusCode != http.StatusUnauthorized {
					t.Fatalf("应为 401, got %d (body=%s)", resp.StatusCode, body)
				}
				if payload := decodeAnonShare(t, body); payload.Code != CodeSharePasswordRequired {
					t.Fatalf("code 应为 %q, got %q", CodeSharePasswordRequired, payload.Code)
				}
				// 密码错误 ≠ 未认证：不能让前端把访客弹去登录页。
				if probe.called() {
					t.Fatal("密码校验经过了 authMW —— 前端会把 401 误读成未登录")
				}
				if strings.Contains(body, "机密正文") {
					t.Fatalf("密码未通过却返回了正文: %s", body)
				}
				if provided != "" && strings.Contains(strings.ToLower(body), strings.ToLower(provided)) {
					t.Fatalf("响应体回显了提交的密码: %s", body)
				}
			})
		}
	})

	t.Run("密码正确即 200", func(t *testing.T) {
		row := activeShareRow("tok-pw", futureTime())
		row.passwordHash = string(hash)
		store := &anonymousShareStore{
			row: row,
			messages: []sharedSessionMessage{
				{id: "m-user", role: "user", content: "机密正文", createdAt: base},
			},
		}
		srv, probe := newAnonymousShareServer(t, &ShareHandler{store: store})

		resp, body := getAnonymousShare(t, srv.URL+"/v1/share/tok-pw", "s3cret")
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("正确密码应为 200, got %d (body=%s)", resp.StatusCode, body)
		}
		if probe.called() {
			t.Fatal("分享读取经过了鉴权中间件")
		}
		if payload := decodeAnonShare(t, body); len(payload.Data.Messages) != 1 {
			t.Fatalf("应返回选中消息: %s", body)
		}
		if strings.Contains(body, "s3cret") {
			t.Fatalf("响应体回显了密码: %s", body)
		}
	})
}

// ── 4. 撤销与过期：都是 410，但 code 不同 ────────────────────────────

func TestSharePublicHTTP_RevokedAndExpired(t *testing.T) {
	revokedAt := time.Now().Add(-time.Minute)
	past := time.Now().Add(-time.Hour)
	pwHash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}

	cases := []struct {
		name     string
		mutate   func(*shareRowData)
		wantCode string
	}{
		{
			name:     "已撤销",
			mutate:   func(r *shareRowData) { r.revokedAt = &revokedAt },
			wantCode: CodeShareRevoked,
		},
		{
			// 撤销**且**设了密码：410 优先于 401，否则"这条链接有密码"这件事
			// 会泄露给一个本来就不该再打开它的人。
			name: "已撤销且设了密码仍是 410（不提示要密码）",
			mutate: func(r *shareRowData) {
				r.revokedAt = &revokedAt
				r.passwordHash = string(pwHash)
			},
			wantCode: CodeShareRevoked,
		},
		{
			name:     "已过期",
			mutate:   func(r *shareRowData) { r.expiresAt = &past },
			wantCode: CodeShareExpired,
		},
		{
			// 回填前的历史行：expires_at 为 NULL 一律 fail-closed 按过期处理。
			name:     "有效期缺失",
			mutate:   func(r *shareRowData) { r.expiresAt = nil },
			wantCode: CodeShareExpired,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			row := activeShareRow("tok-dead", futureTime())
			tc.mutate(&row)
			store := &anonymousShareStore{
				row:      row,
				messages: []sharedSessionMessage{{id: "m-user", role: "user", content: "不该出现的正文", createdAt: time.Now()}},
			}
			srv, _ := newAnonymousShareServer(t, &ShareHandler{store: store})

			resp, body := getAnonymousShare(t, srv.URL+"/v1/share/tok-dead", "s3cret")
			if resp.StatusCode != http.StatusGone {
				t.Fatalf("应为 410, got %d (body=%s)", resp.StatusCode, body)
			}
			payload := decodeAnonShare(t, body)
			if payload.Code != tc.wantCode {
				t.Fatalf("code 应为 %q, got %q", tc.wantCode, payload.Code)
			}
			if strings.Contains(body, "不该出现的正文") {
				t.Fatalf("失效链接仍返回了正文: %s", body)
			}
		})
	}
}

// ── 5. 访问次数上限 ──────────────────────────────────────────────────

func TestSharePublicHTTP_ViewLimit(t *testing.T) {
	limit := 2
	row := activeShareRow("tok-limit", futureTime())
	row.maxViews = &limit
	row.views = 2

	srv, _ := newAnonymousShareServer(t, &ShareHandler{store: &anonymousShareStore{
		row:      row,
		messages: []sharedSessionMessage{{id: "m-user", role: "user", content: "限流后的正文", createdAt: time.Now()}},
	}})
	resp, body := getAnonymousShare(t, srv.URL+"/v1/share/tok-limit", "")
	if resp.StatusCode != http.StatusGone {
		t.Fatalf("达到上限应为 410, got %d (body=%s)", resp.StatusCode, body)
	}
	payload := decodeAnonShare(t, body)
	if payload.Code != CodeShareViewLimit {
		t.Fatalf("code 应为 %q, got %q", CodeShareViewLimit, payload.Code)
	}
	if strings.Contains(body, "限流后的正文") {
		t.Fatalf("限额耗尽仍返回了正文: %s", body)
	}

	// 差一次可用：边界是 >=，不是 >。
	row2 := activeShareRow("tok-limit", futureTime())
	row2.maxViews = &limit
	row2.views = int64(limit - 1)
	srv2, _ := newAnonymousShareServer(t, &ShareHandler{store: &anonymousShareStore{
		row:      row2,
		messages: []sharedSessionMessage{{id: "m-user", role: "user", content: "还剩一次", createdAt: time.Now()}},
	}})
	resp2, body2 := getAnonymousShare(t, srv2.URL+"/v1/share/tok-limit", "")
	if resp2.StatusCode != http.StatusOK {
		t.Fatalf("上限内应为 200, got %d (body=%s)", resp2.StatusCode, body2)
	}
	if !strings.Contains(body2, "还剩一次") {
		t.Fatalf("应返回正文: %s", body2)
	}
}

// ── 6. 不存在的 token ────────────────────────────────────────────────

func TestSharePublicHTTP_UnknownTokenIs404(t *testing.T) {
	srv, _ := newAnonymousShareServer(t, &ShareHandler{
		store: &anonymousShareStore{row: shareRowData{err: pgx.ErrNoRows}},
	})
	resp, body := getAnonymousShare(t, srv.URL+"/v1/share/nope", "")
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("不存在的分享应为 404, got %d (body=%s)", resp.StatusCode, body)
	}
}
