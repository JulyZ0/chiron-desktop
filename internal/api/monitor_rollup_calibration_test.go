package api

// monitor_rollup_calibration_test.go —— 分钟聚合的**口径**测试（无需 PG）。
//
// 这一组用例钉的是"数字怎么拼出来的"，而不是"SQL 写得对不对"：
//
//  1. 窗口切分：rollup 段与实时尾巴段首尾相接、不重叠、不越界；
//  2. 多副本合计：两行来自不同副本的分钟数据 SUM、p95 取 MAX、副本数如实报出；
//  3. 单写者：抢锁失败的副本必须**认账这一分钟**，否则 30s 后会用自己的部分样本
//     覆盖掉对手写好的完整一分钟；
//  4. overview 的对外口径：4xx 未知（null）、错误率分母是合计、sources 写明副本数。
//
// fake DB 的注入点在 monitor_rollup.go 的两个包级接口变量（rollupReads /
// rollupConns / rollupSamples）：*pgxpool.Pool 无法伪造，而这三处口径最值得测。

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/athenavi/chiron/internal/monitor"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

// ── fake DB ──

// fakeRollupReader 用给定的分钟行替代 PG 查询，并记下收到的区间。
type fakeRollupReader struct {
	rows     []rollupRowSample
	err      error
	from, to time.Time
	calls    int
}

func (f *fakeRollupReader) Rows(_ context.Context, from, to time.Time) ([]rollupRowSample, error) {
	f.calls++
	f.from, f.to = from, to
	return f.rows, f.err
}

func withRollupReads(t *testing.T, r monitorRollupReader) {
	t.Helper()
	prev := rollupReads
	rollupReads = r
	t.Cleanup(func() { rollupReads = prev })
}

type fakeExec struct {
	sql  string
	args []any
}

// fakeRollupConn 模拟一条带会话级 advisory lock 的连接。
type fakeRollupConn struct {
	locked    bool
	lockErr   error
	queryRows int
	execs     []fakeExec
	releases  int
}

func (c *fakeRollupConn) QueryRow(_ context.Context, _ string, _ ...any) pgx.Row {
	c.queryRows++
	return fakeLockRow{locked: c.locked, err: c.lockErr}
}

func (c *fakeRollupConn) Exec(_ context.Context, sql string, args ...any) (pgconn.CommandTag, error) {
	c.execs = append(c.execs, fakeExec{sql: sql, args: args})
	return pgconn.CommandTag{}, nil
}

func (c *fakeRollupConn) Release() { c.releases++ }

type fakeLockRow struct {
	locked bool
	err    error
}

func (r fakeLockRow) Scan(dest ...any) error {
	if r.err != nil {
		return r.err
	}
	if len(dest) != 1 {
		return nil
	}
	ptr, ok := dest[0].(*bool)
	if !ok {
		return nil
	}
	*ptr = r.locked
	return nil
}

type fakeRollupConns struct {
	conn     *fakeRollupConn
	err      error
	acquires int
}

func (p *fakeRollupConns) AcquireRollupConn(context.Context) (rollupWriterConn, error) {
	p.acquires++
	if p.err != nil {
		return nil, p.err
	}
	return p.conn, nil
}

func withRollupConns(t *testing.T, p rollupConnProvider) {
	t.Helper()
	prev := rollupConns
	rollupConns = p
	t.Cleanup(func() { rollupConns = prev })
}

func withRollupSamples(t *testing.T, s rollupSampler) {
	t.Helper()
	prev := rollupSamples
	rollupSamples = s
	t.Cleanup(func() { rollupSamples = prev })
}

// resetRolledMinute 清空"已认账的分钟"并保证用例结束后复原。
func resetRolledMinute(t *testing.T) {
	t.Helper()
	prev := lastRolledMinute
	lastRolledMinute = 0
	t.Cleanup(func() { lastRolledMinute = prev })
}

// ── 1. 窗口切分 ──

