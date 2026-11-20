package api

// monitor_rollup.go —— 运行时监控的分钟级持久化（P2）。
//
// ## 为什么要有这一层
//
// overview 的请求统计原本完全来自**本进程**的 60 分钟环缓冲。这带来两个
// 无法在面板上自证的偏差：
//
//  1. 多副本部署时，「近 15 分钟请求量」只是本副本的 15 分钟；
//  2. 进程重启后趋势归零，而排障通常正是从重启前后开始。
//
// 这里把**已走完的分钟**按 (ts_minute, route_class) 落进
// `metrics_rollup_minute`（迁移 0003），于是：
//
//   - overview 的 performance = 「窗口内 rollup 汇总（多副本、跨重启）+
//     本进程当前分钟的实时尾巴」；
//   - 「历史趋势（最近 24h）」直接查这张表。
//
// ## 单写者：PG advisory lock
//
// 每个副本都在跑同一个分钟定时器。如果都写，同一分钟会被 N 个副本各写一次，
// 而每个副本只看得见**自己的**采样 —— upsert 后最终留下的只有一个副本的数字，
// 比不写更糟（看起来完整，实际只覆盖 1/N 流量）。
//
// 因此抢 `pg_try_advisory_lock`：抢到的那一个负责写，其它副本跳过本轮。
// 锁挂在**专用连接**上，写完/提前返回都显式释放（连接归还池时锁也会随会话
// 结束释放，但显式 unlock 让下一次不必等连接回收）。
//
// 这意味着同一分钟的数据只来自一个副本 —— 这是**故意**的：一分钟的聚合本来就
// 无法从多个副本事后拼出准确值（每个副本只有自己的样本），单写者至少保证
// 「每一分钟要么有完整的一份，要么没有」，而不是 N 份互相覆盖的部分数据。
//
// ## 没有补写
//
// 只写「刚走完的那一分钟」。进程停机期间缺的分钟不会补 —— 那些样本在进程内
// 环缓冲里本来就不存在。历史趋势上表现为空洞，前端画成断点，比插值成假数据好。

import (
	"context"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
)

const (
	// monitorRollupTick 是写入检查的节奏：比 1 分钟更密，这样即使某次 tick
	// 因为停机/GC 抖动被跳过，下一分钟仍有两次机会；同分钟重复写入由
	// lastRolledMinute 挡住。
	monitorRollupTick = 30 * time.Second
	// monitorRollupTimeout 是单次落库的超时（一分钟几十行，10s 足够）。
	monitorRollupTimeout = 10 * time.Second

	// monitorRollupLockKey 是 advisory lock 键。取一个常量而不是哈希字符串，
	// 便于在 SQL 与运维文档里对得上；与仓内其它 advisory lock 不重复即可。
	monitorRollupLockKey int64 = 0x6d6f6e5f726f6c

	// monitorRollupDefaultClass 是 route_class 为空时的归类。
	monitorRollupDefaultClass = "default"

	// monitorHistoryMaxHours 是历史趋势端点允许的最大回看（7 天）。
	monitorHistoryMaxHours = 168
)

// rollupAdvisory* 单独提出来，便于测试断言「用的是 try_advisory_lock 且带键」。
const (
	rollupAdvisoryLockSQL   = "SELECT pg_try_advisory_lock($1)"
	rollupAdvisoryUnlockSQL = "SELECT pg_advisory_unlock($1)"
)

// rollupUpsertSQL 幂等写入一行分钟聚合。
const rollupUpsertSQL = `
INSERT INTO metrics_rollup_minute (ts_minute, route_class, count, errors, p95_ms)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (ts_minute, route_class) DO UPDATE
SET count = EXCLUDED.count,
    errors = EXCLUDED.errors,
    p95_ms = EXCLUDED.p95_ms`

