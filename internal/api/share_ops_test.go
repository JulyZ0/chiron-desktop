package api

// share_ops_test.go —— 分享运营（统计 / 密码 / 限额 / 批量撤销）的契约。
//
// 这些用例守的是四条"看起来能跑、实际会伤人"的边界：
//  1. 统计写入必须是**脱敏**的（不落明文 IP、不落完整 Referer），且**不阻塞**响应；
//  2. 失败分支（404 / 410 / 密码错）不能混进"有人看过"的计数；
//  3. 限额按"已成功打开次数"判定，不能把 401 也算成一次访问；
//  4. 批量撤销的"部分失败"必须如实回报，而不是一个笼统的 500。

import (
	"context"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"golang.org/x/crypto/bcrypt"
)

func errNoRows() error { return pgx.ErrNoRows }

func timePtr(t time.Time) *time.Time { return &t }

// ── 访问统计 ─────────────────────────────────────────────────────────

// shareAccessRecorder 是同步收集访问记录的假实现（handler 在协程里调，用 channel 同步）。
type shareAccessRecorder struct {
	got chan shareAccessEntry
}

func newAccessRecorder() *shareAccessRecorder {
	return &shareAccessRecorder{got: make(chan shareAccessEntry, 4)}
}

func (r *shareAccessRecorder) writer() shareAccessWriter {
	return func(_ context.Context, e shareAccessEntry) { r.got <- e }
}

func (r *shareAccessRecorder) next(t *testing.T) shareAccessEntry {
	t.Helper()
	select {
	case e := <-r.got:
		return e
	case <-time.After(2 * time.Second):
		t.Fatal("超时：成功访问没有写出访问记录")
		return shareAccessEntry{}
	}
}

func okShareStore() *fakeShareStore {
	return &fakeShareStore{
		row: shareRowData{
			id: "tok123", sessionID: "s1", title: "T", messageIDs: []string{"m1"},
			createdAt: time.Now().Add(-time.Minute), expiresAt: futureTime(),
		}.row(),
	}
}

func accessRequest() *http.Request {
	req := publicShareRequest()
	req.RemoteAddr = "203.0.113.7:5555"
	req.Header.Set("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36")
	req.Header.Set("Referer", "https://news.example.com/a/b?token=leak#frag")
	return req
}

// 成功访问：记一行，且只有脱敏字段。
func TestSharePublicGet_RecordsSanitizedAccess(t *testing.T) {
	rec := newAccessRecorder()
	h := &ShareHandler{store: okShareStore(), access: rec.writer()}
	w := httptest.NewRecorder()

	h.PublicGet(w, accessRequest())

	if w.Code != http.StatusOK {
		t.Fatalf("有效分享应为 200, got %d", w.Code)
	}
	e := rec.next(t)
	if e.ShareID != "tok123" {
		t.Fatalf("记录的 share_id 不符: %q", e.ShareID)
	}
	// IP 只存哈希：不能等于明文，且是 64 位十六进制摘要
	if e.IPHash == "203.0.113.7" || e.IPHash == "" {
		t.Fatalf("IP 必须哈希后落库, got %q", e.IPHash)
	}
	if len(e.IPHash) != 64 || strings.Trim(e.IPHash, "0123456789abcdef") != "" {
		t.Fatalf("IP 哈希应是 sha256 十六进制, got %q", e.IPHash)
	}
	if _, err := hex.DecodeString(e.IPHash); err != nil {
		t.Fatalf("IP 哈希不是合法十六进制: %v", err)
	}
	if e.UAClass != "desktop" {
		t.Fatalf("桌面 UA 应归为 desktop, got %q", e.UAClass)
	}
	// Referer 只留主机名：路径 / 查询串 / 锚点都不能进库
	if e.RefererHost != "news.example.com" {
		t.Fatalf("Referer 只应保留主机名, got %q", e.RefererHost)
	}
	if strings.Contains(e.RefererHost, "token=leak") || strings.Contains(e.RefererHost, "/a/b") {
		t.Fatalf("Referer 的路径或查询串泄露进统计: %q", e.RefererHost)
	}
}

