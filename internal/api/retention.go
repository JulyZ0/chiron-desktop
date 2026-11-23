package api

// retention.go —— 数据保留期清理（P0-4）。
//
// ## 三张表的共同问题
//
// `usage_events`（每次 LLM 调用一行）、`conversation_shares`（分享链接，过期/撤销后
// 只是垃圾行）、检查点 CAS 分片（本机未跟踪的 git 对象文件）都是**只增不减**的。
// 长期运行的部署里它们会把磁盘、索引与备份窗口一起撑大，而没有任何一条业务路径
// 会把它们删掉 —— 因为「什么时候删」是运维决策，不该由业务代码顺手做。
//
// 这里把它变成一个**周期任务**：每小时一轮，三件事一起做，各自独立降级：
//
//  1. usage_events：删 `created_at < now - usage_days`；
//  2. conversation_shares：删**已过期或已撤销**且时间早于 `now - share_days` 的；
//  3. 检查点 CAS：调引擎 `POST /v1/internal/checkpoints/gc`
//     （retention_days = checkpoint_days，**dry_run=false**，仅此任务内）。
//
// 另外保留原有 turns 清理（每回合一行，`TURN_RETENTION_DAYS` 环境变量），
// 与上述周期同一时刻执行，避免再多一个定时器。
//
// ## 删除的边界规则（不可妥协的几点）
//
//   - **只按时间**：谓词里不出现 status / 类型之类的业务过滤（turns 的
//     `status <> 'running'` 是唯一例外，它保护的是"进行中的现场"）；
//   - **分批 LIMIT**：一次 DELETE 十万行会长时间持锁并放大 WAL，按批
//     `DELETE ... WHERE id IN (SELECT ... LIMIT $2)` 循环；
//   - **绝不删未过期数据**：cutoff 由 `now - days` 算出，SQL 用严格 `<`
//     （恰好等于 cutoff 的行不算过期）—— 单测钉死这条；
//   - **dry_run**：只 count 不 delete，把"会删多少"如实报出来。
//
// ## 单副本执行
//
// 沿用 `tryAcquireRetentionLock`（Redis SET NX）。清理本身幂等，多副本并发
// 只是浪费与争行锁；Redis 不可用时退回"每个副本各跑一遍"（见函数注释）。

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

const (
	// retentionIntervalDefault 改为 1 小时：三张表都是分钟级增长的量，
	// 6 小时一轮意味着单轮删除量更大、峰值锁持有更久。
	retentionIntervalDefault = time.Hour
	turnRetentionDaysDefault = 90
	// retentionTimeout 要覆盖「三张表批量删 + 引擎 GC」的总时长，比原来的
	// 单表 30s 放大到 2 分钟；引擎 GC 自身有超时，这里的 ctx 只是兜底。
	retentionTimeout = 2 * time.Minute
	// retentionLockTTL 跨实例互斥键的 TTL：远大于单轮清理超时。
	retentionLockTTL = 15 * time.Minute

	// settingsCategoryRetention 是「保留期」配置所在的 system_settings 分组。
	settingsCategoryRetention = "retention"

	retentionUsageDaysDefault      = 180
	retentionShareDaysDefault      = 90
	retentionCheckpointDaysDefault = 90

	// retentionBatchLimit 是单批删除行数。5000 行在常见 PG 上是毫秒级，
	// 又足以让十万行数据在 20 轮内清完。
	retentionBatchLimit = 5000
	// retentionMaxBatches 是单表单轮的批次上限（防"边删边写"时永不停歇）。
	retentionMaxBatches = 200
	// retentionMaxDays 是配置上限（10 年），防止一次手滑输入把整表删光。
	retentionMaxDays = 3650
)

// retentionConfig 是三张表的保留天数（来自 system_settings 的 retention 分类）。
type retentionConfig struct {
	UsageDays      int `json:"usage_days"`
	ShareDays      int `json:"share_days"`
	CheckpointDays int `json:"checkpoint_days"`
}