// lastRolledMinute 记录本进程最后一次成功落库的分钟（unix 秒），防止
// 30s tick 对同一分钟重复写入 —— 重复写入会用「更晚的采样子集」覆盖
// 「完整的那一分钟」，数字反而变小。
var lastRolledMinute int64

// routeClassOf 把请求路径归到一个**低基数**分类。
//
// 取「路径首段（+ 次段，若首段是 API 版本前缀）」：`/v1/chat/completions` 与
// `/v1/chat/stream` 归为 `/v1/chat`。只用首段会让本仓所有业务路由都塌成 `/v1`
// 一类（那样聚合表就失去分组意义）；用完整路径又会让表变回逐请求日志。
// 也不用 HTTP 方法 —— 排障关心的是「哪类接口慢」，不是「GET 还是 POST」。
func routeClassOf(path string) string {
	p := strings.Trim(strings.TrimSpace(path), "/")
	if p == "" {
		return "root"
	}
	parts := strings.Split(p, "/")
	class := sanitizeClassSegment(parts[0])
	if class == "" {
		return "other"
	}
	// API 版本前缀下再取一层：/v1/chat、/v1/knowledge、/v1/admin …
	if class == "v1" && len(parts) >= 2 {
		if sub := sanitizeClassSegment(parts[1]); sub != "" {
			return "/v1/" + sub
		}
	}
	return "/" + class
}

// sanitizeClassSegment 只保留 ASCII 字母数字与 -_. ，避免把怪字符写进聚合键。
func sanitizeClassSegment(seg string) string {
	var b strings.Builder
	for _, r := range seg {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_', r == '.':
			b.WriteRune(r)
		}
	}
	return b.String()
}

// ── 写入 ──

// rollupRow 是一行待写入的分钟聚合（与 SQL 参数一一对应，便于单测）。
type rollupRow struct {
	Minute time.Time
	Class  string
	Count  int64
	Errors int64
	// P95 为 nil 表示该分钟没有可计耗时的样本（例如全是 101/304），
	// 写 NULL 而不是 0 —— 0ms 会被读成「非常快」。
	P95 *float64
}

// rollupRows 把按分类的区间统计转成待写入行。
//
// 分类为空归一到 "default"：中间件在极少数入口（单测、非 HTTP 调用）可能不传分类，
// 这个键的存在让 SQL 主键不会因空串与 NULL 的差异出现重复行。
func rollupRows(minute time.Time, byClass map[string]monitor.IntervalStats) []rollupRow {
	rows := make([]rollupRow, 0, len(byClass))
	for class, st := range byClass {
		if st.Count == 0 {
			continue // 无样本的分钟不写行：表里没有这一行 = 「没数据」，不是「0 请求」
		}
		c := strings.TrimSpace(class)
		if c == "" {
			c = monitorRollupDefaultClass
		}
		row := rollupRow{Minute: minute, Class: c, Count: st.Count, Errors: st.Errors}
		if st.P95Ms > 0 {
			v := st.P95Ms
			row.P95 = &v
		}
		rows = append(rows, row)
	}
	return rows
}

// StartMonitorRollup 启动分钟级落库循环（受 lifecycleCtx 控制）。
func StartMonitorRollup(ctx context.Context) {
	if db.Pool == nil {
		slog.Debug("monitor rollup writer not started: postgres unavailable")
		return
	}
	go func() {
		ticker := time.NewTicker(monitorRollupTick)
		defer ticker.Stop()
		slog.Info("monitor rollup writer started",
			"tick", monitorRollupTick.String(), "table", "metrics_rollup_minute")
		for {
			select {
			case <-ctx.Done():
				slog.Info("monitor rollup writer stopped")
				return
			case <-ticker.C:
				runBackgroundCycle("monitor-rollup", func() {
					writeRollupMinute(ctx, time.Now())
				})
			}
		}
	}()
}

