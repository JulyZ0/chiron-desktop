package api

// monitor_rollup_test.go —— 分钟聚合的写入形状 / 合并口径 / 单写者约束。
//
// 这一层没有真实 PG 也能测的部分很多，而且正是最容易写错的部分：
//
//   - 行转换：空分类归一、无样本不写行、无耗时写 NULL（不是 0ms）；
//   - 合并：count/errors 相加、p95 取最大、HasSamples 的判定；
//   - 单写者：advisory lock 的键与 SQL 形状（写错成普通 SELECT 就失去互斥）；
//   - 趋势桶：尾部合并、桶宽。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/monitor"
)

func TestMonitorRollupRowsShape(t *testing.T) {
	minute := time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC)
	rows := rollupRows(minute, map[string]monitor.IntervalStats{
		"/v1/chat": {Count: 10, Errors: 2, P95Ms: 120.5, HasSamples: true},
		"":         {Count: 3, Errors: 0, HasSamples: true},   // 空分类 → default
		"/healthz": {Count: 0, P95Ms: 9, HasSamples: false},   // 无样本 → 不写行
	})
	if len(rows) != 2 {
		t.Fatalf("无样本的分类不应写行，实际 %d 行: %#v", len(rows), rows)
	}
	byClass := map[string]rollupRow{}
	for _, r := range rows {
		byClass[r.Class] = r
	}
	if got := byClass["/v1/chat"]; got.Count != 10 || got.Errors != 2 || got.P95 == nil || *got.P95 != 120.5 {
		t.Fatalf("chat 行不符: %#v", got)
	}
	def, ok := byClass[monitorRollupDefaultClass]
	if !ok {
		t.Fatalf("空分类必须归一到 %q: %#v", monitorRollupDefaultClass, byClass)
	}
	if def.P95 != nil {
		t.Fatalf("没有耗时样本时 p95 必须是 NULL（0 会被读成「非常快」），实际 %v", *def.P95)
	}
	for _, r := range rows {
		if !r.Minute.Equal(minute) {
			t.Fatalf("ts_minute 必须落在分钟边界，实际 %v", r.Minute)
		}
	}
}

func TestMonitorMergeRollupWithTail(t *testing.T) {
	agg := rollupWindowAgg{Count: 900, Errors: 9, MaxP95: 80, Rows: 15}
	tail := monitor.IntervalStats{Count: 30, Errors: 3, P95Ms: 120, HasSamples: true}
	got := mergeRollupWithTail(agg, tail)
	if got.Count != 930 || got.Errors != 12 {
		t.Fatalf("count/errors 应相加，实际 %#v", got)
	}
	if got.P95Ms != 120 {
		t.Fatalf("p95 应取两者最大值，实际 %v", got.P95Ms)
	}
	if !got.HasSamples {
		t.Fatalf("有任一侧数据时 HasSamples 必须为 true")
	}
	// 两边都空：HasSamples 必须是 false —— 前端据此显示「无采样数据」。
	empty := mergeRollupWithTail(rollupWindowAgg{}, monitor.IntervalStats{})
	if empty.HasSamples || empty.Count != 0 {
		t.Fatalf("双空合并应无样本，实际 %#v", empty)
	}
	// 只有 rollup 有行、尾巴为空：仍算有样本（跨副本合计不能因本副本空闲而消失）。
	onlyRollup := mergeRollupWithTail(agg, monitor.IntervalStats{})
	if !onlyRollup.HasSamples || onlyRollup.Count != 900 {
		t.Fatalf("仅 rollup 有数据时也必须算有样本，实际 %#v", onlyRollup)
	}
}