// TestMonitorSplitRollupWindowIsAdjacentAndBounded 钉住切分点的三条性质：
//
//	(a) 首尾相接：RollupTo == TailFrom（整分钟那一刻只属于 tail）；
//	(b) 不越界：覆盖总长 <= 请求的窗口（历史实现会多算最多 59 秒）；
//	(c) 不丢整分钟：被上取整掉的零头 < 1 分钟（rollup 段不能白白缩水一个整分钟）。
func TestMonitorSplitRollupWindowIsAdjacentAndBounded(t *testing.T) {
	cases := []struct {
		name       string
		now        time.Time
		window     time.Duration
		wantFrom   time.Time
		wantTo     time.Time
		wantTailTo time.Time
	}{
		{
			name:       "窗口未对齐整分钟",
			now:        time.Date(2026, 11, 20, 10, 7, 30, 0, time.UTC),
			window:     15 * time.Minute,
			wantFrom:   time.Date(2026, 11, 20, 9, 53, 0, 0, time.UTC),
			wantTo:     time.Date(2026, 11, 20, 10, 7, 0, 0, time.UTC),
			wantTailTo: time.Date(2026, 11, 20, 10, 7, 30, 0, time.UTC),
		},
		{
			name:       "now 恰在整分钟",
			now:        time.Date(2026, 11, 20, 10, 7, 0, 0, time.UTC),
			window:     15 * time.Minute,
			wantFrom:   time.Date(2026, 11, 20, 9, 52, 0, 0, time.UTC),
			wantTo:     time.Date(2026, 11, 20, 10, 7, 0, 0, time.UTC),
			wantTailTo: time.Date(2026, 11, 20, 10, 7, 0, 0, time.UTC),
		},
		{
			name:       "5 分钟窗口",
			now:        time.Date(2026, 11, 20, 10, 7, 30, 0, time.UTC),
			window:     5 * time.Minute,
			wantFrom:   time.Date(2026, 11, 20, 10, 3, 0, 0, time.UTC),
			wantTo:     time.Date(2026, 11, 20, 10, 7, 0, 0, time.UTC),
			wantTailTo: time.Date(2026, 11, 20, 10, 7, 30, 0, time.UTC),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			split := splitRollupWindow(tc.now, tc.window)
			if !split.RollupFrom.Equal(tc.wantFrom) {
				t.Errorf("RollupFrom: 期望 %v，实际 %v", tc.wantFrom, split.RollupFrom)
			}
			if !split.RollupTo.Equal(tc.wantTo) {
				t.Errorf("RollupTo: 期望 %v，实际 %v", tc.wantTo, split.RollupTo)
			}
			if !split.TailTo.Equal(tc.wantTailTo) {
				t.Errorf("TailTo: 期望 %v，实际 %v", tc.wantTailTo, split.TailTo)
			}
			// (a) 首尾相接，不重叠也不留缝。
			if !split.TailFrom.Equal(split.RollupTo) {
				t.Errorf("tail 起点必须等于 rollup 终点（%v vs %v）", split.TailFrom, split.RollupTo)
			}
			// (b) 覆盖总长不超过请求窗口。
			if covered := tc.now.Sub(split.RollupFrom); covered > tc.window {
				t.Errorf("覆盖 %v 超过请求窗口 %v", covered, tc.window)
			}
			// (c) 被上取整掉的部分不足一分钟。
			if dropped := split.RollupFrom.Sub(tc.now.Add(-tc.window)); dropped >= time.Minute || dropped < 0 {
				t.Errorf("窗口起点与 rollup 起点相差 %v（应落在 [0,1m)）", dropped)
			}
			// tail 段不足一分钟（这是"只补当前分钟"的直接后果）。
			if tailSpan := split.TailTo.Sub(split.TailFrom); tailSpan >= time.Minute || tailSpan < 0 {
				t.Errorf("tail 段长度 %v 应落在 [0,1m)", tailSpan)
			}
		})
	}

	// window<=0 按 15 分钟处理，而不是退化成"只有尾巴"。
	now := time.Date(2026, 11, 20, 10, 7, 30, 0, time.UTC)
	split := splitRollupWindow(now, 0)
	if got := now.Sub(split.RollupFrom); got < 13*time.Minute || got > 15*time.Minute {
		t.Fatalf("window<=0 应回落到 15 分钟，实际覆盖 %v", got)
	}
}

