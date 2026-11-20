package api

// admin_monitor_overview_test.go —— 「运行时监控」统一数据源的契约测试。
//
// 这一页最容易被写坏的地方不是"算错数"，而是**把"没采到"显示成 0**：
// 面板上 P99=0ms、错误率 0%、队列深度 0 全都"看起来正常"，而排障的人会据此
// 得出完全相反的结论。所以这里的用例主要钉三件事：
//
//  1. 没有后端（PG / Redis / 引擎）时，各区块**独立**降级，缺的字段是 null；
//  2. partial 语义：一侧可用、另一侧不可用时是 partial，而不是 ok/unavailable；
//  3. 门禁：端点走平台读权限（租户 admin 的 admin:read 不够）。

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/monitor"
)

// withoutBackends 在用例期间清空 PG/Redis 全局句柄（生产里它们由启动流程装配）。
func withoutBackends(t *testing.T) {
	t.Helper()
	prevPool, prevRedis := db.Pool, db.Redis
	db.Pool, db.Redis = nil, nil
	t.Cleanup(func() { db.Pool, db.Redis = prevPool, prevRedis })
}

// callOverview 直接调用 handler 并解出 data 载荷。
func callOverview(t *testing.T, h *AdminHandler, query string) (*httptest.ResponseRecorder, map[string]interface{}) {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/monitor/overview"+query, nil)
	rec := httptest.NewRecorder()
	h.GetMonitorOverview(rec, req)
	var envelope struct {
		Success bool                   `json:"success"`
		Data    map[string]interface{} `json:"data"`
		Error   string                 `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("响应不是合法 JSON: %v / body=%s", err, rec.Body.String())
	}
	return rec, envelope.Data
}

func section(t *testing.T, data map[string]interface{}, name string) map[string]interface{} {
	t.Helper()
	sec, ok := data[name].(map[string]interface{})
	if !ok {
		t.Fatalf("响应缺少 %s 区块: %#v", name, data)
	}
	return sec
}

func TestMonitorOverviewDegradesWithoutBackends(t *testing.T) {
	withoutBackends(t)
	h := &AdminHandler{}

	rec, data := callOverview(t, h, "")
	if rec.Code != http.StatusOK {
		t.Fatalf("后端全缺时仍应回 200（页面要能显示'未知'），得到 %d: %s", rec.Code, rec.Body.String())
	}
	if data["partial"] != true {
		t.Errorf("有区块不可用时 partial 必须为 true，得到 %#v", data["partial"])
	}
	if _, ok := data["generated_at"].(string); !ok {
		t.Errorf("generated_at 必须是 RFC3339 字符串，得到 %#v", data["generated_at"])
	}

	perf := section(t, data, "performance")
	if perf["status"] != "partial" {
		t.Errorf("PG/Redis 都缺时性能区块应为 partial（Go runtime 仍可读），得到 %v", perf["status"])
	}
	reqs := perf["requests"].(map[string]interface{})
	if reqs["has_samples"] != false {
		t.Errorf("没有请求样本时 has_samples 必须为 false，得到 %v", reqs["has_samples"])
	}
	if reqs["p99_ms"] != nil {
		t.Errorf("无样本时 p99_ms 必须是 null（0 会被读成'延迟为 0'），得到 %v", reqs["p99_ms"])
	}
	if note, _ := reqs["note"].(string); !strings.Contains(note, "无请求采样数据") {
		t.Errorf("无样本时必须说明'无采样数据'，得到 %q", note)
	}
	if perf["db_pool"] != nil {
		t.Errorf("PG 不可用时 db_pool 必须是 null，得到 %#v", perf["db_pool"])
	}
	goRt, _ := perf["go_runtime"].(map[string]interface{})
	if goRt == nil || goRt["goroutines"] == nil {
		t.Errorf("Go runtime 不依赖外部后端，必须始终可读，得到 %#v", perf["go_runtime"])
	}
	if !errorsMention(perf, "redis") || !errorsMention(perf, "postgres") {
		t.Errorf("降级原因必须点名具体组件（redis/postgres），得到 %#v", perf["errors"])
	}

	queue := section(t, data, "queue")
	if queue["status"] != "unavailable" {
		t.Errorf("Redis 与 PG 都缺时队列区块应为 unavailable，得到 %v", queue["status"])
	}
	if streams, _ := queue["streams"].([]interface{}); len(streams) != 0 {
		t.Errorf("队列不可用时不该编造 stream 行，得到 %#v", streams)
	}
	if queue["run_state"] != nil {
		t.Errorf("两种 run 计数来源都不可用时 run_state 必须是 null，得到 %#v", queue["run_state"])
	}

	cache := section(t, data, "cache")
	if cache["status"] != "unavailable" {
		t.Errorf("Redis 与引擎都缺时缓存区块应为 unavailable，得到 %v", cache["status"])
	}
	if cache["redis"] != nil || cache["llm"] != nil {
		t.Errorf("缓存两侧都不可用时都应为 null，得到 %#v / %#v", cache["redis"], cache["llm"])
	}
}

func errorsMention(section map[string]interface{}, component string) bool {
	errs, _ := section["errors"].([]interface{})
	for _, e := range errs {
		m, _ := e.(map[string]interface{})
		if m["component"] == component {
			return true
		}
	}
	return false
}

// TestMonitorOverviewPartialWhenOnlyEngineReachable 钉住"单项失败不影响其它项"：
// 引擎可达 ⇒ 性能区块拿到 RSS/CPU、缓存区块拿到 LLM 命中；而 Redis/PG 仍缺，
// 这两块只降级到 partial，缓存里 Redis 那半是 null 而不是 0。
func TestMonitorOverviewPartialWhenOnlyEngineReachable(t *testing.T) {
	withoutBackends(t)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/info" {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"version":"3.0.0","instance_id":"eng-1","uptime_seconds":120,
			"memory_mb":321.5,"cpu_percent":7.25,"active_tasks":3,
			"gateway":{"cache":{"l1_hits":90,"l2_hits":6,"l3_hits":4,"misses":10,"hit_rate":0.9}}
		}`))
	}))
	defer srv.Close()

	h := &AdminHandler{pythonClient: engine.NewPythonClient(srv.URL)}
	_, data := callOverview(t, h, "?window=5m")

	perf := section(t, data, "performance")
	if perf["window"] != "5m" || perf["window_seconds"].(float64) != 300 {
		t.Errorf("时间窗必须原样回显（5m/300s），得到 %v/%v", perf["window"], perf["window_seconds"])
	}
	eng, _ := perf["engine"].(map[string]interface{})
	if eng == nil || eng["reachable"] != true {
		t.Fatalf("引擎可达时 engine.reachable 应为 true，得到 %#v", perf["engine"])
	}
	if eng["memory_mb"].(float64) != 321.5 || eng["cpu_percent"].(float64) != 7.25 {
		t.Errorf("引擎 RSS/CPU 必须来自 /info，得到 %#v", eng)
	}
	if perf["status"] != "partial" {
		t.Errorf("引擎可用但 PG/Redis 缺 ⇒ partial，得到 %v", perf["status"])
	}

	cache := section(t, data, "cache")
	if cache["status"] != "partial" {
		t.Errorf("只有引擎侧缓存可用时缓存区块应为 partial，得到 %v", cache["status"])
	}
	if cache["redis"] != nil {
		t.Errorf("Redis 不可用时 cache.redis 必须是 null，得到 %#v", cache["redis"])
	}
	llm, _ := cache["llm"].(map[string]interface{})
	if llm == nil {
		t.Fatalf("引擎上报了 gateway.cache，cache.llm 不该为空")
	}
	// 0.9（0-1 小数）必须换成 90%，而不是原样显示 0.9%。
	if llm["hit_rate_percent"].(float64) != 90 {
		t.Errorf("hit_rate 应换算成百分比 90，得到 %v", llm["hit_rate_percent"])
	}
	if llm["l1_hits"].(float64) != 90 || llm["misses"].(float64) != 10 {
		t.Errorf("LLM 命中/未命中计数应原样透出，得到 %#v", llm)
	}

	// 队列仍不可用：引擎可达不代表队列可读（队列在 Redis 里）。
	if q := section(t, data, "queue"); q["status"] != "unavailable" {
		t.Errorf("Redis 缺时队列区块必须 unavailable，得到 %v", q["status"])
	}
}

