package api

// ── 检查点 CAS 回收（GC）的定时化 ──
//
// 为什么落在**网关**的后台循环里，而不是引擎进程自己注册周期任务：
//
//  1. 引擎是按需拉起的（会话开始时由网关连上），生命周期不保证覆盖 24h；网关的
//     后台维护（保留策略清理、会话地图对账）本来就是"进程级常驻"的那一层。
//  2. GC 需要的两样外部事实都只有网关侧现成：PG 里的**已知项目根**
//     （`session_map_workspaces.root_path`，用于枚举 ref 做扫描统计）与内部令牌。
//  3. 引擎端 `/v1/internal/checkpoints/gc` 已经把全部安全细节做完了：引用集合来自
//     PG 且**读不到就 503 中止**、保留期内不删、`dry_run` 缺省 true。定时任务因此
//     只是"每周期发一次 `dry_run=false`"，不新增任何删除逻辑 —— 也就不会出现
//     "两条删除路径各有一套判据"的经典事故。
//
// 开关（`CHIRON_CHECKPOINT_GC_INTERVAL_HOURS`）：
//   - 缺省 **24**；
//   - `off` / `false` / `no` / `disabled` / `0` = 关闭；
//   - 其它非法值**回退默认**（宁可跑，不可静默关掉 —— 关掉的后果是 CAS 只增不减，
//     而用户不会收到任何提示）。
//
// 幂等：一轮 GC 的删除集合是"未被引用 **且** 超过保留期"的分片，引用集合在删除前
// 一次性冻结；已删掉的文件下一轮遇到 `FileNotFoundError` 直接跳过（引擎侧
// `checkpoint_maintenance.gc_execute`）。因此多副本、重启、手工触发都不会重复扣减，
// 第二轮 `deleted` 为 0。这也是这里**不需要**跨实例分布式租约的原因：最坏只是多扫
// 一遍 ref（每 24h 一次，成本可以忽略），不会多删一个字节。

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

const (
	// checkpointGcIntervalDefault 缺省周期：一天一次。
	//
	// 24h 的依据：分片的保留期默认 30 天，而"该不该删"由保留期 + 引用集合决定，
	// 周期只影响垃圾多留多久。一天一次既能及时释放磁盘，又不会让引擎被无谓唤醒。
	checkpointGcIntervalDefault = 24 * time.Hour
	// checkpointGcTimeout 单轮总超时：引擎侧要扫 CAS 分片目录，项目根多时会更慢，
	// 但不能无限占着 —— 超时后下一轮照常开始（GC 幂等）。
	checkpointGcTimeout = 10 * time.Minute
	// checkpointGcRootsLimit 与 CheckpointGcHandler 一致：只取有绑定目录的项目。
	checkpointGcRootsLimit = 200
	// checkpointGcEnvInterval 是周期（小时）与开关的同一个环境变量。
	checkpointGcEnvInterval = "CHIRON_CHECKPOINT_GC_INTERVAL_HOURS"
)

// checkpointGcInterval 解析 (周期, 是否启用)。
//
// 缺省启用；显式关闭是**唯一**能把定时 GC 关掉的途径（没有"因为配置写错所以不跑"
// 这种分支）。非法值回退默认并记一条 warn —— 静默吞掉配置错误是运维最难查的一类。
func checkpointGcInterval() (time.Duration, bool) {
	raw := strings.TrimSpace(os.Getenv(checkpointGcEnvInterval))
	if raw == "" {
		return checkpointGcIntervalDefault, true
	}
	switch strings.ToLower(raw) {
	case "0", "off", "false", "no", "disabled", "none":
		return 0, false
	}
	hours, err := strconv.ParseFloat(raw, 64)
	if err != nil || hours <= 0 {
		slog.Warn("invalid checkpoint gc interval; falling back to default",
			"env", checkpointGcEnvInterval, "value", raw,
			"default_hours", int(checkpointGcIntervalDefault.Hours()))
		return checkpointGcIntervalDefault, true
	}
	return time.Duration(hours * float64(time.Hour)), true
}

// postCheckpointGc 是引擎调用的唯一出口（包级变量便于单测替换）。
var postCheckpointGc = func(
	ctx context.Context, python *engine.PythonClient, payload map[string]interface{}, out *map[string]interface{},
) error {
	return python.PostJSON(ctx, "/v1/internal/checkpoints/gc", payload, out)
}