func defaultRetentionConfig() retentionConfig {
	return retentionConfig{
		UsageDays:      retentionUsageDaysDefault,
		ShareDays:      retentionShareDaysDefault,
		CheckpointDays: retentionCheckpointDaysDefault,
	}
}

// normalize 把越界/缺失的值夹回合法区间。days<=0 视为"没配"，回落默认值。
func (c retentionConfig) normalize() retentionConfig {
	d := defaultRetentionConfig()
	clamp := func(v, def int) int {
		if v <= 0 {
			return def
		}
		if v > retentionMaxDays {
			return retentionMaxDays
		}
		return v
	}
	return retentionConfig{
		UsageDays:      clamp(c.UsageDays, d.UsageDays),
		ShareDays:      clamp(c.ShareDays, d.ShareDays),
		CheckpointDays: clamp(c.CheckpointDays, d.CheckpointDays),
	}
}

// loadRetentionConfig 从 system_settings 读保留期配置。
//
// 直接读表而不是走 settings.Store：这三个键都是非敏感整数，且后台任务没有
// 请求上下文里的 settingsStore 实例。读不到（表不存在 / 值损坏）一律回落默认值
// 并返回 nil error —— 保留期读失败不该让清理停下，默认值就是安全值。
func loadRetentionConfig(ctx context.Context) retentionConfig {
	cfg := defaultRetentionConfig()
	if db.Pool == nil {
		return cfg
	}
	rows, err := db.Pool.Query(ctx,
		`SELECT key, (value #>> '{}') AS raw FROM system_settings WHERE category = $1`,
		settingsCategoryRetention)
	if err != nil {
		slog.Debug("retention config load failed, using defaults", "error", err)
		return cfg
	}
	defer rows.Close()
	for rows.Next() {
		var key, raw string
		if err := rows.Scan(&key, &raw); err != nil {
			continue
		}
		n, err := strconv.Atoi(raw)
		if err != nil {
			continue
		}
		switch key {
		case "usage_days":
			cfg.UsageDays = n
		case "share_days":
			cfg.ShareDays = n
		case "checkpoint_days":
			cfg.CheckpointDays = n
		}
	}
	return cfg.normalize()
}

// ── 清理执行器（接口便于单测注入内存假表） ──

// retentionExecutor 是一张"按时间边界清理"的表的执行器。
//
// cutoff 语义固定为**严格早于**：`created_at < cutoff`。恰好等于 cutoff 的行
// 不算过期 —— 单测 TestRetentionTimeBoundary 钉死这条，因为 off-by-one 在这里
// 只有一种表现：多删了一天。
type retentionExecutor interface {
	// CountBefore 返回早于 cutoff 的行数（dry_run 用）。
	CountBefore(ctx context.Context, cutoff time.Time) (int64, error)
	// DeleteBatchBefore 删除最多 limit 行早于 cutoff 的数据，返回实际删除数。
	DeleteBatchBefore(ctx context.Context, cutoff time.Time, limit int) (int64, error)
}

// sqlRetentionTable 是 retentionExecutor 的 PG 实现（一批一条语句）。
type sqlRetentionTable struct {
	name      string
	countSQL  string
	deleteSQL string
}

func (t *sqlRetentionTable) CountBefore(ctx context.Context, cutoff time.Time) (int64, error) {
	if db.Pool == nil {
		return 0, fmt.Errorf("postgres 未配置")
	}
	var n int64
	err := db.Pool.QueryRow(ctx, t.countSQL, cutoff).Scan(&n)
	return n, err
}

func (t *sqlRetentionTable) DeleteBatchBefore(ctx context.Context, cutoff time.Time, limit int) (int64, error) {
	if db.Pool == nil {
		return 0, fmt.Errorf("postgres 未配置")
	}
	tag, err := db.Pool.Exec(ctx, t.deleteSQL, cutoff, limit)
	if err != nil {
		return 0, err
	}
	return tag.RowsAffected(), nil
}