// 统计不能阻塞匿名首屏：写库卡住时响应仍须先返回。
func TestSharePublicGet_RecordDoesNotBlockResponse(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{}, 1)
	w := httptest.NewRecorder()
	h := &ShareHandler{store: okShareStore(), access: func(context.Context, shareAccessEntry) {
		started <- struct{}{}
		<-release // 模拟写库卡住
	}}

	done := make(chan struct{})
	go func() {
		h.PublicGet(w, publicShareRequest())
		close(done)
	}()

	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("访问统计阻塞了响应")
	}
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("统计写入没有启动")
	}
	close(release)
	if w.Code != http.StatusOK {
		t.Fatalf("应为 200, got %d", w.Code)
	}
}

// 失败分支不能记成"有人看过"：否则被扫的链接会让限额瞬间耗尽、统计变成攻击面。
func TestSharePublicGet_NoAccessRecordOnFailure(t *testing.T) {
	now := time.Now()
	bcryptHash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	cases := map[string]*fakeShareStore{
		"行不存在": {row: shareRowData{err: errNoRows()}.row()},
		"已撤销": {row: shareRowData{
			id: "tok123", createdAt: now, revokedAt: &now, expiresAt: futureTime(),
		}.row()},
		"已过期": {row: shareRowData{
			id: "tok123", createdAt: now, expiresAt: timePtr(now.Add(-time.Hour)),
		}.row()},
		"缺密码": {row: shareRowData{
			id: "tok123", createdAt: now, expiresAt: futureTime(), passwordHash: string(bcryptHash),
		}.row()},
	}
	for name, store := range cases {
		t.Run(name, func(t *testing.T) {
			rec := newAccessRecorder()
			h := &ShareHandler{store: store, access: rec.writer()}
			h.PublicGet(httptest.NewRecorder(), accessRequest())

			select {
			case e := <-rec.got:
				t.Fatalf("失败分支不应写访问记录, got %+v", e)
			case <-time.After(150 * time.Millisecond):
			}
		})
	}
}

// ── 密码保护 ─────────────────────────────────────────────────────────

// 摘要里不能出现明文（也不该可逆）。
func TestSharePasswordHash_NotPlaintext(t *testing.T) {
	const pw = "correct horse battery"
	hash, err := hashSharePassword(pw)
	if err != nil {
		t.Fatalf("hashSharePassword: %v", err)
	}
	if strings.Contains(hash, pw) {
		t.Fatalf("摘要里出现了明文密码: %q", hash)
	}
	if !strings.HasPrefix(hash, "$2") {
		t.Fatalf("应为 bcrypt 摘要, got %q", hash)
	}
	if !sharePasswordMatches(hash, pw) {
		t.Fatal("正确密码应校验通过")
	}
	if sharePasswordMatches(hash, "wrong") {
		t.Fatal("错误密码不应校验通过")
	}
	if sharePasswordMatches("", pw) {
		t.Fatal("未设密码时不应视为通过（调用方据此跳过校验）")
	}
}

func passwordShareStore(t *testing.T, hash string) *fakeShareStore {
	t.Helper()
	return &fakeShareStore{row: shareRowData{
		id: "tok123", sessionID: "s1", title: "T", messageIDs: []string{"m1"},
		createdAt: time.Now().Add(-time.Minute), expiresAt: futureTime(), passwordHash: hash,
	}.row()}
}