// checkpointGcOnce 执行一轮回收，返回引擎的统计（已含 `dry_run=false` 的真实删除数）。
//
// `roots` 只用于引擎侧枚举 checkpoint ref 做**扫描统计**；引用集合由引擎自己从 PG
// 取。因此这里读不到项目根不是致命错误：GC 仍会安全地只删"未被引用且过期"的分片。
func checkpointGcOnce(
	ctx context.Context, python *engine.PythonClient,
) (map[string]interface{}, error) {
	if python == nil {
		return nil, errors.New("python engine client unavailable")
	}
	cctx, cancel := context.WithTimeout(context.WithoutCancel(ctx), checkpointGcTimeout)
	defer cancel()

	roots, err := checkpointGcRoots(cctx, "")
	if err != nil {
		// 项目根列表只影响统计口径，不该让回收整轮作废。
		slog.Warn("checkpoint gc: workspace roots unavailable, continuing with empty root list",
			"error", err)
		roots = nil
	}

	payload := map[string]interface{}{
		"roots":          roots,
		"retention_days": checkpointGcDefaultRetentionDays,
		// 定时任务的语义就是"真的删"：预演没有意义，用户不会来看这份计划。
		// 安全性由引擎侧的三道闸门保证（引用集合 / 保留期 / 只删未引用）。
		"dry_run": false,
	}
	var result map[string]interface{}
	if err := postCheckpointGc(cctx, python, payload, &result); err != nil {
		return nil, err
	}
	if result == nil {
		result = map[string]interface{}{}
	}
	return result, nil
}

// checkpointGcRound 跑一轮并把统计写进日志。
//
// 周期任务的价值全在"事后能查"：`deleted` / `freed_bytes` / `orphan` / `kept_young`
// 四个数字合起来才能回答"GC 到底有没有干活、是不是什么都没删"。
func checkpointGcRound(ctx context.Context, python *engine.PythonClient) {
	result, err := checkpointGcOnce(ctx, python)
	if err != nil {
		slog.Warn("checkpoint gc round failed; next round will retry", "error", err)
		return
	}
	slog.Info("checkpoint gc round done",
		"deleted", intOf(result["deleted"]),
		"freed_bytes", intOf(result["freed_bytes"]),
		"expired", intOf(result["expired"]),
		"orphan", intOf(result["orphan"]),
		"kept_young", intOf(result["kept_young"]),
		"referenced", intOf(result["referenced"]),
		"store_files", intOf(result["store_files"]),
		"store_bytes", intOf(result["store_bytes"]),
		"refs", intOf(result["refs"]),
		"retention_days", intOf(result["retention_days"]))
}

// StartCheckpointGcScheduler 注册周期 GC（受 lifecycleCtx 控制）。main 启动时调用一次。
//
// 首轮不立即执行：启动期正是引擎刚热起来、数据库连接池还在爬坡的时候，
// 等一个周期再跑，避免和应用启动抢 IO。
func StartCheckpointGcScheduler(ctx context.Context, python *engine.PythonClient) {
	interval, enabled := checkpointGcInterval()
	if !enabled {
		slog.Info("checkpoint gc scheduler disabled by env", "env", checkpointGcEnvInterval)
		return
	}
	if python == nil {
		slog.Info("checkpoint gc scheduler skipped: python engine client unavailable")
		return
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		slog.Info("checkpoint gc scheduler started",
			"interval_hours", interval.Hours(),
			"retention_days", checkpointGcDefaultRetentionDays)
		for {
			select {
			case <-ctx.Done():
				slog.Info("checkpoint gc scheduler stopped")
				return
			case <-ticker.C:
				// panic 必须拦在**这一轮**内：未捕获的 panic 会终止整个网关进程
				// （见 runBackgroundCycle 注释）。
				runBackgroundCycle("checkpoint-gc", func() { checkpointGcRound(ctx, python) })
			}
		}
	}()
}

// checkpointGcRoots 取"已知项目根"，供 GC 枚举 ref 做扫描统计。
//
// `workspaceID` 非空时只取这一个项目；为空则取全平台已绑定目录（上限
// `checkpointGcRootsLimit`）。数据库不可用时返回 (nil, nil)：调用方各有各的处理
// —— 管理端点回 503，定时任务继续跑（见 checkpointGcOnce）。
func checkpointGcRoots(ctx context.Context, workspaceID string) ([]string, error) {
	if db.GlobalDBManager == nil {
		return nil, nil
	}
	roots := make([]string, 0, 8)
	if wsID := strings.TrimSpace(workspaceID); wsID != "" {
		row, err := db.GlobalDBManager.FetchOne(ctx,
			`SELECT COALESCE(root_path,'') AS root_path FROM session_map_workspaces WHERE id = $1`,
			wsID)
		if err != nil {
			return nil, err
		}
		if row != nil {
			if root := strings.TrimSpace(stringOf(row["root_path"])); root != "" {
				roots = append(roots, root)
			}
		}
		return roots, nil
	}
	// 全平台：只取有绑定目录的项目。失效目录由引擎跳过（项目被挪走很常见）。
	rows, err := db.GlobalDBManager.FetchAll(ctx,
		`SELECT DISTINCT root_path FROM session_map_workspaces
		  WHERE COALESCE(root_path,'') <> ''
		  LIMIT $1`, checkpointGcRootsLimit)
	if err != nil {
		return nil, err
	}
	for _, row := range rows {
		if root := strings.TrimSpace(stringOf(row["root_path"])); root != "" {
			roots = append(roots, root)
		}
	}
	return roots, nil
}
