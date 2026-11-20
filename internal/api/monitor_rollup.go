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
// **抢锁失败也必须记下这一分钟**（`lastRolledMinute`）：tick 是 30s 一次，若失败
// 只 return，30s 后本轮会再抢一次锁；此时对手往往已经写完并释放，于是本副本用
// **只有自己样本**的 upsert 覆盖掉那一分钟的完整数据 —— 数字变小、没有任何
// 错误日志，正是上面这段要防的那件事。跳过即认账，是单写者语义的一部分。
//
// ## 每一行都带写入者身份（instance）
//
// 迁移 0006 给聚合表加了 `instance` 列。单写者只解决"同一分钟不被覆盖"，
// 但窗口内的多条 rollup 行可能来自**不同的**副本（每轮抢到锁的副本不一定相同），
// 面板因此必须能说出"这些数字是几个副本合出来的" —— 否则"多副本合计"这句话
// 无法自证。副本数由查询侧对 distinct instance 计数得出，见 aggregateRollupRows。
//
// ## 没有补写
//
// 只写「刚走完的那一分钟」。进程停机期间缺的分钟不会补 —— 那些样本在进程内
// 环缓冲里本来就不存在。历史趋势上表现为空洞，前端画成断点，比插值成假数据好。

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
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
//
// instance 是**写入者身份**（见 rollupInstanceID）：单写者保证同一分钟只有一份，
// 但不同分钟可能由不同副本写入，窗口汇总据此报出"参与落库的副本数"。
const rollupUpsertSQL = `
INSERT INTO metrics_rollup_minute (ts_minute, route_class, count, errors, p95_ms, instance)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (ts_minute, route_class) DO UPDATE
SET count = EXCLUDED.count,
    errors = EXCLUDED.errors,
    p95_ms = EXCLUDED.p95_ms,
    instance = EXCLUDED.instance`

// errRollupNoPostgres 表示"没有 PG，本轮不落库"。它不是故障：调用方在没配库时
// 根本不会启动写循环，只有单测/降级路径会走到这里，因此按 Debug 记录。
var errRollupNoPostgres = errors.New("postgres 未配置，metrics_rollup_minute 不落库")

// rollupInstanceMaxLen 与迁移 0006 的列宽无关（text），只用于避免把超长主机名
// 写进主键之外的列；64 字符足够区分 StatefulSet pod 与 host:pid。
const rollupInstanceMaxLen = 64

var (
	rollupInstanceOnce sync.Once
	rollupInstance     string
)

// rollupInstanceID 返回本副本身份（写进 rollup 行的 instance 列）。
//
// 优先级：CHIRON_INSTANCE_ID（编排显式注入）→ `<hostname>:<pid>`。后者让
// "同一台机器上起两个网关"也彼此可辨 —— 只用 hostname 时，本地双实例调试
// 会被算成同一个副本，副本数永远显示 1。
func rollupInstanceID() string {
	rollupInstanceOnce.Do(func() {
		host, err := os.Hostname()
		if err != nil {
			host = ""
		}
		rollupInstance = resolveRollupInstance(os.Getenv("CHIRON_INSTANCE_ID"), host, os.Getpid())
	})
	return rollupInstance
}

// resolveRollupInstance 是身份的纯规则（rollupInstanceID 的取值实现），
// 便于单测直接验算"显式值优先 + 超长截断 + 同机多实例可辨"。
func resolveRollupInstance(explicit, host string, pid int) string {
	if v := strings.TrimSpace(explicit); v != "" {
		return truncateInstance(v)
	}
	if host == "" {
		host = "unknown"
	}
	return truncateInstance(fmt.Sprintf("%s:%d", host, pid))
}

func truncateInstance(s string) string {
	if len(s) <= rollupInstanceMaxLen {
		return s
	}
	return s[:rollupInstanceMaxLen]
}

// lastRolledMinute 记录本进程最后一次**认账**的分钟（unix 秒），防止
// 30s tick 对同一分钟重复写入 —— 重复写入会用「更晚的采样子集」覆盖
// 「完整的那一分钟」，数字反而变小。抢锁失败同样要记账（见文件头注释）。
var lastRolledMinute int64