// writeRollupMinute 写入「刚走完的那一分钟」。
//
// now 由调用方传入（便于测试）；内部按 now.Truncate(分钟) 取左闭右开区间
// [minute-1m, minute)。
func writeRollupMinute(ctx context.Context, now time.Time) {
	if db.Pool == nil {
		return
	}
	minuteEnd := now.Truncate(time.Minute)
	minuteStart := minuteEnd.Add(-time.Minute)
	if minuteStart.Unix() <= lastRolledMinute {
		return // 这一分钟已经写过（或已经过去了更晚的分钟）
	}
	byClass := monitor.RequestWindowStats.SnapshotIntervalByClass(minuteStart, minuteEnd)
	rows := rollupRows(minuteStart, byClass)
	if len(rows) == 0 {
		// 没有样本也要记账：否则 30s tick 会每分钟重复扫一遍环缓冲。
		lastRolledMinute = minuteStart.Unix()
		return
	}

	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), monitorRollupTimeout)
	defer cancel()

	conn, err := db.Pool.Acquire(cctx)
	if err != nil {
		slog.Warn("monitor rollup: acquire connection failed", "error", err)
		return
	}
	defer conn.Release()

	var locked bool
	if err := conn.QueryRow(cctx, rollupAdvisoryLockSQL, monitorRollupLockKey).Scan(&locked); err != nil {
		slog.Warn("monitor rollup: advisory lock failed", "error", err)
		return
	}
	if !locked {
		// 另一个副本正在写这一分钟：跳过，不是错误。
		slog.Debug("monitor rollup skipped: another replica holds the advisory lock")
		return
	}
	defer func() {
		if _, err := conn.Exec(context.Background(), rollupAdvisoryUnlockSQL, monitorRollupLockKey); err != nil {
			slog.Debug("monitor rollup: unlock failed", "error", err)
		}
	}()

	var written int
	for _, row := range rows {
		if _, err := conn.Exec(cctx, rollupUpsertSQL,
			row.Minute, row.Class, row.Count, row.Errors, row.P95); err != nil {
			slog.Warn("monitor rollup: upsert failed",
				"minute", row.Minute.Format(time.RFC3339), "route_class", row.Class, "error", err)
			return
		}
		written++
	}
	lastRolledMinute = minuteStart.Unix()
	slog.Debug("monitor rollup written",
		"minute", minuteStart.Format(time.RFC3339), "rows", written)
}

// ── 查询 ──

// rollupWindowAgg 是窗口内 rollup 的合计（多副本、多分钟、多分类）。
type rollupWindowAgg struct {
	Count  int64
	Errors int64
	MaxP95 float64
	// Rows 是参与汇总的聚合行数。0 表示「窗口内没有任何分钟被落库」——
	// 调用方必须据此退回「仅本进程实时窗口」，而不是把 0 当成流量为 0。
	Rows int64
}

func queryRollupWindow(ctx context.Context, from, to time.Time) (rollupWindowAgg, error) {
	var agg rollupWindowAgg
	if db.Pool == nil {
		return agg, fmt.Errorf("postgres 未配置")
	}
	err := db.Pool.QueryRow(ctx, `
		SELECT COALESCE(SUM(count), 0)::bigint,
		       COALESCE(SUM(errors), 0)::bigint,
		       COALESCE(MAX(p95_ms), 0)::float8,
		       COUNT(*)::bigint
		  FROM metrics_rollup_minute
		 WHERE ts_minute >= $1 AND ts_minute < $2`, from, to).
		Scan(&agg.Count, &agg.Errors, &agg.MaxP95, &agg.Rows)
	return agg, err
}

