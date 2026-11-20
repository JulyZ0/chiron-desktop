package api

// ── 检查点：回滚差异预览（只读）与 CAS 回收（GC）──
//
// 两条端点的共同点：**真正干活的是引擎**，网关只做两件它才能做的事 ——
// 1) 用 claims 校验归属（引擎端点无法区分"这个 id 是不是你自己的"）；
// 2) 从 PG 取近 30 天的检查点规模，补成响应里的 `stats`（前端时间线头部要显示）。
//
// 为什么差异预览必须由网关校验归属：引擎的 `/v1/internal/checkpoints/diff`
// 拿到 `root` 与 `commit_sha` 就去读工作区。少了归属校验，任何登录用户都能凭
// 一个猜到的 id 让引擎去快照**别人的**工作区（目录结构、文件名都会进响应）。
// 这条与回滚端点（`checkpoint_restore.go`）是同一个判据，刻意保持一致：
// 否则会出现"列得出来但预览不了"或更糟的"预览得了但回滚不了"。
//
// 为什么 GC 要用平台 owner 写权限：它删的是**本机共享**的 CAS 分片目录
// （`%LOCALAPPDATA%/chiron/checkpoints/untracked`），一个租户 admin 调一次就会
// 影响这台机器上所有项目的回滚能力。读都不给租户 admin（store 布局属于内部信息）。