// TestMonitorTailAndRollupDoNotDoubleCount：同一条样本最多被一侧算到。
//
// 用真实采样器写入 3 条"现在"的请求，再按切分点分别统计：rollup 段与 tail 段
// 的样本数之和必须恰好是 3 —— 重叠（同一批算两次，错误率被稀释）与漏算
// （数字偏小）都会让这条断言失败。
func TestMonitorTailAndRollupDoNotDoubleCount(t *testing.T) {
	w := monitor.NewRequestWindow(64)
	for i := 0; i < 3; i++ {
		w.RecordClass("/v1/chat", time.Duration(10+i)*time.Millisecond, 200)
	}

	// 参考时刻取 now+1ms：区间的右边界是**开**的，而 Windows 的时钟粒度可达 1ms，
	// 样本时间可能与 time.Now() 完全相等（生产中采样发生在请求结束时，严格早于
	// 采集时刻，测试里加这 1ms 才等价）。
	ref := time.Now().Add(time.Millisecond)
	split := splitRollupWindow(ref, 15*time.Minute)
	rollupCount := w.SnapshotInterval(split.RollupFrom, split.RollupTo).Count
	tail := w.SnapshotInterval(split.TailFrom, split.TailTo)

	if total := rollupCount + tail.Count; total != 3 {
		t.Fatalf("rollup(%d) + tail(%d) 应恰好等于 3 条样本", rollupCount, tail.Count)
	}
	// 跨分钟记录时两侧都可能非零，但合并后总数必须是 3（不重复计数）。
	agg := rollupWindowAgg{}
	if rollupCount > 0 {
		agg = rollupWindowAgg{Count: rollupCount, Rows: 1}
	}
	if merged := mergeRollupWithTail(agg, tail); merged.Count != 3 {
		t.Fatalf("合并后应为 3 条，实际 %d", merged.Count)
	}
}

// ── 2. 多副本合计 ──

// TestMonitorAggregateRollupRowsTwoReplicas 用两行**来自不同副本**的分钟数据
// 钉住汇总口径：count/errors 相加、p95 取最大、副本数按 distinct instance 计数。
func TestMonitorAggregateRollupRowsTwoReplicas(t *testing.T) {
	rows := []rollupRowSample{
		{Minute: time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC), Class: "/v1/chat", Count: 120, Errors: 3, P95Ms: 80, Instance: "gw-a"},
		{Minute: time.Date(2026, 11, 20, 10, 1, 0, 0, time.UTC), Class: "/v1/chat", Count: 30, Errors: 1, P95Ms: 140, Instance: "gw-b"},
	}

	agg := aggregateRollupRows(rows)
	if agg.Count != 150 {
		t.Errorf("count 应相加为 150，实际 %d", agg.Count)
	}
	if agg.Errors != 4 {
		t.Errorf("errors（仅 5xx）应相加为 4，实际 %d", agg.Errors)
	}
	if agg.MaxP95 != 140 {
		t.Errorf("p95 应取最大值 140，实际 %v", agg.MaxP95)
	}
	if agg.Rows != 2 {
		t.Errorf("行数应为 2，实际 %d", agg.Rows)
	}
	if agg.Instances != 2 {
		t.Errorf("两个不同 instance 应报 2 个副本，实际 %d", agg.Instances)
	}

	// 副本数只数非空 instance：0006 之前的行没有身份，不能默认算成 1 个副本。
	legacy := aggregateRollupRows([]rollupRowSample{
		{Minute: rows[0].Minute, Count: 5, Instance: ""},
	})
	if legacy.Instances != 0 {
		t.Errorf("空 instance 不该计入副本数，实际 %d", legacy.Instances)
	}
	// 同一副本写了两分钟：副本数仍是 1。
	sameReplica := aggregateRollupRows([]rollupRowSample{
		{Instance: "gw-a"}, {Instance: "gw-a"},
	})
	if sameReplica.Instances != 1 {
		t.Errorf("同一 instance 的两行应算 1 个副本，实际 %d", sameReplica.Instances)
	}

	// 空窗口：Rows=0 让调用方退回实时窗口，而不是把 0 当成流量。
	empty := aggregateRollupRows(nil)
	if empty.Rows != 0 || empty.Count != 0 || empty.Instances != 0 {
		t.Errorf("空窗口应报全 0，实际 %#v", empty)
	}
}