// 缺密码 / 密码错 → 401 + share_password_required（不是 403，也不是 auth_required）。
func TestSharePublicGet_PasswordRequiredIs401(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	for name, header := range map[string]string{"未提供": "", "错误": "not-it"} {
		t.Run(name, func(t *testing.T) {
			h := &ShareHandler{store: passwordShareStore(t, string(hash))}
			req := accessRequest()
			if header != "" {
				req.Header.Set(sharePasswordHeader, header)
			}
			rec := httptest.NewRecorder()

			h.PublicGet(rec, req)

			if rec.Code != http.StatusUnauthorized {
				t.Fatalf("应为 401, got %d", rec.Code)
			}
			if resp := decodeShareResponse(t, rec); resp.Code != CodeSharePasswordRequired {
				t.Fatalf("code 应为 %q, got %q", CodeSharePasswordRequired, resp.Code)
			}
			// 响应体不得回显任何密码片段（包括错误的那个）
			if header != "" && strings.Contains(strings.ToLower(rec.Body.String()), strings.ToLower(header)) {
				t.Fatalf("响应体回显了提交的密码: %s", rec.Body.String())
			}
		})
	}
}

// 密码正确 → 200，并照常记一次访问。
func TestSharePublicGet_PasswordCorrectIs200(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	rec := newAccessRecorder()
	h := &ShareHandler{store: passwordShareStore(t, string(hash)), access: rec.writer()}
	req := accessRequest()
	req.Header.Set(sharePasswordHeader, "s3cret")
	w := httptest.NewRecorder()

	h.PublicGet(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("正确密码应为 200, got %d (body=%s)", w.Code, w.Body.String())
	}
	if e := rec.next(t); e.ShareID != "tok123" {
		t.Fatalf("记录不符: %+v", e)
	}
}

// ── 访问次数上限 ─────────────────────────────────────────────────────

// views 达到 max_views → 410 + share_view_limit；未达到照常 200。
func TestSharePublicGet_ViewLimit(t *testing.T) {
	limit := 2
	base := shareRowData{
		id: "tok123", sessionID: "s1", title: "T", messageIDs: []string{"m1"},
		createdAt: time.Now().Add(-time.Minute), expiresAt: futureTime(), maxViews: &limit,
	}

	reached := base
	reached.views = 2
	rec := httptest.NewRecorder()
	h := &ShareHandler{store: &fakeShareStore{row: reached.row()}}
	h.PublicGet(rec, publicShareRequest())
	if rec.Code != http.StatusGone {
		t.Fatalf("达到上限应为 410, got %d", rec.Code)
	}
	resp := decodeShareResponse(t, rec)
	if resp.Code != CodeShareViewLimit {
		t.Fatalf("code 应为 %q, got %q", CodeShareViewLimit, resp.Code)
	}

	below := base
	below.views = 1
	rec2 := httptest.NewRecorder()
	h2 := &ShareHandler{store: &fakeShareStore{row: below.row()}}
	h2.PublicGet(rec2, publicShareRequest())
	if rec2.Code != http.StatusOK {
		t.Fatalf("未达上限应为 200, got %d", rec2.Code)
	}

	// 不限次（NULL）时 views 再大也不拦
	unlimited := base
	unlimited.maxViews = nil
	unlimited.views = 10_000
	rec3 := httptest.NewRecorder()
	h3 := &ShareHandler{store: &fakeShareStore{row: unlimited.row()}}
	h3.PublicGet(rec3, publicShareRequest())
	if rec3.Code != http.StatusOK {
		t.Fatalf("未设上限不应拦截, got %d", rec3.Code)
	}
}

// ── 统计聚合 ─────────────────────────────────────────────────────────

// 趋势必须是固定长度：缺的日期补 0（否则前端 sparkline 每条长度不同，画歪）。
func TestShareDailyBuckets_FillsMissingDays(t *testing.T) {
	now := time.Date(2026, 11, 20, 13, 45, 0, 0, time.UTC)
	counts := map[string]int64{"2026-11-20": 3, "2026-11-18": 1}

	got := shareDailyBuckets(counts, now, 7)

	if len(got) != 7 {
		t.Fatalf("应铺满 7 天, got %d", len(got))
	}
	if got[0].Date != "2026-11-14" || got[6].Date != "2026-11-20" {
		t.Fatalf("窗口应为最近 7 天（含今天）: %s .. %s", got[0].Date, got[6].Date)
	}
	byDate := map[string]int64{}
	for _, d := range got {
		byDate[d.Date] = d.Count
	}
	if byDate["2026-11-20"] != 3 || byDate["2026-11-18"] != 1 {
		t.Fatalf("计数丢失: %+v", byDate)
	}
	if byDate["2026-11-19"] != 0 {
		t.Fatalf("缺的日期应补 0, got %d", byDate["2026-11-19"])
	}
	// 空 map 也要给满长度
	if n := len(shareDailyBuckets(nil, now, 7)); n != 7 {
		t.Fatalf("空计数也应返回 7 个点, got %d", n)
	}
}

