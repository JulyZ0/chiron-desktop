package api

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"

	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/market"
)

// P1-2：技能升级 / 触发策略开关的网关侧接线。
//
// 三件事必须钉住，它们各自都是"看起来通了、实际没通"的高发点：
//
//  1. **市场安装要写来源**（origin）—— 不写的话技能列表分不清"市场装的 / 本地传的"，
//     升级端点也永远找不到上游（它只对 market: 前缀生效）。
//  2. **升级/元数据开关的身份走 query** —— 引擎这两个端点从 query 读 user_id/tenant_id
//     （FastAPI 把非模型参数当 query），走 proxy 那套"身份塞 body"会让身份静默丢失，
//     引擎回退到全局共享层，等于改到"所有人"那一层去。
//  3. **引擎的 409 要原样透出** —— 「没有升级来源」是可操作的稳定结论，
//     折成 500 会让前端只能显示"服务器错误"。

// engineProbe 记录假引擎收到的方法 / URI / body。
type engineProbe struct {
	method string
	uri    string
	body   []byte
}

func newEngineProbe(t *testing.T, reply string, status int) (*httptest.Server, *engineProbe) {
	t.Helper()
	probe := &engineProbe{}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		probe.method = r.Method
		probe.uri = r.RequestURI
		probe.body, _ = io.ReadAll(r.Body)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = w.Write([]byte(reply))
	}))
	t.Cleanup(srv.Close)
	return srv, probe
}

func TestSkillMarketInstallerSendsOrigin(t *testing.T) {
	srv, probe := newEngineProbe(t, `{"skill":{"name":"demo-skill"},"scope":"user"}`, http.StatusOK)
	inst := skillMarketInstaller{py: engine.NewPythonClient(srv.URL)}

	item := market.Item{
		Kind:    market.KindSkill,
		ID:      "skill:demo",
		Name:    "demo",
		Source:  "builtin",
		Payload: json.RawMessage(`{"name":"demo-skill","description":"x"}`),
	}

	ref, err := inst.Install(claimsCtx(), item, nil)
	if err != nil {
		t.Fatalf("安装失败：%v", err)
	}

	var sent map[string]any
	if err := json.Unmarshal(probe.body, &sent); err != nil {
		t.Fatalf("请求体不是 JSON：%v; body=%s", err, probe.body)
	}
	if got := sent["origin"]; got != "market:builtin/skill:demo" {
		t.Fatalf("origin = %v, want market:builtin/skill:demo（装完必须能分清来源）", got)
	}
	// 落点引用里也带上 origin：卸载/排障时能反查"当初从哪装的"
	if ref["origin"] != "market:builtin/skill:demo" {
		t.Fatalf("ref.origin = %v, want market:builtin/skill:demo", ref["origin"])
	}
}

func TestSkillUpgradeForwardsIdentityViaQuery(t *testing.T) {
	srv, probe := newEngineProbe(t, `{"upgraded":true,"from_version":"1.0.0","to_version":"1.1.0"}`, http.StatusOK)
	h := NewSkillHandler(engine.NewPythonClient(srv.URL))

	req := httptest.NewRequest(http.MethodPost, "/v1/skills/demo-skill/upgrade", strings.NewReader(`{}`))
	req.SetPathValue("name", "demo-skill")
	req = req.WithContext(claimsCtx())
	rec := httptest.NewRecorder()

	h.upgrade(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if probe.method != http.MethodPost {
		t.Fatalf("engine method = %s, want POST", probe.method)
	}
	i := strings.IndexByte(probe.uri, '?')
	if i < 0 {
		t.Fatalf("升级请求没有带身份 query：%s", probe.uri)
	}
	if !strings.HasSuffix(probe.uri[:i], "/v1/skills/demo-skill/upgrade") {
		t.Fatalf("转发路径不对：%s", probe.uri)
	}
	q, err := url.ParseQuery(probe.uri[i+1:])
	if err != nil {
		t.Fatalf("query 解析失败：%v; uri=%s", err, probe.uri)
	}
	if q.Get("user_id") != "u1" || q.Get("tenant_id") != "1" {
		t.Fatalf("身份没经 query 下发：%s（引擎从 query 读，写 body 会被丢弃）", probe.uri)
	}
}

func TestSkillUpgradeMapsEngineConflict(t *testing.T) {
	body := `{"detail":{"reason":"no_source","error":"no upgrade source"}}`
	srv, _ := newEngineProbe(t, body, http.StatusConflict)
	h := NewSkillHandler(engine.NewPythonClient(srv.URL))

	req := httptest.NewRequest(http.MethodPost, "/v1/skills/demo-skill/upgrade", strings.NewReader(`{}`))
	req.SetPathValue("name", "demo-skill")
	req = req.WithContext(claimsCtx())
	rec := httptest.NewRecorder()

	h.upgrade(rec, req)

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409（引擎的稳定结论要透出，不能折成 500）; body=%s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "no_source") {
		t.Fatalf("响应里要带上引擎给的原因：%s", rec.Body.String())
	}
}

func TestSkillPatchMetaUsesPatchMethod(t *testing.T) {
	srv, probe := newEngineProbe(t, `{"skill":{"name":"demo-skill","auto_inject":false}}`, http.StatusOK)
	h := NewSkillHandler(engine.NewPythonClient(srv.URL))

	req := httptest.NewRequest(http.MethodPatch, "/v1/skills/demo-skill/meta",
		strings.NewReader(`{"auto_inject":false}`))
	req.SetPathValue("name", "demo-skill")
	req = req.WithContext(claimsCtx())
	rec := httptest.NewRecorder()

	h.patchMeta(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", rec.Code, rec.Body.String())
	}
	if probe.method != http.MethodPatch {
		t.Fatalf("engine method = %s, want PATCH", probe.method)
	}
	if !strings.Contains(probe.uri, "/v1/skills/demo-skill/meta") {
		t.Fatalf("转发路径不对：%s", probe.uri)
	}
	var sent map[string]any
	if err := json.Unmarshal(probe.body, &sent); err != nil {
		t.Fatalf("请求体不是 JSON：%v", err)
	}
	if sent["auto_inject"] != false {
		t.Fatalf("auto_inject 没被转发：%v", sent)
	}
}

func TestSkillUpgradeRejectsInvalidName(t *testing.T) {
	h := NewSkillHandler(nil) // 引擎不可用也要先校验名字
	req := httptest.NewRequest(http.MethodPost, "/v1/skills/../evil/upgrade", strings.NewReader(`{}`))
	req.SetPathValue("name", "../evil")
	req = req.WithContext(claimsCtx())
	rec := httptest.NewRecorder()

	h.upgrade(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400（非法技能名不该被转发）; body=%s", rec.Code, rec.Body.String())
	}
}