// TestMonitorAggregateRollupBucketsTwoReplicas：趋势桶同样按 SUM + MAX(p95) 合并，
// 且输出按时间升序（map 迭代顺序不稳定，乱序会让线随机连线）。
func TestMonitorAggregateRollupBucketsTwoReplicas(t *testing.T) {
	rows := []rollupRowSample{
		{Minute: time.Date(2026, 11, 20, 10, 0, 0, 0, time.UTC), Class: "/v1/chat", Count: 120, Errors: 3, P95Ms: 80, Instance: "gw-a"},
		{Minute: time.Date(2026, 11, 20, 10, 1, 0, 0, time.UTC), Class: "/v1/chat", Count: 30, Errors: 1, P95Ms: 140, Instance: "gw-b"},
	}

	perMinute := aggregateRollupBuckets(rows, 60)
	if len(perMinute) != 2 {
		t.Fatalf("60s 桶应得 2 个点，实际 %d", len(perMinute))
	}
	if perMinute[0].Count != 120 || perMinute[0].P95Ms != 80 {
		t.Errorf("第一个桶不符: %#v", perMinute[0])
	}
	if perMinute[1].Count != 30 || perMinute[1].P95Ms != 140 {
		t.Errorf("第二个桶不符: %#v", perMinute[1])
	}
	if !perMinute[0].At.Before(perMinute[1].At) {
		t.Errorf("桶必须按时间升序，实际 %v / %v", perMinute[0].At, perMinute[1].At)
	}

	merged := aggregateRollupBuckets(rows, 300)
	if len(merged) != 1 {
		t.Fatalf("300s 桶应把两行合并成一个点，实际 %d", len(merged))
	}
	if merged[0].Count != 150 || merged[0].Errors != 4 || merged[0].P95Ms != 140 {
		t.Errorf("合并桶应为 count=150 errors=4 p95=140，实际 %#v", merged[0])
	}
	// 桶起点按 epoch 取整：同一段历史在不同宽度视图里落在同一刻度上。
	if got := merged[0].At.Unix(); got%300 != 0 {
		t.Errorf("桶起点应 epoch 对齐到 300s，实际 %d", got)
	}

	if got := aggregateRollupBuckets(nil, 60); got != nil {
		t.Errorf("空窗口不该编造桶，实际 %#v", got)
	}
}