// ── 批量撤销 ─────────────────────────────────────────────────────────

func tenantRequest(method, target string) *http.Request {
	req := httptest.NewRequest(method, target, nil)
	return req.WithContext(context.WithValue(req.Context(), CtxKeyTenantID, int16(1)))
}

// 没有租户上下文 = 未鉴权：批量的"权限"首先体现在这里（平台写权限由路由中间件保证）。
func TestAdminRevokeShares_RequiresTenant(t *testing.T) {
	h := &AdminHandler{}
	req := httptest.NewRequest(http.MethodDelete, "/v1/admin/shares?ids=a,b", nil)
	rec := httptest.NewRecorder()

	h.RevokeShares(rec, req)

	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("缺租户上下文应为 401, got %d", rec.Code)
	}
}

func TestAdminRevokeShares_ValidatesIDs(t *testing.T) {
	h := &AdminHandler{}
	// 201 个**互不相同**的 id：去重逻辑会把重复 id 合并，重复串测不到上限。
	many := make([]string, 0, 201)
	for i := 0; i <= 200; i++ {
		many = append(many, "id"+strconv.Itoa(i))
	}
	cases := map[string]string{
		"缺 ids":  "",
		"只有空白":   " , ,, ",
		"超过 200": strings.Join(many, ","),
	}
	for name, raw := range cases {
		t.Run(name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.RevokeShares(rec, tenantRequest(http.MethodDelete, "/v1/admin/shares?ids="+url.QueryEscape(raw)))
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("应拒为 400, got %d (body=%s)", rec.Code, rec.Body.String())
			}
		})
	}
}