// usageEventsRetentionTable：usage_events.created_at 是 timestamptz，直接比较。
// 走 `id IN (SELECT ... ORDER BY created_at LIMIT n)` 而不是
// `DELETE ... LIMIT n`：PG 没有 DELETE ... LIMIT，子查询形式能稳定命中
// `idx_usage_events_created_at` 之类的范围索引（若存在），并让每批行数确定。
func usageEventsRetentionTable() *sqlRetentionTable {
	return &sqlRetentionTable{
		name: "usage_events",
		countSQL: `SELECT COUNT(*) FROM usage_events
		            WHERE created_at < $1`,
		deleteSQL: `DELETE FROM usage_events
		             WHERE id IN (SELECT id FROM usage_events
		                           WHERE created_at < $1
		                           ORDER BY created_at
		                           LIMIT $2)`,
	}
}

// conversationSharesRetentionTable：只删**已过期或已撤销**且时间早于 cutoff 的分享。
//
// 谓词里两个时间列各自比较 cutoff，且都用 IS NOT NULL 显式排除空值 ——
// `NULL < cutoff` 在 SQL 里是 UNKNOWN（不删），这正是我们要的：没有 expires_at
// 又没有 revoked_at 的分享是"永久有效"的，绝不能因为一次清理就消失。
func conversationSharesRetentionTable() *sqlRetentionTable {
	const predicate = `((expires_at IS NOT NULL AND expires_at < $1)
	                     OR (revoked_at IS NOT NULL AND revoked_at < $1))`
	return &sqlRetentionTable{
		name:      "conversation_shares",
		countSQL:  `SELECT COUNT(*) FROM conversation_shares WHERE ` + predicate,
		deleteSQL: `DELETE FROM conversation_shares WHERE id IN (` +
			`SELECT id FROM conversation_shares WHERE ` + predicate +
			` ORDER BY COALESCE(revoked_at, expires_at) LIMIT $2)`,
	}
}

// retentionTaskStats 是单张表的清理结果。
type retentionTaskStats struct {
	Table     string    `json:"table"`
	Cutoff    time.Time `json:"cutoff"`
	Deleted   int64     `json:"deleted"`
	Matched   int64     `json:"matched,omitempty"` // dry_run 下的命中数
	Batches   int       `json:"batches"`
	DryRun    bool      `json:"dry_run"`
	Truncated bool      `json:"truncated,omitempty"` // 达到批次上限，下一轮继续
	Error     string    `json:"error,omitempty"`
}

// runRetentionPurge 按批清理一张表。
//
// 不变式（单测覆盖）：
//   - dry_run 下**不执行任何 DELETE**，只返回 Matched；
//   - 每批最多 batchLimit 行，循环直到不足一批或达到 retentionMaxBatches；
//   - 出错立即停止并带上错误文本，已删部分如实计入 Deleted（它确实删了）。
func runRetentionPurge(
	ctx context.Context,
	table retentionExecutor,
	name string,
	cutoff time.Time,
	batchLimit int,
	dryRun bool,
) retentionTaskStats {
	stats := retentionTaskStats{Table: name, Cutoff: cutoff.UTC(), DryRun: dryRun}
	if batchLimit <= 0 {
		batchLimit = retentionBatchLimit
	}
	if dryRun {
		n, err := table.CountBefore(ctx, cutoff)
		if err != nil {
			stats.Error = err.Error()
			return stats
		}
		stats.Matched = n
		return stats
	}
	for i := 0; i < retentionMaxBatches; i++ {
		n, err := table.DeleteBatchBefore(ctx, cutoff, batchLimit)
		if err != nil {
			stats.Error = err.Error()
			return stats
		}
		stats.Deleted += n
		stats.Batches++
		if n < int64(batchLimit) {
			return stats
		}
	}
	stats.Truncated = true
	return stats
}

// ── 检查点 CAS 回收（委托引擎） ──