// TestMonitorOverviewAggregatesReplicasFromRollup 是 overview 的端到端口径断言：
// 注入两行不同副本的 rollup 数据，检查响应里的合计、p95、4xx 未知、副本数文案。
func TestMonitorOverviewAggregatesReplicasFromRollup(t *testing.T) {
	withoutBackends(t) // PG/Redis 句柄为空：rollup 走 fake，其余区块照常降级

	reader := &fakeRollupReader{rows: []rollupRowSample{
		{Minute: time.Now().Add(-2 * time.Minute).Truncate(time.Minute), Class: "/v1/chat", Count: 120, Errors: 3, P95Ms: 80, Instance: "gw-a"},
		{Minute: time.Now().Add(-time.Minute).Truncate(time.Minute), Class: "/v1/chat", Count: 30, Errors: 1, P95Ms: 140, Instance: "gw-b"},
	}}
	withRollupReads(t, reader)

	h := &AdminHandler{}
	_, data := callOverview(t, h, "?window=15m")
	reqs := section(t, data, "performance")["requests"].(map[string]interface{})

	if reader.calls != 1 {
		t.Errorf("窗口合计与趋势桶应共用一次查询，实际 %d 次", reader.calls)
	}
	if got := reader.to; !got.Equal(got.Truncate(time.Minute)) {
		t.Errorf("rollup 查询上界必须落在整分钟，实际 %v", got)
	}
	if span := reader.to.Sub(reader.from); span > 15*time.Minute {
		t.Errorf("rollup 查询区间 %v 超过请求窗口 15m", span)
	}

	if reqs["rollup_samples"].(float64) != 150 {
		t.Errorf("rollup 样本数应为两副本之和 150，实际 %v", reqs["rollup_samples"])
	}
	samples := reqs["samples"].(float64)
	tail := reqs["tail_samples"].(float64)
	if samples != 150+tail {
		t.Errorf("合计应 = rollup 150 + tail %v，实际 %v", tail, samples)
	}
	// 4xx 在 rollup 路径下必须未知（null），不能写成 0。
	if reqs["client_errors"] != nil {
		t.Errorf("rollup 路径下 client_errors 必须是 null，实际 %#v", reqs["client_errors"])
	}
	serverErrors := reqs["server_errors"].(float64)
	if serverErrors < 4 {
		t.Errorf("两行 rollup 共 4 个 5xx，实际 %v", serverErrors)
	}
	// 错误率 = 5xx / 合计；4xx 不参与（这里也没有 4xx 数据可参与）。
	want := round2(serverErrors / samples * 100)
	if got := reqs["error_rate_percent"].(float64); got != want {
		t.Errorf("错误率应为 %v，实际 %v", want, got)
	}
	if p95 := reqs["p95_ms"].(float64); p95 != 140 {
		t.Errorf("p95 应取两行最大值 140，实际 %v", p95)
	}

	// sources 必须写明副本数 —— "多副本合计"这句话要能自证。
	sources, _ := reqs["sources"].([]interface{})
	joined := ""
	for _, s := range sources {
		joined += s.(string) + "|"
	}
	if !strings.Contains(joined, "2 个副本参与落库") {
		t.Errorf("sources 必须写明 2 个副本，实际 %q", joined)
	}
	if note, _ := reqs["note"].(string); !strings.Contains(note, "2 个副本参与落库") {
		t.Errorf("note 也要写明副本数，实际 %q", note)
	}
	if note, _ := reqs["note"].(string); !strings.Contains(note, "4xx") {
		t.Errorf("note 必须说明 4xx 在 rollup 路径下未知，实际 %q", note)
	}
}

// TestMonitorRollupReplicaLegacyRowsReportUnknown：没有 instance 的历史行
// 只能报"副本数未知"，不能默认写成 1 个副本。
func TestMonitorRollupReplicaLegacyRowsReportUnknown(t *testing.T) {
	withoutBackends(t)
	withRollupReads(t, &fakeRollupReader{rows: []rollupRowSample{
		{Minute: time.Now().Add(-time.Minute).Truncate(time.Minute), Class: "/v1/chat", Count: 10, Instance: ""},
	}})

	_, data := callOverview(t, &AdminHandler{}, "?window=5m")
	reqs := section(t, data, "performance")["requests"].(map[string]interface{})
	sources, _ := reqs["sources"].([]interface{})
	joined := ""
	for _, s := range sources {
		joined += s.(string) + "|"
	}
	if !strings.Contains(joined, "副本数未知") {
		t.Errorf("无 instance 的 rollup 行应报副本数未知，实际 %q", joined)
	}
}

// ── 3. 单写者 ──