// 部分失败：只有命中且未撤销的那条被撤，其余进 failed，HTTP 仍是 200。
func TestAdminRevokeShares_PartialFailure(t *testing.T) {
	store := &fakeShareStore{rows: &shareFakeRows{rows: [][]any{
		{"ok-1", "u1", "s1", "标题"},
	}}}
	h := &AdminHandler{shares: store}
	rec := httptest.NewRecorder()

	h.RevokeShares(rec, tenantRequest(http.MethodDelete, "/v1/admin/shares?ids=ok-1,gone-1,ok-1"))

	if rec.Code != http.StatusOK {
		t.Fatalf("部分失败仍应是 200, got %d (body=%s)", rec.Code, rec.Body.String())
	}
	var resp struct {
		Success bool                 `json:"success"`
		Data    AdminShareBulkResult `json:"data"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if !resp.Success {
		t.Fatalf("应成功: %s", rec.Body.String())
	}
	// 去重后 requested 是 2（ok-1 出现两次）
	if resp.Data.Requested != 2 || resp.Data.Revoked != 1 {
		t.Fatalf("汇总不符: %+v", resp.Data)
	}
	if len(resp.Data.Failed) != 1 || resp.Data.Failed[0].ID != "gone-1" {
		t.Fatalf("失败清单不符: %+v", resp.Data.Failed)
	}
	if len(resp.Data.RevokedIDs) != 1 || resp.Data.RevokedIDs[0] != "ok-1" {
		t.Fatalf("撤销清单不符: %+v", resp.Data.RevokedIDs)
	}
}

func TestSplitShareIDs(t *testing.T) {
	got := splitShareIDs(" a , b,,a ,c ")
	want := []string{"a", "b", "c"}
	if len(got) != len(want) {
		t.Fatalf("want %v, got %v", want, got)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("want %v, got %v", want, got)
		}
	}
}

// ── 后台列表的访问统计 ───────────────────────────────────────────────

// 列表必须带出 24h / 累计 / 最近访问与 7 天趋势，且链接前缀是 /s/。
func TestAdminListShares_IncludesViewStats(t *testing.T) {
	now := time.Now().UTC()
	viewed := now.Add(-2 * time.Hour)
	today := now.Format("2006-01-02")

	store := &fakeShareStore{rowSets: []pgx.Rows{
		&shareFakeRows{rows: [][]any{
			{"shr1", "ses1", "u1", "ops@example.com", "标题", 3,
				now.Add(-time.Hour), now.Add(24 * time.Hour), int64(4), int64(9), &viewed},
		}},
		// 趋势：只有今天有 4 次，其余日期由 shareDailyBuckets 补 0
		&shareFakeRows{rows: [][]any{{"shr1", today, int64(4)}}},
	}}
	h := &AdminHandler{shares: store}
	rec := httptest.NewRecorder()

	h.ListShares(rec, tenantRequest(http.MethodGet, "/v1/admin/shares"))

	if rec.Code != http.StatusOK {
		t.Fatalf("应为 200, got %d (body=%s)", rec.Code, rec.Body.String())
	}
	var resp struct {
		Data struct {
			Items []AdminShareItem `json:"items"`
		} `json:"data"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&resp); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if len(resp.Data.Items) != 1 {
		t.Fatalf("应有一行, got %d", len(resp.Data.Items))
	}
	it := resp.Data.Items[0]
	if it.Views24h != 4 || it.ViewsTotal != 9 {
		t.Fatalf("访问统计不符: 24h=%d total=%d", it.Views24h, it.ViewsTotal)
	}
	if it.LastViewedAt == nil {
		t.Fatal("last_viewed_at 应被带出")
	}
	if len(it.Daily) != 7 {
		t.Fatalf("趋势应铺满 7 天, got %d", len(it.Daily))
	}
	if it.Daily[6].Count != 4 || it.Daily[0].Count != 0 {
		t.Fatalf("趋势计数不符: first=%d last=%d", it.Daily[0].Count, it.Daily[6].Count)
	}
	if !strings.HasSuffix(it.URL, "/s/shr1") {
		t.Fatalf("后台链接也必须是 /s/ 前缀, got %q", it.URL)
	}
}

// ── 密码失败短锁（L07）──────────────────────────────────────────────

// fakeShareGuard 用内存模拟 Redis 计数，避免单测依赖真实 Redis。
type fakeShareGuard struct {
	mu        sync.Mutex
	fails     map[string]int
	lockedFor map[string]time.Duration
	max       int
	lock      time.Duration
}

func newFakeShareGuard(max int, lock time.Duration) *fakeShareGuard {
	return &fakeShareGuard{fails: map[string]int{}, lockedFor: map[string]time.Duration{}, max: max, lock: lock}
}

func shareGuardKey(shareID, ipHash string) string { return shareID + "|" + ipHash }

func (g *fakeShareGuard) locked(_ context.Context, shareID, ipHash string) (time.Duration, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	d, ok := g.lockedFor[shareGuardKey(shareID, ipHash)]
	return d, ok
}

func (g *fakeShareGuard) recordFailure(_ context.Context, shareID, ipHash string) (time.Duration, bool) {
	g.mu.Lock()
	defer g.mu.Unlock()
	k := shareGuardKey(shareID, ipHash)
	g.fails[k]++
	if g.fails[k] >= g.max {
		delete(g.fails, k)
		g.lockedFor[k] = g.lock
		return g.lock, true
	}
	return 0, false
}

func (g *fakeShareGuard) clearFailures(_ context.Context, shareID, ipHash string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	delete(g.fails, shareGuardKey(shareID, ipHash))
}