func TestMonitorRollupAdvisoryLockContract(t *testing.T) {
	if monitorRollupLockKey == 0 {
		t.Fatalf("advisory lock 键不能为 0")
	}
	if !strings.Contains(rollupAdvisoryLockSQL, "pg_try_advisory_lock") {
		t.Fatalf("抢锁必须用 pg_try_advisory_lock（非阻塞），实际 %q", rollupAdvisoryLockSQL)
	}
	if !strings.Contains(rollupAdvisoryLockSQL, "$1") {
		t.Fatalf("抢锁 SQL 必须带键参数，实际 %q", rollupAdvisoryLockSQL)
	}
	if !strings.Contains(rollupAdvisoryUnlockSQL, "pg_advisory_unlock") {
		t.Fatalf("释放必须显式 unlock，实际 %q", rollupAdvisoryUnlockSQL)
	}
	if !strings.Contains(rollupUpsertSQL, "ON CONFLICT (ts_minute, route_class)") {
		t.Fatalf("写入必须幂等 upsert 到复合主键，实际 %q", rollupUpsertSQL)
	}
	for _, col := range []string{"count", "errors", "p95_ms", "instance"} {
		if !strings.Contains(rollupUpsertSQL, col) {
			t.Fatalf("upsert 缺少列 %s", col)
		}
	}
}

func TestMonitorRouteClassOf(t *testing.T) {
	cases := map[string]string{
		"/v1/chat/completions": "/v1/chat",
		"/v1/admin/monitor":    "/v1/admin",
		"":                     "root",
		"/":                    "root",
		"/healthz":             "/healthz",
		"//weird//":            "/weird",
		"/v1/中文/x":            "/v1",
	}
	for path, want := range cases {
		if got := routeClassOf(path); got != want {
			t.Fatalf("routeClassOf(%q) = %q, want %q", path, got, want)
		}
	}
}

func TestMonitorBucketWidthAndTailMerge(t *testing.T) {
	if got := bucketWidthSeconds(15*time.Minute, 24); got != 37 {
		t.Fatalf("15m/24 桶宽应为 37s，实际 %d", got)
	}
	if got := bucketWidthSeconds(0, 24); got != 1 {
		t.Fatalf("过小窗口应夹到 1s，实际 %d", got)
	}

	at := time.Date(2026, 11, 20, 10, 5, 0, 0, time.UTC)
	base := []monitor.WindowBucket{{At: at.Add(-5 * time.Minute), Count: 10, Errors: 1, P95Ms: 50}}
	tail := monitor.IntervalStats{Count: 4, Errors: 0, P95Ms: 30, HasSamples: true}
	out := appendTailBucket(base, at, tail)
	if len(out) != 2 {
		t.Fatalf("尾巴应追加一个新桶，实际 %d", len(out))
	}
	if last := out[len(out)-1]; !last.At.Equal(at) || last.Count != 4 {
		t.Fatalf("尾巴桶不符: %#v", last)
	}
	// 同刻已有桶：累加而不是新增。
	again := appendTailBucket(out, at, tail)
	if len(again) != 2 {
		t.Fatalf("同刻桶应合并，实际 %d 个", len(again))
	}
	if again[len(again)-1].Count != 8 {
		t.Fatalf("同刻桶应累加 count，实际 %d", again[len(again)-1].Count)
	}
}

func TestMonitorHistoryUnavailableWithoutPostgres(t *testing.T) {
	withoutBackends(t)
	h := &AdminHandler{}
	req := httptest.NewRequest(http.MethodGet, "/v1/admin/monitor/history?hours=24", nil)
	rec := httptest.NewRecorder()
	h.GetMonitorHistory(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("缺 PG 时应降级返回 200，实际 %d", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "unavailable") {
		t.Fatalf("缺 PG 时 status 必须是 unavailable，body=%s", rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "\"points\":[]") {
		t.Fatalf("缺 PG 时 points 必须是空数组而不是 null，body=%s", rec.Body.String())
	}
}

func TestMonitorRollupWriteNoopWithoutPostgres(t *testing.T) {
	withoutBackends(t)
	// 不应 panic：db.Pool 为空时直接返回。
	writeRollupMinute(context.Background(), time.Now())
}