// checkpointGcStats 是引擎 GC 的结果投影（字段名与引擎 GcReport 对齐）。
type checkpointGcStats struct {
	RetentionDays int    `json:"retention_days"`
	DryRun        bool   `json:"dry_run"`
	Roots         int    `json:"roots"`
	Deleted       int64  `json:"deleted"`
	FreedBytes    int64  `json:"freed_bytes"`
	Expired       int64  `json:"expired"`
	Orphan        int64  `json:"orphan"`
	KeptYoung     int64  `json:"kept_young"`
	Error         string `json:"error,omitempty"`
}

// retentionGcRoots 读本平台已绑定的项目根（与 CheckpointGcHandler 同口径，上限 200）。
func retentionGcRoots(ctx context.Context) []string {
	roots := make([]string, 0, 8)
	if db.Pool == nil {
		return roots
	}
	rows, err := db.Pool.Query(ctx,
		`SELECT DISTINCT root_path FROM session_map_workspaces
		  WHERE COALESCE(root_path, '') <> ''
		  LIMIT 200`)
	if err != nil {
		slog.Warn("retention: checkpoint roots lookup failed", "error", err)
		return roots
	}
	defer rows.Close()
	for rows.Next() {
		var root string
		if err := rows.Scan(&root); err != nil {
			continue
		}
		roots = append(roots, root)
	}
	return roots
}

// runCheckpointRetention 调引擎 GC 删过期未引用的 CAS 分片。
//
// **dry_run 固定为 false**：这是周期的保留期任务，不是管理端的手动演练
// （管理端演练走 POST /v1/admin/checkpoints/gc，缺省 dry_run=true）。
// 安全性由引擎侧保证：引用集合读不到就不删、保留期内不删。
func runCheckpointRetention(ctx context.Context, python *engine.PythonClient, days int) checkpointGcStats {
	stats := checkpointGcStats{RetentionDays: days, DryRun: false}
	if python == nil {
		stats.Error = "python engine 未配置"
		return stats
	}
	roots := retentionGcRoots(ctx)
	stats.Roots = len(roots)
	payload := map[string]interface{}{
		"roots":          roots,
		"retention_days": days,
		"dry_run":        false,
	}
	var result map[string]interface{}
	if err := python.PostJSON(ctx, "/v1/internal/checkpoints/gc", payload, &result); err != nil {
		stats.Error = err.Error()
		return stats
	}
	stats.Deleted = int64(intOf(result["deleted"]))
	stats.FreedBytes = int64(intOf(result["freed_bytes"]))
	stats.Expired = int64(intOf(result["expired"]))
	stats.Orphan = int64(intOf(result["orphan"]))
	stats.KeptYoung = int64(intOf(result["kept_young"]))
	if failed, ok := result["failed"].([]interface{}); ok && len(failed) > 0 {
		stats.Error = fmt.Sprintf("%d 个分片删除失败（详见引擎日志）", len(failed))
	}
	return stats
}

// ── 一轮清理 ──

// retentionRunStats 是一轮清理的结果（对外暴露给 retention 卡片）。
type retentionRunStats struct {
	RunAt        time.Time            `json:"run_at"`
	DurationMs   float64              `json:"duration_ms"`
	DryRun       bool                 `json:"dry_run"`
	Config       retentionConfig      `json:"config"`
	Tasks        []retentionTaskStats `json:"tasks"`
	Checkpoint   *checkpointGcStats   `json:"checkpoint,omitempty"`
	TotalDeleted int64                `json:"total_deleted"`
}

// lastRetentionRun 是最近一轮结果（GET /v1/admin/retention 用）。
var lastRetentionRun atomic.Pointer[retentionRunStats]

// retentionPythonClient 由 StartRetentionCleaner 注入，供手动触发端点使用。
var retentionPythonClient atomic.Pointer[engine.PythonClient]