func (g *fakeShareGuard) failureCount(shareID, ipHash string) int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.fails[shareGuardKey(shareID, ipHash)]
}

// 连续错误达到阈值 → 429 + share_locked + Retry-After；锁定期内正确密码也不放行。
func TestSharePublicGet_PasswordFailuresTripShortLock(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	guard := newFakeShareGuard(sharePasswordMaxFailures, sharePasswordLockTTL)
	h := &ShareHandler{store: passwordShareStore(t, string(hash)), passwordGuard: guard}

	wrong := func() *httptest.ResponseRecorder {
		req := accessRequest()
		req.Header.Set(sharePasswordHeader, "not-it")
		rec := httptest.NewRecorder()
		h.PublicGet(rec, req)
		return rec
	}

	for i := 1; i < sharePasswordMaxFailures; i++ {
		if rec := wrong(); rec.Code != http.StatusUnauthorized {
			t.Fatalf("第 %d 次错误应为 401, got %d", i, rec.Code)
		}
	}
	rec := wrong()
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("达到阈值应为 429, got %d (body=%s)", rec.Code, rec.Body.String())
	}
	if resp := decodeShareResponse(t, rec); resp.Code != CodeShareLocked {
		t.Fatalf("code 应为 %q, got %q", CodeShareLocked, resp.Code)
	}
	if got := rec.Header().Get("Retry-After"); got == "" || got == "0" {
		t.Fatalf("429 必须带 Retry-After，got %q", got)
	}

	// 锁定期内即使密码正确也拒绝：否则短锁只挡住了错误尝试，挡不住脚本重试。
	req := accessRequest()
	req.Header.Set(sharePasswordHeader, "s3cret")
	locked := httptest.NewRecorder()
	h.PublicGet(locked, req)
	if locked.Code != http.StatusTooManyRequests {
		t.Fatalf("锁定期内正确密码也应为 429, got %d", locked.Code)
	}
}

// 未提供密码（首次打开）不计失败：否则正常访客刷新几次就被锁。
func TestSharePublicGet_MissingPasswordDoesNotCount(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	guard := newFakeShareGuard(sharePasswordMaxFailures, sharePasswordLockTTL)
	h := &ShareHandler{store: passwordShareStore(t, string(hash)), passwordGuard: guard}

	for i := 0; i < sharePasswordMaxFailures+1; i++ {
		rec := httptest.NewRecorder()
		h.PublicGet(rec, accessRequest())
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("未提供密码应为 401, got %d", rec.Code)
		}
	}
	if n := guard.failureCount("tok123", hashShareIP("203.0.113.7")); n != 0 {
		t.Fatalf("未提供密码不应计数，got %d", n)
	}
}

// 密码正确后清空失败计数：之前输错 4 次不该在下次成功后被"记仇"。
func TestSharePublicGet_PasswordSuccessClearsFailures(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("s3cret"), bcrypt.MinCost)
	if err != nil {
		t.Fatalf("bcrypt: %v", err)
	}
	guard := newFakeShareGuard(sharePasswordMaxFailures, sharePasswordLockTTL)
	h := &ShareHandler{store: passwordShareStore(t, string(hash)), passwordGuard: guard}

	bad := accessRequest()
	bad.Header.Set(sharePasswordHeader, "not-it")
	h.PublicGet(httptest.NewRecorder(), bad)
	if n := guard.failureCount("tok123", hashShareIP("203.0.113.7")); n != 1 {
		t.Fatalf("错误密码应记 1 次，got %d", n)
	}

	good := accessRequest()
	good.Header.Set(sharePasswordHeader, "s3cret")
	goodRec := httptest.NewRecorder()
	h.PublicGet(goodRec, good)
	if goodRec.Code != http.StatusOK {
		t.Fatalf("正确密码应为 200, got %d", goodRec.Code)
	}
	if n := guard.failureCount("tok123", hashShareIP("203.0.113.7")); n != 0 {
		t.Fatalf("正确密码后应清空计数，got %d", n)
	}
}