// rollupSampler 是写分钟聚合所需的采样能力（*monitor.RequestWindow 实现）。
// 抽成接口是为了让"写哪一分钟 / 抢锁失败怎么收场"能在没有 PG 的单测里钉住。
type rollupSampler interface {
	SnapshotIntervalByClass(from, to time.Time) map[string]monitor.IntervalStats
}

// rollupWriterConn 是写分钟聚合所需的最小连接能力（*pgxpool.Conn 实现）。
type rollupWriterConn interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgx.Row
	Exec(ctx context.Context, sql string, args ...any) (pgconn.CommandTag, error)
	Release()
}

// rollupConnProvider 提供"带会话级锁"的连接。抽出来同样是为了单测能注入
// 抢锁成功/失败两种结果。
type rollupConnProvider interface {
	AcquireRollupConn(ctx context.Context) (rollupWriterConn, error)
}

type pgxRollupConns struct{}

func (pgxRollupConns) AcquireRollupConn(ctx context.Context) (rollupWriterConn, error) {
	if db.Pool == nil {
		return nil, errRollupNoPostgres
	}
	return db.Pool.Acquire(ctx)
}

var (
	rollupSamples rollupSampler      = monitor.RequestWindowStats
	rollupConns   rollupConnProvider = pgxRollupConns{}
)

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
	minuteEnd := now.Truncate(time.Minute)
	minuteStart := minuteEnd.Add(-time.Minute)
	if minuteStart.Unix() <= lastRolledMinute {
		return // 这一分钟已经写过（或已经过去了更晚的分钟）
	}
	byClass := rollupSamples.SnapshotIntervalByClass(minuteStart, minuteEnd)
	rows := rollupRows(minuteStart, byClass)
	if len(rows) == 0 {
		// 没有样本也要记账：否则 30s tick 会每分钟重复扫一遍环缓冲。
		lastRolledMinute = minuteStart.Unix()
		return
	}

	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), monitorRollupTimeout)
	defer cancel()

	conn, err := rollupConns.AcquireRollupConn(cctx)
	if err != nil {
		if !errors.Is(err, errRollupNoPostgres) {
			slog.Warn("monitor rollup: acquire connection failed", "error", err)
		}
		return
	}
	defer conn.Release()

	var locked bool
	if err := conn.QueryRow(cctx, rollupAdvisoryLockSQL, monitorRollupLockKey).Scan(&locked); err != nil {
		slog.Warn("monitor rollup: advisory lock failed", "error", err)
		return
	}
	if !locked {
		// 另一个副本正在写这一分钟：跳过，**并且把这一分钟记成已处理**。
		// 只跳过不记账的话，30s 后的下一轮 tick 会再抢一次锁；对手写完释放后
		// 本副本就能拿到锁，用自己的样本 upsert 覆盖掉那一分钟的完整数据 ——
		// 表现为面板数字变小、且没有任何错误日志。
		lastRolledMinute = minuteStart.Unix()
		slog.Debug("monitor rollup skipped: another replica holds the advisory lock",
			"minute", minuteStart.Format(time.RFC3339))
		return
	}
	defer func() {
		if _, err := conn.Exec(context.Background(), rollupAdvisoryUnlockSQL, monitorRollupLockKey); err != nil {
			slog.Debug("monitor rollup: unlock failed", "error", err)
		}
	}()

	instance := rollupInstanceID()
	var written int
	for _, row := range rows {
		if _, err := conn.Exec(cctx, rollupUpsertSQL,
			row.Minute, row.Class, row.Count, row.Errors, row.P95, instance); err != nil {
			slog.Warn("monitor rollup: upsert failed",
				"minute", row.Minute.Format(time.RFC3339), "route_class", row.Class, "error", err)
			return
		}
		written++
	}
	lastRolledMinute = minuteStart.Unix()
	slog.Debug("monitor rollup written",
		"minute", minuteStart.Format(time.RFC3339), "rows", written, "instance", instance)
}

