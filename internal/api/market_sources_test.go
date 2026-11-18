package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/market"
)

// configForTest 返回一份"env 全开"的配置，用于验证 DB 覆盖/回落逻辑。
// 直接构造结构体而不读环境变量：读 env 会让测试结果依赖运行环境。
func configForTest() *config.Config {
	return &config.Config{
		MarketRemoteEnabled:    true,
		MarketCatalogSources:   []string{"https://env.example.com/c.json"},
		MarketAllowUserSources: true,
		MarketAllowRemoteMCP:   true,
		MarketMaxSources:       20,
	}
}

// newSourceTestHandler 构造一个带用户源存储、可注入配置的 handler。
func newSourceTestHandler(t *testing.T, cfg market.Config) *MarketHandler {
	t.Helper()
	svc := market.NewService(nil)
	svc.Configure(cfg)
	svc.SetUserStore(market.NewFileSourceStore(filepath.Join(t.TempDir(), "market")))
	return NewMarketHandler(svc)
}

// withUser 给请求挂上认证 claims（authMW 在真实链路里做的事）。
func withUser(r *http.Request, userID string) *http.Request {
	return r.WithContext(auth.WithClaims(r.Context(), &auth.Claims{UserID: userID}))
}

func decodeSources(t *testing.T, rec *httptest.ResponseRecorder) sourcesResponse {
	t.Helper()
	var got sourcesResponse
	decodeOK(t, rec, &got)
	return got
}

// GET /v1/market/sources：内置源恒在列表里，limits 必须带上（前端据它决定
// 要不要显示添加按钮、上限是多少）。
func TestMarketSourcesListIncludesBuiltinAndLimits(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{MaxSources: 20})

	rec := httptest.NewRecorder()
	h.sources.List(rec, withUser(httptest.NewRequest(http.MethodGet, "/v1/market/sources", nil), "u1"))
	got := decodeSources(t, rec)

	if len(got.Sources) == 0 || got.Sources[0].ID != market.SourceBuiltin {
		t.Fatalf("sources = %+v, want builtin 在首位", got.Sources)
	}
	if got.Limits.MaxSources != 20 {
		t.Errorf("max_sources = %d, want 20", got.Limits.MaxSources)
	}
	if got.Limits.AllowUserSources {
		t.Error("初装形态 allow_user_sources 必须为 false")
	}
	if got.Limits.RemoteEnabled {
		t.Error("初装形态 remote_enabled 必须为 false")
	}
}

// remote_enabled=false 时，**已配置的**源仍要出现在列表里（Enabled=false），
// 否则用户在设置页看不到自己配的源，会以为没保存成功。
func TestMarketSourcesListShowsDisabledSources(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{
		RemoteEnabled:   false,
		OperatorSources: []string{"https://op.example.com/c.json"},
	})

	rec := httptest.NewRecorder()
	h.sources.List(rec, withUser(httptest.NewRequest(http.MethodGet, "/v1/market/sources", nil), "u1"))
	got := decodeSources(t, rec)

	var found *market.Source
	for i := range got.Sources {
		if got.Sources[i].Type == market.SourceTypeOperator {
			found = &got.Sources[i]
		}
	}
	if found == nil {
		t.Fatalf("运维源未出现在列表里: %+v", got.Sources)
	}
	if found.Enabled {
		t.Error("remote_enabled=false 时外部源应标 Enabled=false（存在但不参与拉取）")
	}
}

// allow_user_sources=false 时必须 403 —— 用户级源把 SSRF 面变成自助入口，
// 默认关闭是产品决策，不能被前端一条 POST 绕开。
func TestMarketSourceCreateForbiddenWhenDisabled(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: false})

	body := strings.NewReader(`{"url":"https://a.example.com/c.json"}`)
	req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources", body), "u1")
	rec := httptest.NewRecorder()
	h.sources.Create(rec, req)

	if rec.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", rec.Code, rec.Body.String())
	}
}