// TestMonitorWriteRollupMinuteSkipsWhenLockHeld 钉住"抢锁失败也要认账"：
//
//   - 一行都不写（不 upsert）、不显式解锁；
//   - 但 lastRolledMinute 必须前进到这一分钟 —— 否则 30s 后的下一轮 tick 会再抢锁，
//     等对手写完释放后用自己的部分样本覆盖掉那一分钟的完整数据（数字变小，
//     且没有任何错误日志）；
//   - 同一分钟不重复扫环缓冲（acquire 只发生一次）。
func TestMonitorWriteRollupMinuteSkipsWhenLockHeld(t *testing.T) {
	resetRolledMinute(t)
	sampler := monitor.NewRequestWindow(32)
	sampler.RecordClass("/v1/chat", 10*time.Millisecond, 200)
	withRollupSamples(t, sampler)

	conn := &fakeRollupConn{locked: false}
	provider := &fakeRollupConns{conn: conn}
	withRollupConns(t, provider)

	// 目标区间 = 当前这一分钟：[truncate, truncate+1m)，所以 now 加一分钟。
	now := time.Now().Add(time.Minute)
	minuteStart := now.Truncate(time.Minute).Add(-time.Minute)

	writeRollupMinute(context.Background(), now)

	if provider.acquires != 1 {
		t.Fatalf("应尝试抢锁 1 次，实际 %d", provider.acquires)
	}
	if len(conn.execs) != 0 {
		t.Fatalf("没抢到锁时一行都不该写（也不该显式解锁），实际执行 %d 次", len(conn.execs))
	}
	if conn.releases != 1 {
		t.Errorf("连接必须归还，实际 %d 次 Release", conn.releases)
	}
	if lastRolledMinute != minuteStart.Unix() {
		t.Fatalf("抢锁失败也必须认账这一分钟（%d），实际 %d", minuteStart.Unix(), lastRolledMinute)
	}

	// 同一分钟的第二次 tick：不再抢锁、不再扫描。
	writeRollupMinute(context.Background(), now)
	if provider.acquires != 1 {
		t.Errorf("同一分钟不该重复抢锁，实际 acquire %d 次", provider.acquires)
	}
}

// TestMonitorWriteRollupMinuteWritesWhenLockAcquired：抢到锁时写一行/分类 + 显式解锁，
// 并带上写入者身份；4xx 只进 count、不进 errors。
func TestMonitorWriteRollupMinuteWritesWhenLockAcquired(t *testing.T) {
	resetRolledMinute(t)
	sampler := monitor.NewRequestWindow(32)
	sampler.RecordClass("/v1/chat", 10*time.Millisecond, 200)
	sampler.RecordClass("/v1/chat", 20*time.Millisecond, 404) // 4xx：只计数
	sampler.RecordClass("/v1/chat", 30*time.Millisecond, 500) // 5xx：计数 + errors
	withRollupSamples(t, sampler)

	conn := &fakeRollupConn{locked: true}
	provider := &fakeRollupConns{conn: conn}
	withRollupConns(t, provider)

	now := time.Now().Add(time.Minute)
	minuteStart := now.Truncate(time.Minute).Add(-time.Minute)
	writeRollupMinute(context.Background(), now)

	if len(conn.execs) != 2 {
		t.Fatalf("应写 1 行聚合 + 1 次解锁，实际 %d 次: %#v", len(conn.execs), conn.execs)
	}
	upsert := conn.execs[0]
	if !strings.Contains(upsert.sql, "INSERT INTO metrics_rollup_minute") {
		t.Fatalf("第一次 Exec 必须是 upsert，实际 %q", upsert.sql)
	}
	if !strings.Contains(upsert.sql, "instance") {
		t.Fatalf("upsert 必须写 instance 列（副本数口径依赖它），实际 %q", upsert.sql)
	}
	if len(upsert.args) != 6 {
		t.Fatalf("upsert 参数应为 6 个（分钟/分类/count/errors/p95/instance），实际 %d", len(upsert.args))
	}
	if minute, ok := upsert.args[0].(time.Time); !ok || !minute.Equal(minuteStart) {
		t.Errorf("ts_minute 应落在分钟边界 %v，实际 %v", minuteStart, upsert.args[0])
	}
	if class, _ := upsert.args[1].(string); class != "/v1/chat" {
		t.Errorf("route_class 应为 /v1/chat，实际 %v", upsert.args[1])
	}
	if count, _ := upsert.args[2].(int64); count != 3 {
		t.Errorf("count 应含 4xx（3 条），实际 %v", upsert.args[2])
	}
	if errs, _ := upsert.args[3].(int64); errs != 1 {
		t.Errorf("errors 只含 5xx（1 条），实际 %v", upsert.args[3])
	}
	if inst, _ := upsert.args[5].(string); inst == "" || inst != rollupInstanceID() {
		t.Errorf("instance 必须是本副本身份 %q，实际 %v", rollupInstanceID(), upsert.args[5])
	}
	if !strings.Contains(conn.execs[1].sql, "pg_advisory_unlock") {
		t.Errorf("写完必须显式解锁，实际 %q", conn.execs[1].sql)
	}
	if lastRolledMinute != minuteStart.Unix() {
		t.Errorf("成功写入后应认账这一分钟 %d，实际 %d", minuteStart.Unix(), lastRolledMinute)
	}
}