// ── 查询 ──

// rollupWindowSplit 是「窗口内已落库的分钟」与「本进程当前分钟的实时尾巴」的切分。
//
// 切点固定在**整分钟边界**（RollupTo == TailFrom），两侧左闭右开，因此：
//   - 不重叠：同一批请求不会被 rollup 与 tail 各算一次（错误率会看起来比实际好）；
//   - 不留缝：恰好落在整分钟那一刻的样本进 tail，上一纳秒的进 rollup；
//   - 不越界：RollupFrom 上取整到分钟，覆盖总长**不超过**调用方要的 window。
//     历史实现用 `now.Truncate(分钟) - window`，实际覆盖是 window + 零头（最多多
//     算 59 秒），面板上写着「近 15 分钟」而数字来自 15 分 59 秒。
type rollupWindowSplit struct {
	RollupFrom time.Time
	RollupTo   time.Time
	TailFrom   time.Time
	TailTo     time.Time
}

// splitRollupWindow 计算切分点。window<=0 按 15 分钟处理（与 parseMonitorWindow 一致）。
func splitRollupWindow(now time.Time, window time.Duration) rollupWindowSplit {
	if window <= 0 {
		window = 15 * time.Minute
	}
	rollupTo := now.Truncate(time.Minute)
	start := now.Add(-window)
	rollupFrom := start.Truncate(time.Minute)
	if rollupFrom.Before(start) {
		rollupFrom = rollupFrom.Add(time.Minute) // 上取整：不把窗口外的半分钟算进来
	}
	if rollupFrom.After(rollupTo) {
		// 窗口不足一分钟（或 now 恰在整分钟）：rollup 侧为空，全部由尾巴承担。
		rollupFrom = rollupTo
	}
	return rollupWindowSplit{
		RollupFrom: rollupFrom,
		RollupTo:   rollupTo,
		TailFrom:   rollupTo,
		TailTo:     now,
	}
}

// rollupRowSample 是 metrics_rollup_minute 的一行（查询侧形状）。
//
// P95Ms 用 0 表示该行没有可计耗时的样本（表里是 NULL）—— 与 IntervalStats 同口径：
// 分位数"没有"和"是 0ms"必须能区分，但 bucket 汇总只关心 MAX，0 不会污染最大值。
type rollupRowSample struct {
	Minute   time.Time
	Class    string
	Count    int64
	Errors   int64
	P95Ms    float64
	Instance string
}

// rollupWindowAgg 是窗口内 rollup 的合计（多副本、多分钟、多分类）。
type rollupWindowAgg struct {
	Count  int64
	Errors int64
	MaxP95 float64
	// Rows 是参与汇总的聚合行数。0 表示「窗口内没有任何分钟被落库」——
	// 调用方必须据此退回「仅本进程实时窗口」，而不是把 0 当成流量为 0。
	Rows int64
	// Instances 是窗口内参与落库的**副本数**（distinct instance，忽略空值）。
	// 单写者保证同一分钟只有一行来源，但不同分钟可能由不同副本抢到锁。
	Instances int64
}

// monitorRollupReader 是 rollup 读取的最小接口。
//
// 抽成接口的唯一理由是**口径可测**：多副本合计（SUM / MAX(p95) / 副本数）
// 必须能用两行"来自不同副本"的数据钉住，而 *pgxpool.Pool 无法在单测里伪造。
type monitorRollupReader interface {
	Rows(ctx context.Context, from, to time.Time) ([]rollupRowSample, error)
}

type pgRollupReader struct{}