// runRetentionCycle 执行一轮：usage_events / conversation_shares / 检查点 CAS。
//
// 三个任务**互不阻塞**：任何一个失败只写进自己的 Error，其余照常执行 ——
// 一次 PG 抖动不该让检查点回收也停摆。
func runRetentionCycle(ctx context.Context, cfg retentionConfig, dryRun bool) retentionRunStats {
	cfg = cfg.normalize()
	started := time.Now()
	now := started.UTC()
	out := retentionRunStats{
		RunAt:  now,
		DryRun: dryRun,
		Config: cfg,
		Tasks:  make([]retentionTaskStats, 0, 3),
	}

	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), retentionTimeout)
	defer cancel()

	usageCutoff := now.AddDate(0, 0, -cfg.UsageDays)
	shareCutoff := now.AddDate(0, 0, -cfg.ShareDays)

	usage := runRetentionPurge(cctx, usageEventsRetentionTable(), "usage_events",
		usageCutoff, retentionBatchLimit, dryRun)
	shares := runRetentionPurge(cctx, conversationSharesRetentionTable(), "conversation_shares",
		shareCutoff, retentionBatchLimit, dryRun)

	python := retentionPythonClient.Load()
	// 检查点回收的口径必须与"是不是演练"一致：dry_run=false 才真的删，
	// 演练模式下改用 dry_run=true 拿引擎的计划值（**不能**先真跑一次再覆盖）。
	var cpStats checkpointGcStats
	if dryRun {
		cpStats = runCheckpointRetentionDryRun(cctx, python, cfg.CheckpointDays)
	} else {
		cpStats = runCheckpointRetention(cctx, python, cfg.CheckpointDays)
	}

	out.Tasks = append(out.Tasks, usage, shares)
	for _, t := range out.Tasks {
		out.TotalDeleted += t.Deleted
	}
	out.Checkpoint = &cpStats
	out.TotalDeleted += cpStats.Deleted
	out.DurationMs = round2(float64(time.Since(started).Microseconds()) / 1000)
	lastRetentionRun.Store(&out)

	slog.Info("retention cycle done",
		"dry_run", dryRun,
		"usage_days", cfg.UsageDays, "share_days", cfg.ShareDays,
		"checkpoint_days", cfg.CheckpointDays,
		"usage_deleted", usage.Deleted, "usage_matched", usage.Matched,
		"shares_deleted", shares.Deleted, "shares_matched", shares.Matched,
		"checkpoint_deleted", cpStats.Deleted, "checkpoint_freed_bytes", cpStats.FreedBytes,
		"total_deleted", out.TotalDeleted, "duration_ms", out.DurationMs)
	return out
}

// runCheckpointRetentionDryRun 是演练模式下的引擎 GC 调用（dry_run=true）。
func runCheckpointRetentionDryRun(ctx context.Context, python *engine.PythonClient, days int) checkpointGcStats {
	stats := checkpointGcStats{RetentionDays: days, DryRun: true}
	if python == nil {
		stats.Error = "python engine 未配置"
		return stats
	}
	roots := retentionGcRoots(ctx)
	stats.Roots = len(roots)
	payload := map[string]interface{}{
		"roots":          roots,
		"retention_days": days,
		"dry_run":        true,
	}
	var result map[string]interface{}
	if err := python.PostJSON(ctx, "/v1/internal/checkpoints/gc", payload, &result); err != nil {
		stats.Error = err.Error()
		return stats
	}
	// dry_run 下引擎回的是计划值：deleted 已按"会删多少"给出。
	stats.Deleted = int64(intOf(result["deleted"]))
	stats.FreedBytes = int64(intOf(result["freed_bytes"]))
	stats.Expired = int64(intOf(result["expired"]))
	stats.Orphan = int64(intOf(result["orphan"]))
	stats.KeptYoung = int64(intOf(result["kept_young"]))
	return stats
}

// ── 周期任务 ──

// retentionLockKey 保留清理的跨实例互斥键，与 cron_scheduler 的租约做法一致。
var retentionLockKey = db.RedisKey("retention:lock")