// TestMonitorWriteRollupMinuteRetriesAfterLockQueryError：抢锁查询本身失败时
// **不能**认账这一分钟 —— 下一轮 tick 还有机会补齐，而不是永久留空洞。
func TestMonitorWriteRollupMinuteRetriesAfterLockQueryError(t *testing.T) {
	resetRolledMinute(t)
	sampler := monitor.NewRequestWindow(8)
	sampler.RecordClass("/v1/chat", time.Millisecond, 200)
	withRollupSamples(t, sampler)

	withRollupConns(t, &fakeRollupConns{conn: &fakeRollupConn{lockErr: context.DeadlineExceeded}})
	writeRollupMinute(context.Background(), time.Now().Add(time.Minute))
	if lastRolledMinute != 0 {
		t.Fatalf("抢锁查询失败时不该认账（应留待下一轮重试），实际 lastRolledMinute=%d", lastRolledMinute)
	}

	// 连接都拿不到（PG 未配置）：同样不认账、不 panic。
	withRollupConns(t, &fakeRollupConns{err: errRollupNoPostgres})
	writeRollupMinute(context.Background(), time.Now().Add(time.Minute))
	if lastRolledMinute != 0 {
		t.Fatalf("无 PG 时不该认账，实际 lastRolledMinute=%d", lastRolledMinute)
	}
}

// TestMonitorRollupInstanceIDRules：身份规则 = 显式值优先 + 超长截断 + 同机可辨。
//
// instance 为空会被查询侧当成"0006 之前的行"，副本数就永远显示未知；
// 只取 hostname 的话，同机双实例会被算成同一个副本（副本数永远 1）。
func TestMonitorRollupInstanceIDRules(t *testing.T) {
	if id := rollupInstanceID(); id == "" {
		t.Fatal("实例身份不能为空")
	} else if len(id) > rollupInstanceMaxLen {
		t.Fatalf("实例身份不应超过 %d 字符，实际 %d", rollupInstanceMaxLen, len(id))
	} else if rollupInstanceID() != id {
		t.Fatalf("同一进程内身份必须稳定：%q vs %q", id, rollupInstanceID())
	}

	if got := resolveRollupInstance("", "chiron-0", 42); got != "chiron-0:42" {
		t.Errorf("缺省身份应为 host:pid，实际 %q", got)
	}
	if got := resolveRollupInstance("  ", "", 7); got != "unknown:7" {
		t.Errorf("主机名取不到时应回落 unknown，实际 %q", got)
	}
	if got := resolveRollupInstance("  gw-explicit  ", "chiron-0", 42); got != "gw-explicit" {
		t.Errorf("显式 CHIRON_INSTANCE_ID 优先（并去空白），实际 %q", got)
	}
	long := resolveRollupInstance(strings.Repeat("x", rollupInstanceMaxLen+10), "h", 1)
	if len(long) != rollupInstanceMaxLen {
		t.Errorf("超长身份应截断到 %d 字符，实际 %d", rollupInstanceMaxLen, len(long))
	}
}