// mergeRollupWithTail 合并「窗口内已落库的分钟聚合」与「本进程当前分钟的实时尾巴」。
//
// 口径（也写进 API 的 note）：
//   - count / errors 相加，得到跨副本合计；
//   - p95 取两者最大值 —— 分位数不可加，取最大是保守近似（真实 P95 ≤ 两者
//     中较大者的情形在尾部主导时成立）；
//   - HasSamples 只在两者都为空时为 false。
func mergeRollupWithTail(agg rollupWindowAgg, tail monitor.IntervalStats) monitor.IntervalStats {
	out := monitor.IntervalStats{
		Count:      agg.Count + tail.Count,
		Errors:     agg.Errors + tail.Errors,
		HasSamples: agg.Rows > 0 || tail.HasSamples,
	}
	if agg.MaxP95 > tail.P95Ms {
		out.P95Ms = agg.MaxP95
	} else {
		out.P95Ms = tail.P95Ms
	}
	return out
}

// queryRollupBuckets 把窗口内的分钟聚合按固定宽度分组（跨副本 SUM）。
//
// widthSeconds 是前端趋势图的桶宽；结果**只含有数据的桶**，空桶由前端按 0
// 处理（与进程内 Buckets 不同：这里不生成 24 个固定点，因为前端还要按
// 实际覆盖率判断「这段时间到底有没有数据」）。
func queryRollupBuckets(ctx context.Context, from, to time.Time, widthSeconds int) ([]monitor.WindowBucket, error) {
	if db.Pool == nil {
		return nil, fmt.Errorf("postgres 未配置")
	}
	if widthSeconds <= 0 {
		widthSeconds = 300
	}
	rows, err := db.Pool.Query(ctx, `
		SELECT to_timestamp(floor(extract(epoch FROM ts_minute) / $3) * $3) AS bucket_at,
		       SUM(count)::bigint,
		       SUM(errors)::bigint,
		       COALESCE(MAX(p95_ms), 0)::float8
		  FROM metrics_rollup_minute
		 WHERE ts_minute >= $1 AND ts_minute < $2
		 GROUP BY bucket_at
		 ORDER BY bucket_at`, from, to, widthSeconds)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := make([]monitor.WindowBucket, 0, 32)
	for rows.Next() {
		var (
			at     time.Time
			count  int64
			errs   int64
			p95    float64
			bucket monitor.WindowBucket
		)
		if err := rows.Scan(&at, &count, &errs, &p95); err != nil {
			return nil, err
		}
		bucket = monitor.WindowBucket{At: at.UTC(), Count: count, Errors: errs, P95Ms: p95}
		out = append(out, bucket)
	}
	return out, rows.Err()
}

// bucketWidthSeconds 把窗口切成 bucketCount 个桶（不足 1 秒按 1 秒）。
func bucketWidthSeconds(window time.Duration, bucketCount int) int {
	if bucketCount <= 0 {
		bucketCount = 24
	}
	w := int(window.Seconds()) / bucketCount
	if w <= 0 {
		w = 1
	}
	return w
}

// appendTailBucket 把本进程当前分钟的实时尾巴合并进趋势桶。
//
// rollup 只覆盖已走完的分钟，最后那一段（当前分钟）必须由尾巴补上，
// 否则趋势图总在末尾"掉到 0"，看起来像流量突然停了。
func appendTailBucket(buckets []monitor.WindowBucket, at time.Time, tail monitor.IntervalStats) []monitor.WindowBucket {
	if !tail.HasSamples {
		return buckets
	}
	merged := monitor.WindowBucket{At: at, Count: tail.Count, Errors: tail.Errors, P95Ms: tail.P95Ms}
	// 若已有同刻的桶（时钟对齐时可能发生），累加而不是覆盖。
	for i := range buckets {
		if buckets[i].At.Equal(at) {
			buckets[i].Count += merged.Count
			buckets[i].Errors += merged.Errors
			if merged.P95Ms > buckets[i].P95Ms {
				buckets[i].P95Ms = merged.P95Ms
			}
			return buckets
		}
	}
	return append(buckets, merged)
}

// ── 历史趋势端点 ──

// MonitorHistoryPoint 是趋势图的一个点。
type MonitorHistoryPoint struct {
	At     time.Time `json:"at"`
	Count  int64     `json:"count"`
	Errors int64     `json:"errors"`
	P95Ms  *float64  `json:"p95_ms"`
}