import (
	"context"
	"log/slog"
	"net/http"
	"strconv"
	"strings"

	"github.com/athenavi/chiron/internal/auth"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// checkpointStatsWindowDays 是"平均规模"的统计窗口。
//
// 30 天与用户对"最近"的直觉一致，也足够平滑掉单个巨型回合的抖动。
const checkpointStatsWindowDays = 30

// checkpointGcDefaultRetentionDays 是回收默认保留期。
//
// 30 天：刚被删掉的检查点，其 blob 还要留一段时间，因为用户可能在时间线上
// 翻旧点、或者某次上报失败后补救。保留期只影响"垃圾什么时候消失"，不影响
// 任何可回滚的点 —— 被引用的分片永远不删。
const checkpointGcDefaultRetentionDays = 30

// checkpointScaleStats 汇总"检查点有多大"。
//
// `avg_*` 来自 PG（近 `window_days` 天的成功检查点）；`store_*` / `current_*`
// 来自引擎（本机 CAS 与当前工作区快照），由调用方合并进来。
type checkpointScaleStats struct {
	// SampleCount 是统计窗口内的成功检查点数。为 0 时平均值没有意义，
	// 前端据此显示"暂无数据"而不是一个 0。
	SampleCount     int64   `json:"sample_count"`
	AvgFileCount    float64 `json:"avg_file_count"`
	AvgByteSize     float64 `json:"avg_byte_size"`
	WindowDays      int     `json:"window_days"`
	// CurrentFiles/CurrentBytes 只在差异预览里有（当前工作区快照规模）。
	CurrentFiles int64 `json:"current_files,omitempty"`
	CurrentBytes int64 `json:"current_bytes,omitempty"`
	// StoreFiles/StoreBytes 是未跟踪 CAS 的规模（预览与 GC 都有）。
	StoreFiles int64 `json:"store_files,omitempty"`
	StoreBytes int64 `json:"store_bytes,omitempty"`
}

// checkpointAverages 读某个工作区（`workspaceID` 为空则全库）近 N 天的平均规模。
//
// 查询失败只记日志并返回零值：统计是**附加信息**，它坏掉不该让预览或 GC 失败
// —— 那会把"看不到平均值"升级成"回滚前无法预览"。
func checkpointAverages(ctx context.Context, workspaceID string) checkpointScaleStats {
	stats := checkpointScaleStats{WindowDays: checkpointStatsWindowDays}
	if db.GlobalDBManager == nil {
		return stats
	}
	// `::float8` 不能省：`AVG(bigint)` 在 PG 里返回 numeric，pgx 解码成
	// pgtype.Numeric，落到 floatOf 的 default 分支就是 0 —— 统计块永远显示 0。
	query := `SELECT COUNT(*) AS n,
	                 COALESCE(AVG(file_count),0)::float8 AS avg_files,
	                 COALESCE(AVG(byte_size),0)::float8 AS avg_bytes
	            FROM project_checkpoints
	           WHERE state = 'ok'
	             AND created_at > NOW() - ($1 || ' days')::interval`
	args := []interface{}{strconv.Itoa(checkpointStatsWindowDays)}
	if workspaceID != "" {
		query = `SELECT COUNT(*) AS n,
		                COALESCE(AVG(file_count),0)::float8 AS avg_files,
		                COALESCE(AVG(byte_size),0)::float8 AS avg_bytes
		           FROM project_checkpoints
		          WHERE workspace_id = $1 AND state = 'ok'
		            AND created_at > NOW() - ($2 || ' days')::interval`
		args = []interface{}{workspaceID, strconv.Itoa(checkpointStatsWindowDays)}
	}
	row, err := db.GlobalDBManager.FetchOne(ctx, query, args...)
	if err != nil || row == nil {
		if err != nil {
			slog.Warn("checkpoint stats query failed", "error", err, "workspace", workspaceID)
		}
		return stats
	}
	stats.SampleCount = int64(intOf(row["n"]))
	stats.AvgFileCount = floatOf(row["avg_files"])
	stats.AvgByteSize = floatOf(row["avg_bytes"])
	return stats
}

// checkpointDiffOwnershipSQL 是预览端点的归属校验。
//
// **单独成常量**而不是内联，是因为它是一条安全不变量：`tenant_id` 与
// `user_id` 两颗条件缺任何一颗，"别人的检查点"就会被当成自己的 —— 而引擎拿到
// root 后会把对方工作区的目录结构读出来返回。单测直接钉住这两颗条件。
const checkpointDiffOwnershipSQL = `SELECT c.id, c.state, c.commit_sha,
	       COALESCE(c.session_id,'') AS session_id,
	       COALESCE(c.workspace_id,'') AS workspace_id,
	       COALESCE(w.root_path,'') AS root_path
	  FROM project_checkpoints c
	  LEFT JOIN session_map_workspaces w ON w.id = c.workspace_id
	 WHERE c.id = $1 AND c.tenant_id = $2 AND COALESCE(c.user_id,'') = $3`

// CheckpointDiffHandler 是 POST /v1/checkpoints/{cid}/diff。
//
// **只读**：引擎侧用临时索引与临时 CAS 取快照，工作区一个字节都不动
// （见 python-engine/app/agent/checkpoint_maintenance.py 模块头）。
func CheckpointDiffHandler(python *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if python == nil {
			InternalError(w, "python engine not available")
			return
		}
		claims := auth.GetClaims(r.Context())
		if claims == nil {
			Unauthorized(w, "missing auth context")
			return
		}
		cid := strings.TrimSpace(r.PathValue("cid"))
		if cid == "" {
			BadRequest(w, "checkpoint id is required")
			return
		}
		if db.GlobalDBManager == nil {
			JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
			return
		}

		// 归属 + 工作区根 + commit 一次查完。
		//
		// `COALESCE(user_id,'')` 与回滚端点同口径：该列可空，`user_id = ''` 不匹配
		// NULL，少了包裹会让"创建时没有 user_id"的点永远预览不了。
		owned, err := db.GlobalDBManager.FetchOne(r.Context(), checkpointDiffOwnershipSQL,
			cid, smTenant(claims), claims.UserID)
		if err != nil {
			slog.Error("checkpoint diff: ownership lookup failed", "error", err, "id", cid)
			InternalError(w, "lookup failed")
			return
		}
		if owned == nil {
			// 查不到与无权限同一个 404，不泄露"这个 id 存在但不是你的"。
			NotFound(w, "checkpoint not found")
			return
		}
		if st := stringOf(owned["state"]); st != "" && st != "ok" {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false,
				Error:   "checkpoint state is " + st + ", not ok",
				Code:    CodeRequestFailed,
			})
			return
		}
		commitSHA := strings.TrimSpace(stringOf(owned["commit_sha"]))
		if commitSHA == "" {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: "checkpoint has no commit", Code: CodeRequestFailed,
			})
			return
		}
		rootPath := strings.TrimSpace(stringOf(owned["root_path"]))
		if rootPath == "" {
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: "workspace has no root path", Code: CodeRequestFailed,
			})
			return
		}

		payload := map[string]interface{}{
			"root":          rootPath,
			"commit_sha":    commitSHA,
			"session_id":    stringOf(owned["session_id"]),
			"checkpoint_id": cid,
		}
		var result map[string]interface{}
		if err := python.PostJSON(r.Context(), "/v1/internal/checkpoints/diff", payload, &result); err != nil {
			slog.Warn("checkpoint diff failed", "id", cid, "error", err)
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: err.Error(), Code: CodeRequestFailed,
			})
			return
		}
		if result == nil {
			result = map[string]interface{}{}
		}

		// 统计块：PG 的平均规模 + 引擎给出的当前快照规模。
		// 按 workspace_id 查（与列表端点同口径），不是 root_path。
		stats := mergeEngineScaleStats(
			checkpointAverages(r.Context(), strings.TrimSpace(stringOf(owned["workspace_id"]))),
			result,
		)
		result["stats"] = stats
		OK(w, result)
	}
}

