package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/market"
)

// 市场安装转发到引擎时**身份参数的拼装**。
//
// 回归背景（真实故障）：`identityQuery` 曾返回以 `?` 开头的串，而技能安装的路径
// 本身已带 `?scope=private`，于是拼出
//
//	/v1/skills/install?scope=private?user_id=u1&tenant_id=1
//
// Python 的 query 解析把这串里的 `user_id=u1` 当成 `scope` 的值，只有 `tenant_id`
// 单独成立 —— 引擎中间件看到"半个身份"就回
// `401 Incomplete gateway identity: tenant_id and user_id are both required`。
// 用户看到的是"市场里装不了技能"，而错误文案指向鉴权，离真正的拼接缺陷很远。
//
// 所以这里钉的是**线上的 query 形状**（而不是某个函数的返回值）：整体只能有一个
// 问号，且三项参数都能独立解析出来。

// fakeEngine 起一个假引擎，记录最后一次请求的 RequestURI。
func fakeEngine(t *testing.T, reply string) (*httptest.Server, *string) {
	t.Helper()
	got := new(string)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		*got = r.RequestURI
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(reply))
	}))
	t.Cleanup(srv.Close)
	return srv, got
}

// assertIdentityQuery 断言 requestURI 只带一个 `?`，并把三项参数解析出来核对。
//
// 用 `url.ParseQuery` 而不是手工切分：畸形串（双问号）在这里就会现形 ——
// 手工切分反而可能"看起来正常"，那正是这个缺陷当初能溜过去的原因。
func assertIdentityQuery(t *testing.T, requestURI, wantScope string) {
	t.Helper()
	i := strings.IndexByte(requestURI, '?')
	if i < 0 {
		t.Fatalf("请求没有带任何 query：%s", requestURI)
	}
	raw := requestURI[i+1:]
	if strings.Contains(raw, "?") {
		t.Fatalf("query 里出现第二个 '?'（前一个参数会吞掉后面的键）：%s", requestURI)
	}
	q, err := url.ParseQuery(raw)
	if err != nil {
		t.Fatalf("query 无法解析：%v; raw=%q", err, raw)
	}
	if wantScope != "" && q.Get("scope") != wantScope {
		t.Fatalf("scope = %q, want %q；uri=%s", q.Get("scope"), wantScope, requestURI)
	}
	if got := q.Get("user_id"); got != "u1" {
		t.Fatalf("user_id = %q, want u1；uri=%s", got, requestURI)
	}
	if got := q.Get("tenant_id"); got != "1" {
		t.Fatalf("tenant_id = %q, want 1；uri=%s", got, requestURI)
	}
}

func claimsCtx() context.Context {
	return auth.WithClaims(context.Background(), &auth.Claims{UserID: "u1", TenantID: 1})
}

func TestSkillInstallSendsSingleQueryWithFullIdentity(t *testing.T) {
	srv, got := fakeEngine(t, `{"skill":{"name":"demo-skill"},"scope":"user"}`)
	inst := skillMarketInstaller{py: engine.NewPythonClient(srv.URL)}

	item := market.Item{
		Kind:    market.KindSkill,
		ID:      "skill:demo",
		Name:    "demo",
		Payload: json.RawMessage(`{"name":"demo-skill","description":"x"}`),
	}

	if _, err := inst.Install(claimsCtx(), item, nil); err != nil {
		t.Fatalf("安装失败：%v", err)
	}
	// 安装默认写私有层：scope 必须保住，且不能吞掉 user_id。
	assertIdentityQuery(t, *got, "private")
	if !strings.HasSuffix(strings.SplitN(*got, "?", 2)[0], "/v1/skills/install") {
		t.Fatalf("路径不对：%s", *got)
	}
}

func TestSkillUninstallSendsIdentityQuery(t *testing.T) {
	srv, got := fakeEngine(t, `{"status":"deleted"}`)
	inst := skillMarketInstaller{py: engine.NewPythonClient(srv.URL)}

	item := market.Item{Kind: market.KindSkill, ID: "skill:demo", Name: "demo"}
	if err := inst.Uninstall(claimsCtx(), item, map[string]any{"name": "demo-skill"}); err != nil {
		t.Fatalf("卸载失败：%v", err)
	}
	assertIdentityQuery(t, *got, "")
}

func TestKnowledgeInstallSendsIdentityQuery(t *testing.T) {
	srv, got := fakeEngine(t, `{"id":"kb-1"}`)
	inst := knowledgeMarketInstaller{py: engine.NewPythonClient(srv.URL)}

	item := market.Item{
		Kind:    market.KindKnowledge,
		ID:      "knowledge:tpl",
		Name:    "模板库",
		Payload: json.RawMessage(`{"description":"d"}`),
	}
	if _, err := inst.Install(claimsCtx(), item, nil); err != nil {
		t.Fatalf("安装失败：%v", err)
	}
	assertIdentityQuery(t, *got, "")
}

func TestKnowledgeUninstallSendsIdentityQuery(t *testing.T) {
	srv, got := fakeEngine(t, `{"status":"deleted"}`)
	inst := knowledgeMarketInstaller{py: engine.NewPythonClient(srv.URL)}

	item := market.Item{Kind: market.KindKnowledge, ID: "knowledge:tpl", Name: "模板库"}
	if err := inst.Uninstall(claimsCtx(), item, map[string]any{"kb_id": "kb-1"}); err != nil {
		t.Fatalf("卸载失败：%v", err)
	}
	assertIdentityQuery(t, *got, "")
}