// MonitorHistory 是 GET /v1/admin/monitor/history 的响应。
type MonitorHistory struct {
	GeneratedAt time.Time             `json:"generated_at"`
	From        time.Time             `json:"from"`
	To          time.Time             `json:"to"`
	Hours       int                   `json:"hours"`
	StepSeconds int                   `json:"step_seconds"`
	Status      string                `json:"status"`
	Sources     []string              `json:"sources"`
	Errors      []MonitorComponentError `json:"errors"`
	Points      []MonitorHistoryPoint `json:"points"`
	TotalCount  int64                 `json:"total_count"`
	TotalErrors int64                 `json:"total_errors"`
	Note        string                `json:"note,omitempty"`
}

// GetMonitorHistory GET /v1/admin/monitor/history?hours=24
//
// 平台级只读：回的是全平台流量历史（跨租户、多副本合计），与 overview 同口径。
func (h *AdminHandler) GetMonitorHistory(w http.ResponseWriter, r *http.Request) {
	hours := 24
	if raw := strings.TrimSpace(r.URL.Query().Get("hours")); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n <= 0 {
			BadRequest(w, "invalid hours: must be a positive integer")
			return
		}
		if n > monitorHistoryMaxHours {
			n = monitorHistoryMaxHours
		}
		hours = n
	}
	// 桶宽随回看跨度放大：24h → 5 分钟一个点（288 点）；7d → 30 分钟。
	step := 300
	if hours <= 1 {
		step = 60
	} else if hours > 24 {
		step = 1800
	}

	now := time.Now().UTC()
	from := now.Add(-time.Duration(hours) * time.Hour)
	// 对齐到桶宽，避免最后一个桶只有几十秒的样本而看起来「骤降」。
	to := now
	from = from.Truncate(time.Duration(step) * time.Second)
	to = to.Truncate(time.Duration(step) * time.Second)

	out := MonitorHistory{
		GeneratedAt: now,
		From:        from,
		To:          to,
		Hours:       hours,
		StepSeconds: step,
		Status:      monitorStatusOK,
		Sources:     []string{"postgres metrics_rollup_minute（分钟聚合，多副本合计）"},
		Errors:      []MonitorComponentError{},
		Points:      []MonitorHistoryPoint{},
		Note:        "每个点是窗口内该桶的 count/errors 合计与分钟 p95 的最大值（分位数不可加，取最大是保守近似）。进程停机期间的分钟不会补写，趋势上表现为空洞。",
	}
	if db.Pool == nil {
		out.Status = monitorStatusUnavailable
		out.Errors = append(out.Errors, MonitorComponentError{
			Component: "postgres", Message: "数据库未配置，历史趋势不可用",
		})
		OK(w, out)
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	buckets, err := queryRollupBuckets(ctx, from, to, step)
	if err != nil {
		out.Status = monitorStatusUnavailable
		out.Errors = append(out.Errors, MonitorComponentError{
			Component: "postgres", Message: "查询 metrics_rollup_minute 失败: " + err.Error(),
		})
		OK(w, out)
		return
	}
	for _, b := range buckets {
		var p95 *float64
		if b.P95Ms > 0 {
			v := b.P95Ms
			p95 = &v
		}
		out.Points = append(out.Points, MonitorHistoryPoint{
			At: b.At, Count: b.Count, Errors: b.Errors, P95Ms: p95,
		})
		out.TotalCount += b.Count
		out.TotalErrors += b.Errors
	}
	if len(out.Points) == 0 {
		out.Status = monitorStatusPartial
		out.Errors = append(out.Errors, MonitorComponentError{
			Component: "metrics_rollup_minute",
			Message:   "窗口内没有任何 rollup 行（落库循环可能尚未跑满一分钟，或进程刚启动）",
		})
	}
	OK(w, out)
}