// TestMonitorOverviewRejectsUnknownWindow：窗口只接受 5m/15m/60m。
// 自由取值会让"这个 P99 到底覆盖多久"无法回答，比拒绝更糟。
func TestMonitorOverviewRejectsUnknownWindow(t *testing.T) {
	withoutBackends(t)
	h := &AdminHandler{}
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/monitor/overview?window=7m", nil)
	rec := httptest.NewRecorder()
	h.GetMonitorOverview(rec, req)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("非法窗口应回 400，得到 %d: %s", rec.Code, rec.Body.String())
	}
}

// TestMonitorOverviewRequestSamplingIsReal：窗口统计必须来自真实采样。
// 没有写入点时 has_samples=false、p99=null；写入后样本数/错误率/分位数才出现。
func TestMonitorOverviewRequestSamplingIsReal(t *testing.T) {
	withoutBackends(t)
	h := &AdminHandler{}

	before := monitor.RequestWindowStats.Snapshot(15 * time.Minute)
	// 造三条样本：两条 200、一条 500。直接写全局采样器（与中间件同一入口）。
	monitor.RequestWindowStats.Record(10*time.Millisecond, 200)
	monitor.RequestWindowStats.Record(20*time.Millisecond, 200)
	monitor.RequestWindowStats.Record(30*time.Millisecond, 500)

	_, data := callOverview(t, h, "?window=15m")
	perf := section(t, data, "performance")
	reqs := perf["requests"].(map[string]interface{})

	if reqs["has_samples"] != true {
		t.Fatalf("写入样本后 has_samples 应为 true，得到 %#v", reqs)
	}
	if got := reqs["samples"].(float64); got != float64(before.Samples+3) {
		t.Errorf("窗口样本数应为写入前 %d + 3，得到 %v", before.Samples, got)
	}
	if reqs["server_errors"].(float64) < 1 {
		t.Errorf("500 必须计入 server_errors，得到 %#v", reqs)
	}
	if reqs["p50_ms"] == nil || reqs["p99_ms"] == nil {
		t.Errorf("有样本时分位数不应为 null，得到 %#v", reqs)
	}
	if buckets, _ := reqs["buckets"].([]interface{}); len(buckets) == 0 {
		t.Errorf("有样本时必须返回趋势桶，得到 %#v", reqs["buckets"])
	}
}