// URL 形状不合法 → 400（而不是 500，也不是静默接受）。
func TestMarketSourceCreateRejectsInvalidURL(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: true})

	for _, raw := range []string{"", "http://plain.example.com/c.json", "https://u:p@a.example.com/c.json"} {
		payload, _ := json.Marshal(map[string]string{"url": raw})
		req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources", strings.NewReader(string(payload))), "u1")
		rec := httptest.NewRecorder()
		h.sources.Create(rec, req)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("url=%q status = %d, want 400; body=%s", raw, rec.Code, rec.Body.String())
		}
	}
}

// 成功路径 201，并返回新建的源（前端据此直接插进列表，无需再拉一次）。
func TestMarketSourceCreateCreated(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: true})

	body := strings.NewReader(`{"url":"https://a.example.com/c.json","name":"我的源"}`)
	req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources", body), "u1")
	rec := httptest.NewRecorder()
	h.sources.Create(rec, req)

	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201; body=%s", rec.Code, rec.Body.String())
	}
	var resp struct {
		Success bool          `json:"success"`
		Data    market.Source `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("解析响应失败: %v", err)
	}
	if !resp.Success || resp.Data.Type != market.SourceTypeUser {
		t.Fatalf("resp = %+v", resp)
	}
	if resp.Data.Name != "我的源" {
		t.Errorf("name = %q, want 我的源", resp.Data.Name)
	}
	if resp.Data.ID != market.SourceIDFromURL("https://a.example.com/c.json") {
		t.Errorf("id = %q", resp.Data.ID)
	}
}

// 源总数达上限 → 409（不是 400：这不是用户输入错误，得先删掉一个才能加）。
func TestMarketSourceCreateConflictAtLimit(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: true, MaxSources: 1})

	// 第一条占满唯一的额度。
	req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources",
		strings.NewReader(`{"url":"https://a.example.com/c.json"}`)), "u1")
	rec := httptest.NewRecorder()
	h.sources.Create(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("第一条应成功，status = %d; body=%s", rec.Code, rec.Body.String())
	}

	req = withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources",
		strings.NewReader(`{"url":"https://b.example.com/c.json"}`)), "u1")
	rec = httptest.NewRecorder()
	h.sources.Create(rec, req)
	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%s", rec.Code, rec.Body.String())
	}
}

// 20 是硬上限：max_sources 填 1000 也只能有 20 个外部源。
func TestMarketSourceHardCapIsRespected(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: true, MaxSources: 1000})

	for i := 0; i < market.DefaultMaxSources; i++ {
		url := "https://s" + string(rune('a'+i%26)) + string(rune('a'+i/26)) + ".example.com/c.json"
		payload, _ := json.Marshal(map[string]string{"url": url})
		req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources", strings.NewReader(string(payload))), "u1")
		rec := httptest.NewRecorder()
		h.sources.Create(rec, req)
		if rec.Code != http.StatusCreated {
			t.Fatalf("第 %d 个源应成功，status = %d; body=%s", i+1, rec.Code, rec.Body.String())
		}
	}

	payload, _ := json.Marshal(map[string]string{"url": "https://overflow.example.com/c.json"})
	req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources", strings.NewReader(string(payload))), "u1")
	rec := httptest.NewRecorder()
	h.sources.Create(rec, req)
	if rec.Code != http.StatusConflict {
		t.Fatalf("第 %d 个源应被拒绝，status = %d; body=%s", market.DefaultMaxSources+1, rec.Code, rec.Body.String())
	}
}

// 重复添加同一 URL 是幂等的：不占额度、不产生第二个 Tab。
func TestMarketSourceCreateIsIdempotentOnSameURL(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: true, MaxSources: 1})

	for i := 0; i < 2; i++ {
		req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources",
			strings.NewReader(`{"url":"https://a.example.com/c.json"}`)), "u1")
		rec := httptest.NewRecorder()
		h.sources.Create(rec, req)
		if rec.Code != http.StatusCreated {
			t.Fatalf("第 %d 次应成功，status = %d; body=%s", i+1, rec.Code, rec.Body.String())
		}
	}

	rec := httptest.NewRecorder()
	h.sources.List(rec, withUser(httptest.NewRequest(http.MethodGet, "/v1/market/sources", nil), "u1"))
	got := decodeSources(t, rec)
	userCount := 0
	for _, s := range got.Sources {
		if s.Type == market.SourceTypeUser {
			userCount++
		}
	}
	if userCount != 1 {
		t.Errorf("用户源数 = %d, want 1", userCount)
	}
}

// 用户只能删自己的 user 源；内置/运维源 → 403。
func TestMarketSourceDeleteOnlyUserSources(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{
		AllowUserSources: true,
		OperatorSources:  []string{"https://op.example.com/c.json"},
	})

	// 加一条用户源再删掉：应 200。
	req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources",
		strings.NewReader(`{"url":"https://a.example.com/c.json"}`)), "u1")
	rec := httptest.NewRecorder()
	h.sources.Create(rec, req)
	var created struct {
		Data market.Source `json:"data"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &created)

	del := withUser(httptest.NewRequest(http.MethodDelete, "/v1/market/sources/"+created.Data.ID, nil), "u1")
	del.SetPathValue("id", created.Data.ID)
	rec = httptest.NewRecorder()
	h.sources.Delete(rec, del)
	if rec.Code != http.StatusOK {
		t.Fatalf("删用户源 status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}

	// 删内置源 → 403。
	del = withUser(httptest.NewRequest(http.MethodDelete, "/v1/market/sources/"+market.SourceBuiltin, nil), "u1")
	del.SetPathValue("id", market.SourceBuiltin)
	rec = httptest.NewRecorder()
	h.sources.Delete(rec, del)
	if rec.Code != http.StatusForbidden {
		t.Errorf("删内置源 status = %d, want 403; body=%s", rec.Code, rec.Body.String())
	}

	// 删运维源 → 403。
	opID := market.SourceIDFromURL("https://op.example.com/c.json")
	del = withUser(httptest.NewRequest(http.MethodDelete, "/v1/market/sources/"+opID, nil), "u1")
	del.SetPathValue("id", opID)
	rec = httptest.NewRecorder()
	h.sources.Delete(rec, del)
	if rec.Code != http.StatusForbidden {
		t.Errorf("删运维源 status = %d, want 403; body=%s", rec.Code, rec.Body.String())
	}
}

// 删除是幂等的：删一个从不存在的 id 也返回 200。
func TestMarketSourceDeleteUnknownIsOK(t *testing.T) {
	h := newSourceTestHandler(t, market.Config{AllowUserSources: true})

	req := withUser(httptest.NewRequest(http.MethodDelete, "/v1/market/sources/src_nope", nil), "u1")
	req.SetPathValue("id", "src_nope")
	rec := httptest.NewRecorder()
	h.sources.Delete(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
}

// 目录读取按用户加载：A 用户加的源不会出现在 B 用户的目录里。
func TestMarketCatalogUsesPerUserSources(t *testing.T) {
	svc := market.NewService(nil)
	svc.Configure(market.Config{AllowUserSources: true, RemoteEnabled: true})
	svc.SetUserStore(market.NewFileSourceStore(filepath.Join(t.TempDir(), "market")))
	h := NewMarketHandler(svc)

	req := withUser(httptest.NewRequest(http.MethodPost, "/v1/market/sources",
		strings.NewReader(`{"url":"https://alice.example.com/c.json"}`)), "alice")
	rec := httptest.NewRecorder()
	h.sources.Create(rec, req)
	if rec.Code != http.StatusCreated {
		t.Fatalf("alice 加源失败: status = %d; body=%s", rec.Code, rec.Body.String())
	}

	// alice 的目录里应有这个源（尽管抓取层未注入，源仍列出）。
	rec = httptest.NewRecorder()
	h.Catalog(rec, withUser(httptest.NewRequest(http.MethodGet, "/v1/market/catalog", nil), "alice"))
	var alice catalogResponse
	decodeOK(t, rec, &alice)
	if !hasSourceType(alice.Sources, market.SourceTypeUser) {
		t.Errorf("alice 的目录缺少自己的源: %+v", alice.Sources)
	}

	// bob 的目录里不应有。
	rec = httptest.NewRecorder()
	h.Catalog(rec, withUser(httptest.NewRequest(http.MethodGet, "/v1/market/catalog", nil), "bob"))
	var bob catalogResponse
	decodeOK(t, rec, &bob)
	if hasSourceType(bob.Sources, market.SourceTypeUser) {
		t.Errorf("bob 看到了别人的源: %+v", bob.Sources)
	}
}

func hasSourceType(srcs []market.Source, t market.SourceType) bool {
	for _, s := range srcs {
		if s.Type == t {
			return true
		}
	}
	return false
}

// ── 配置合并（env + DB）──

func TestBuildMarketConfigMergesDBOverrides(t *testing.T) {
	cfg := configForTest()

	m := buildMarketConfig(cfg, map[string]interface{}{
		"remote_enabled":     true,
		"catalog_sources":    []interface{}{"https://db.example.com/c.json"},
		"allow_user_sources": true,
		"allow_remote_mcp":   true,
		"max_sources":        float64(1000),
	})

	if !m.RemoteEnabled {
		t.Error("remote_enabled 应为 true")
	}
	if !m.AllowUserSources || !m.AllowRemoteMCP {
		t.Errorf("布尔覆盖未生效: %+v", m)
	}
	if len(m.OperatorSources) != 1 || m.OperatorSources[0] != "https://db.example.com/c.json" {
		t.Errorf("catalog_sources 未覆盖: %+v", m.OperatorSources)
	}
	// 上限被截到 20 —— 这是"硬上限"的关键断言。
	if m.MaxSources != market.DefaultMaxSources {
		t.Errorf("max_sources = %d, want %d", m.MaxSources, market.DefaultMaxSources)
	}
}

// DB 里没有的键保留 env 的值（"没配"不能等于"配成 false"）。
func TestBuildMarketConfigKeepsEnvWhenDBAbsent(t *testing.T) {
	cfg := configForTest()

	m := buildMarketConfig(cfg, nil)
	if !m.RemoteEnabled {
		t.Error("env 的 MARKET_REMOTE_ENABLED 应保留")
	}
	if !m.AllowUserSources {
		t.Error("env 的 MARKET_ALLOW_USER_SOURCES 应保留")
	}
}

// env 打不开的能力（remote_enabled）不能被 DB 的 false 关掉？——反过来：
// env=true 代表部署方已显式决定，DB 的 false 不该让它失效。
func TestBuildMarketConfigRemoteEnabledIsOR(t *testing.T) {
	cfg := configForTest()

	m := buildMarketConfig(cfg, map[string]interface{}{"remote_enabled": false})
	if !m.RemoteEnabled {
		t.Error("env 已开启时，DB 的 false 不应关闭外部源")
	}
}

// catalog_sources 可能是 JSON 文本（人工/脚本写入），也必须能读回。
func TestDBStringListAcceptsJSONText(t *testing.T) {
	list, ok := dbStringList(`["https://a.example.com/c.json","https://b.example.com/c.json"]`)
	if !ok || len(list) != 2 {
		t.Fatalf("list = %+v, ok = %v", list, ok)
	}
	list, ok = dbStringList("https://a.example.com/c.json, https://b.example.com/c.json")
	if !ok || len(list) != 2 {
		t.Fatalf("逗号分隔: list = %+v, ok = %v", list, ok)
	}
	if _, ok := dbStringList(struct{}{}); ok {
		t.Error("不认识的类型应返回 ok=false（让调用方保留 env）")
	}
}