func (pgRollupReader) Rows(ctx context.Context, from, to time.Time) ([]rollupRowSample, error) {
	if db.Pool == nil {
		return nil, errRollupNoPostgres
	}
	// 一次取回窗口内的原始分钟行，SUM/MAX/副本数在 Go 侧算：
	//   - 口径只有一处实现（aggregateRollupRows），可以用 fake 数据单测；
	//   - overview 的趋势桶与窗口合计共用同一次查询（少一次往返）。
	// 窗口上限 60 分钟，行数 = 分钟 × 分类，量级在千行以内。
	pgRows, err := db.Pool.Query(ctx, `
		SELECT ts_minute,
		       route_class,
		       count,
		       errors,
		       COALESCE(p95_ms, 0)::float8,
		       COALESCE(instance, '')
		  FROM metrics_rollup_minute
		 WHERE ts_minute >= $1 AND ts_minute < $2
		 ORDER BY ts_minute ASC`, from, to)
	if err != nil {
		return nil, err
	}
	defer pgRows.Close()

	out := make([]rollupRowSample, 0, 64)
	for pgRows.Next() {
		var row rollupRowSample
		if err := pgRows.Scan(&row.Minute, &row.Class, &row.Count, &row.Errors, &row.P95Ms, &row.Instance); err != nil {
			return nil, err
		}
		out = append(out, row)
	}
	return out, pgRows.Err()
}

// rollupReads 是 overview/history 读取 rollup 的入口（单测可替换成 fake）。
var rollupReads monitorRollupReader = pgRollupReader{}

// aggregateRollupRows 把窗口内的分钟行汇成总计。
//
// 口径（与 API 的 note 一致）：
//   - count / errors **相加**：多副本、多分钟的请求量与 5xx 都是可加的；
//   - p95 取**最大值**：分位数不可加，取最大是保守近似（尾延迟不会比任何一分钟更差）；
//   - 副本数取 distinct instance，空串不计（0006 之前写入的历史行没有身份）。
func aggregateRollupRows(rows []rollupRowSample) rollupWindowAgg {
	agg := rollupWindowAgg{Rows: int64(len(rows))}
	instances := make(map[string]struct{}, 4)
	for _, row := range rows {
		agg.Count += row.Count
		agg.Errors += row.Errors
		if row.P95Ms > agg.MaxP95 {
			agg.MaxP95 = row.P95Ms
		}
		if inst := strings.TrimSpace(row.Instance); inst != "" {
			instances[inst] = struct{}{}
		}
	}
	agg.Instances = int64(len(instances))
	return agg
}

// aggregateRollupBuckets 把窗口内的分钟行按固定宽度分组（跨副本 SUM、p95 取 MAX）。
//
// 与 queryRollupBuckets 的 SQL 分组**同口径**（epoch 对齐 + SUM + MAX），区别只是
// 这里算在 Go 侧、与窗口合计共用同一份行，少一次往返。桶的归属用 epoch 取整而非
// 窗口起点：这样同一段历史在「近 24h」与「近 1h」两个视图里落在同一个时间刻度上。
func aggregateRollupBuckets(rows []rollupRowSample, widthSeconds int) []monitor.WindowBucket {
	if widthSeconds <= 0 {
		widthSeconds = 300
	}
	width := int64(widthSeconds)
	type acc struct {
		bucket monitor.WindowBucket
	}
	byBucket := make(map[int64]*acc, 16)
	order := make([]int64, 0, 16)
	for _, row := range rows {
		key := row.Minute.Unix() / width * width
		a, ok := byBucket[key]
		if !ok {
			a = &acc{bucket: monitor.WindowBucket{At: time.Unix(key, 0).UTC()}}
			byBucket[key] = a
			order = append(order, key)
		}
		a.bucket.Count += row.Count
		a.bucket.Errors += row.Errors
		if row.P95Ms > a.bucket.P95Ms {
			a.bucket.P95Ms = row.P95Ms
		}
	}
	if len(order) == 0 {
		return nil
	}
	// 行是按时序查出来的，但 map 的迭代顺序不是：按时间排序后再返回，
	// 否则趋势图的线会随机连线（且同一份数据两次请求可能画出不同图形）。
	sort.Slice(order, func(i, j int) bool { return order[i] < order[j] })
	out := make([]monitor.WindowBucket, 0, len(order))
	for _, key := range order {
		out = append(out, byBucket[key].bucket)
	}
	return out
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
