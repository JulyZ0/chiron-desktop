package api

// retention_admin.go —— 保留期配置与回收量（P0-4 的可观测面）。
//
//	GET  /v1/admin/retention        当前生效配置（DB 覆盖 + 默认值）+ 最近一轮回收量
//	POST /v1/admin/retention/run    立刻跑一轮（dry_run 缺省 true）
//
// ## 为什么读端点要同时回"默认值"
//
// 页面上如果只显示"当前生效 180 天"，管理员无法判断这是**他配的**还是
// **系统的缺省**。两者在滚动发布后需要不同的处置（缺省值不需要迁移记录，
// 配过的值要在升级说明里保住）。所以 config 与 defaults 同时给。
//
// ## 为什么手动触发缺省 dry_run=true
//
// 与 /v1/admin/checkpoints/gc 同一取舍：一次手滑的 `{}` 请求不该触发真实删除。
// 真删必须显式 `{"dry_run": false}`。
//
// ## 回收量从哪来
//
// 后台周期任务把每轮结果存在进程内（lastRetentionRun）。它是**进程级**的：
// 重启后卡片显示"尚未运行"，直到下一轮（最多 1 小时）。这比伪造一个 0
// 更诚实 —— 0 会被读成"跑了但没删到东西"。

import (
	"context"
	"net/http"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// AdminRetentionInfo 是 GET /v1/admin/retention 的响应。
type AdminRetentionInfo struct {
	// Config 是当前生效值（DB 有值用 DB，否则默认）。
	Config retentionConfig `json:"config"`
	// Defaults 是代码里的缺省值，供页面区分"配过"与"没配"。
	Defaults retentionConfig `json:"defaults"`
	// SettingsCategory 是保存这组配置用的 system_settings 分类名，
	// 页面用它走 PUT /v1/admin/settings（无需新增保存端点）。
	SettingsCategory string `json:"settings_category"`
	// IntervalMinutes 是周期任务的执行间隔（分钟）。
	IntervalMinutes int `json:"interval_minutes"`
	// BatchLimit 是单批删除行数（只读事实，供解释"为什么删除是分多次的"）。
	BatchLimit int `json:"batch_limit"`
	// DatabaseAvailable=false 时上面两项其实是默认值，页面应提示先接 PG。
	DatabaseAvailable bool `json:"database_available"`
	// LastRun 是**本进程**最近一轮的结果；重启后为 null。
	LastRun *retentionRunStats `json:"last_run"`
	// Note 说明口径（时间边界、dry_run、检查点走引擎 GC）。
	Note string `json:"note"`
}

// GetRetentionInfo GET /v1/admin/retention
func (h *AdminHandler) GetRetentionInfo(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()

	cfg := loadRetentionConfig(ctx)
	info := AdminRetentionInfo{
		Config:            cfg,
		Defaults:          defaultRetentionConfig(),
		SettingsCategory:  settingsCategoryRetention,
		IntervalMinutes:   int(retentionInterval().Minutes()),
		BatchLimit:        retentionBatchLimit,
		DatabaseAvailable: db.Pool != nil,
		LastRun:           lastRetentionRun.Load(),
		Note: "只按时间删除：usage_events 按 created_at、分享按 expires_at/revoked_at、" +
			"检查点 CAS 由引擎 GC 按 retention_days 处理（引用集合读不到就不删）。" +
			"删除按批进行，恰好等于边界的行不算过期。",
	}
	OK(w, info)
}

// RunRetentionNow POST /v1/admin/retention/run
//
// 请求体：`{"dry_run": false}`；缺省或 `{}` 为 dry_run=true（只统计不删除）。
func (h *AdminHandler) RunRetentionNow(w http.ResponseWriter, r *http.Request) {
	if db.Pool == nil {
		JSON(w, http.StatusServiceUnavailable, APIResponse{
			Success: false, Error: "database unavailable",
		})
		return
	}
	var body struct {
		DryRun *bool `json:"dry_run"`
	}
	// 空 body 视为 `{}`（dry_run 走安全缺省），其余解析失败才 400。
	if r.ContentLength != 0 {
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
	}
	dryRun := true
	if body.DryRun != nil {
		dryRun = *body.DryRun
	}

	ctx, cancel := context.WithTimeout(r.Context(), retentionTimeout)
	defer cancel()
	cfg := loadRetentionConfig(ctx)
	// 手动触发不抢跨实例锁：管理员就在等人造结果，抢不到锁反而要解释"为什么没跑"。
	stats := runRetentionCycle(ctx, cfg, dryRun)
	OK(w, stats)
}