// mergeEngineScaleStats 把引擎返回的规模字段搬进统计块。
//
// 单独一个函数是为了让"哪些数字来自引擎、哪些来自 PG"在代码里一眼可见：
// 混在一处时，某个字段取错了源（例如把 pgtype 当 float 读成 0）会静默显示 0。
func mergeEngineScaleStats(stats checkpointScaleStats, result map[string]interface{}) checkpointScaleStats {
	stats.CurrentFiles = int64(intOf(result["current_files"]))
	stats.CurrentBytes = int64(intOf(result["current_bytes"]))
	if v, ok := result["store_files"]; ok {
		stats.StoreFiles = int64(intOf(v))
	}
	if v, ok := result["store_bytes"]; ok {
		stats.StoreBytes = int64(intOf(v))
	}
	return stats
}

// checkpointGcBody 是 POST /v1/admin/checkpoints/gc 的请求体。
type checkpointGcBody struct {
	// RetentionDays 保留期（天），缺省 30；0 表示"未引用即删"。
	RetentionDays int `json:"retention_days"`
	// DryRun 用指针接：缺省必须是 **true**（安全侧），只有显式 false 才真删。
	// 用 bool 时"字段缺失"与"显式 false"无法区分 —— 一个手滑的 {} 请求会直接删文件。
	DryRun *bool `json:"dry_run"`
	// WorkspaceID 可选：只看这一个项目的仓库（枚举其 checkpoint ref 做扫描统计）。
	// 不传则扫平台内所有已绑定的项目根。
	WorkspaceID string `json:"workspace_id"`
}

// resolveGcDryRun 决定"这次是不是真删"：**缺省一律 true**。
//
// 用指针接收是因为 `bool` 无法区分"字段缺失"与"显式 false"，而这两者的差别是
// "什么都不做"和"删掉本机所有未引用的分片"。默认落在安全侧，删除必须显式请求。
func resolveGcDryRun(v *bool) bool {
	if v == nil {
		return true
	}
	return *v
}

// resolveGcRetention 返回实际保留期（天）：缺省 / 非法一律落到 30，上限 3650。
func resolveGcRetention(days int) int {
	if days <= 0 {
		return checkpointGcDefaultRetentionDays
	}
	if days > 3650 {
		return 3650
	}
	return days
}

// CheckpointGcHandler 是 POST /v1/admin/checkpoints/gc（平台 owner 写权限）。
//
// 安全边界（与引擎侧共同成立，缺一不可）：
//   - 引用集合来自 PG 的 `checkpoint_untracked`，**全局**取；引擎查不到就 503 中止；
//   - 保留期内的一律不删；
//   - `dry_run` 缺省为 true。
func CheckpointGcHandler(python *engine.PythonClient) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if python == nil {
			InternalError(w, "python engine not available")
			return
		}
		if db.GlobalDBManager == nil {
			JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "database unavailable"})
			return
		}
		var body checkpointGcBody
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
		dryRun := resolveGcDryRun(body.DryRun)
		// 保留期：缺省（0）落到 30 天。**不接受"0 = 未引用即删"** —— 那个语义
		// 太容易由一次手滑的 {} 请求触发，而代价是本机所有项目的未跟踪缓存。
		retention := resolveGcRetention(body.RetentionDays)

		roots, err := checkpointGcRoots(r.Context(), body.WorkspaceID)
		if err != nil {
			logAndRespond(w, err, http.StatusInternalServerError, "workspace roots lookup failed")
			return
		}

		payload := map[string]interface{}{
			"roots":          roots,
			"retention_days": retention,
			"dry_run":        dryRun,
		}
		var result map[string]interface{}
		if err := python.PostJSON(r.Context(), "/v1/internal/checkpoints/gc", payload, &result); err != nil {
			slog.Warn("checkpoint gc failed", "error", err)
			JSON(w, http.StatusConflict, APIResponse{
				Success: false, Error: err.Error(), Code: CodeRequestFailed,
			})
			return
		}
		if result == nil {
			result = map[string]interface{}{}
		}
		stats := checkpointAverages(r.Context(), "")
		stats.StoreFiles = int64(intOf(result["store_files"]))
		stats.StoreBytes = int64(intOf(result["store_bytes"]))
		result["stats"] = stats
		OK(w, result)
	}
}