// TestMonitoringMiddlewareRecordsRealStatus：错误率要能反映真实状态码。
//
// 写成两条：正常 503 与 panic（客户端看到 500，但 handler 从未 WriteHeader）。
// 后者不补记 500 的话，错误率会恰好漏掉最严重的一类请求。
func TestMonitoringMiddlewareRecordsRealStatus(t *testing.T) {
	withoutBackends(t)
	before := monitor.RequestWindowStats.Snapshot(15 * time.Minute)

	rec := httptest.NewRecorder()
	MonitoringMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	})).ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/boom", nil))

	after := monitor.RequestWindowStats.Snapshot(15 * time.Minute)
	if after.Samples != before.Samples+1 {
		t.Fatalf("503 请求应新增 1 条样本，得到 %d → %d", before.Samples, after.Samples)
	}
	if after.ServerErrors != before.ServerErrors+1 {
		t.Errorf("503 必须计入 server_errors，得到 %d → %d", before.ServerErrors, after.ServerErrors)
	}

	// panic：外层 RecoverMiddleware 会回 500，这里补记后再放行 panic。
	func() {
		defer func() {
			if recover() == nil {
				t.Error("panic 应继续向上抛出（由 RecoverMiddleware 收口）")
			}
		}()
		MonitoringMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			panic("boom")
		})).ServeHTTP(httptest.NewRecorder(), httptest.NewRequest(http.MethodGet, "/panic", nil))
	}()
	panicked := monitor.RequestWindowStats.Snapshot(15 * time.Minute)
	if panicked.ServerErrors != after.ServerErrors+1 {
		t.Errorf("panic 请求必须按 500 计入 server_errors，得到 %d → %d", after.ServerErrors, panicked.ServerErrors)
	}
}

// TestMonitorOverviewRouteIsPlatformReadOnly 钉住门禁：
//   1. 路由必须挂到外部 mux（否则前端 404，与"功能没做"长得一样）；
//   2. 权限必须是**平台读**：租户 admin 持有 admin:read 也不能看全平台运行事实。
func TestMonitorOverviewRouteIsPlatformReadOnly(t *testing.T) {
	mux := http.NewServeMux()
	probe := &middlewareProbe{}
	registerAdminRoutes(mux, probe.reject(http.StatusUnauthorized), probe.passthrough, &AdminHandler{}, nil)

	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/v1/admin/monitor/overview", nil))
	if !probe.hit("/v1/admin/monitor/overview") {
		t.Fatal("GET /v1/admin/monitor/overview 没有匹配到任何注册模式 ⇒ 前端会拿到 404")
	}
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("未认证应被拦在 401，得到 %d", rec.Code)
	}

	// 直接验证权限口径：租户 admin（有 admin:read）403；平台 owner 200。
	guard := RequirePlatformOwner(auth.PermAdminRead)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	tenantAdmin := httptest.NewRequest(http.MethodGet, "/v1/admin/monitor/overview", nil)
	tenantAdmin = tenantAdmin.WithContext(auth.WithClaims(tenantAdmin.Context(), &auth.Claims{
		UserID: "u-admin", Role: "admin", Perms: []string{auth.PermAdminRead},
	}))
	rec = httptest.NewRecorder()
	guard.ServeHTTP(rec, tenantAdmin)
	if rec.Code != http.StatusForbidden {
		t.Errorf("租户 admin 读全平台监控应 403，得到 %d", rec.Code)
	}

	owner := httptest.NewRequest(http.MethodGet, "/v1/admin/monitor/overview", nil)
	owner = owner.WithContext(auth.WithClaims(owner.Context(), &auth.Claims{
		UserID: "u-owner", Role: "owner", Perms: []string{auth.PermAdminRead},
	}))
	rec = httptest.NewRecorder()
	guard.ServeHTTP(rec, owner)
	if rec.Code != http.StatusOK {
		t.Errorf("平台 owner 应可读监控，得到 %d", rec.Code)
	}
}