// retentionLockAcquireLua 抢清理互斥（SET NX + EX）。
const retentionLockAcquireLua = `
if redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2]) then
  return 1
end
return 0
`

// tryAcquireRetentionLock 抢清理互斥；Redis 不可用时返回 ok=true —— 退回"每个实例
// 都跑一遍"。清理是幂等的，安全只是浪费，不该因 Redis 抖动就不清理。
func tryAcquireRetentionLock(ctx context.Context) (release func(), ok bool) {
	if db.Redis == nil {
		return func() {}, true
	}
	res := db.Redis.Eval(ctx, retentionLockAcquireLua,
		[]string{retentionLockKey}, "1", int(retentionLockTTL.Seconds()))
	if err := res.Err(); err != nil {
		slog.Debug("retention lock acquire failed, skipping this round", "error", err)
		return nil, false
	}
	if n, err := res.Int(); err != nil || n != 1 {
		return nil, false
	}
	return func() {
		// 不做 CAS 释放：清理幂等，最坏情况是锁被 TTL 回收后另一实例重复跑一轮。
		_ = db.Redis.Del(context.Background(), retentionLockKey).Err()
	}, true
}

// StartRetentionCleaner 启动保留期清理循环（受 lifecycleCtx 控制）。
//
// python 用于检查点 CAS 回收；为 nil 时该任务只记错误，其余两项照常。
func StartRetentionCleaner(ctx context.Context, python *engine.PythonClient) {
	if python != nil {
		retentionPythonClient.Store(python)
	}
	if db.Pool == nil {
		slog.Debug("retention cleaner not started: postgres unavailable")
		return
	}
	interval := retentionInterval()
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		slog.Info("retention cleaner started",
			"interval_minutes", int(interval.Minutes()),
			"tables", "turns, usage_events, conversation_shares, checkpoints")
		for {
			select {
			case <-ctx.Done():
				slog.Info("retention cleaner stopped")
				return
			case <-ticker.C:
				release, ok := tryAcquireRetentionLock(ctx)
				if !ok {
					slog.Debug("retention cycle skipped: another instance is running it")
					continue
				}
				runBackgroundCycle("retention", func() {
					purgeTurns(ctx)
					cfg := loadRetentionConfig(ctx)
					runRetentionCycle(ctx, cfg, false)
				})
				release()
			}
		}
	}()
}

// retentionInterval 允许 RETENTION_INTERVAL_HOURS 覆盖（默认 1 小时）。
func retentionInterval() time.Duration {
	if v := os.Getenv("RETENTION_INTERVAL_HOURS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return time.Duration(n) * time.Hour
		}
	}
	return retentionIntervalDefault
}

// turnRetentionDays 允许 TURN_RETENTION_DAYS 覆盖（默认 90）。
func turnRetentionDays() int {
	if v := os.Getenv("TURN_RETENTION_DAYS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return turnRetentionDaysDefault
}

// purgeTurns 删除保留期外已完结的回合。
//
// running 状态不删（避免误删进行中的现场）；created_at 在模型里生成为 VARCHAR(255)
// （写入值是 PG NOW() 的文本形式），因此把右侧也转成同样的文本形式、左侧保持裸列
// ⇒ 可走 ix_turns_created_at 做范围扫（见 0009 迁移与旧实现注释）。
func purgeTurns(ctx context.Context) {
	days := turnRetentionDays()
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), retentionTimeout)
	defer cancel()
	tag, err := db.Pool.Exec(cctx,
		`DELETE FROM turns
		 WHERE status <> 'running'
		   AND created_at < ((NOW() - make_interval(days => $1))::text)`,
		days)
	if err != nil {
		slog.Warn("turn retention purge failed", "retention_days", days, "error", err)
		return
	}
	if n := tag.RowsAffected(); n > 0 {
		slog.Info("turn retention purge done", "removed", n, "retention_days", days)
	}
}
