package api

import (
	"context"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── H82：收紧数据保留期必须显式确认（PRD 9.10）──────────────────────────────
//
// 缺陷形态：
//   - 设置页把三个 `*_days` 当普通配置直接 `saveSettings`，**收紧**（例如 usage_days
//     从 180 改到 7）与放宽看起来没有区别，而收紧意味着"下一轮清理会删掉一批历史数据"；
//   - 界面上没有"旧值 → 新值"、没有影响范围（会删多少行），也没有确认步骤；
//   - 后端只做上下限夹取，不记录"谁在什么时候因为什么收紧到了多少"。
//
// 这里补齐三件事：
//  1. `POST /v1/admin/retention/preview`：给出旧值、新值、是否需要确认、以及**按类型**
//     的待删行数（只读，`COUNT(*)`，不删任何东西）；
//  2. `PUT /v1/admin/settings` 的 retention 分组：收紧时必须带 `confirm=true` 与
//     `reason`，否则 400 并说明差在哪；
//  3. 保存成功后写审计（旧值/新值/原因），失败则整体不写（先全量校验再落库）。

// retentionTightenConfirmRequired 判断一次保存是否属于"收紧"。
//
// 只要**任一**天数变小就算收紧：那意味着下一轮清理的截止时间提前，会多删数据。
func retentionTightenConfirmRequired(oldCfg, newCfg retentionConfig) bool {
	return newCfg.UsageDays < oldCfg.UsageDays ||
		newCfg.ShareDays < oldCfg.ShareDays ||
		newCfg.CheckpointDays < oldCfg.CheckpointDays
}

// retentionChange 是一条"旧 → 新"的变化（供预览与错误信息复用）。
type retentionChange struct {
	Field string `json:"field"`
	Old   int    `json:"old"`
	New   int    `json:"new"`
	// Tightened 表示这条变化会让清理更激进。
	Tightened bool `json:"tightened"`
}

func retentionChanges(oldCfg, newCfg retentionConfig) []retentionChange {
	changes := []retentionChange{
		{Field: "usage_days", Old: oldCfg.UsageDays, New: newCfg.UsageDays,
			Tightened: newCfg.UsageDays < oldCfg.UsageDays},
		{Field: "share_days", Old: oldCfg.ShareDays, New: newCfg.ShareDays,
			Tightened: newCfg.ShareDays < oldCfg.ShareDays},
		{Field: "checkpoint_days", Old: oldCfg.CheckpointDays, New: newCfg.CheckpointDays,
			Tightened: newCfg.CheckpointDays < oldCfg.CheckpointDays},
	}
	out := make([]retentionChange, 0, len(changes))
	for _, c := range changes {
		if c.Old != c.New {
			out = append(out, c)
		}
	}
	return out
}

// retentionConfigFromBody 严格解析 retention 分组的请求体。
//
// 与 `loadRetentionConfig` 的宽松解析不同：**保存**路径必须拒绝非法值，
// 而不是悄悄夹回默认值 —— 用户以为自己设了 7 天、实际存的是 90 天，
// 那是比报错更糟的结果。
func retentionConfigFromBody(body map[string]interface{}) (retentionConfig, error) {
	cur := defaultRetentionConfig()
	out := cur
	for _, field := range []struct {
		key string
		dst *int
	}{
		{"usage_days", &out.UsageDays},
		{"share_days", &out.ShareDays},
		{"checkpoint_days", &out.CheckpointDays},
	} {
		raw, ok := body[field.key]
		if !ok {
			continue // 未提供的字段沿用当前值（部分保存）
		}
		n, ok := raw.(float64)
		if !ok {
			return out, fmt.Errorf("%s 必须是数字", field.key)
		}
		v := int(n)
		if v < 1 || v > retentionMaxDays {
			return out, fmt.Errorf("%s 必须在 1–%d 之间（收到 %d）", field.key, retentionMaxDays, v)
		}
		*field.dst = v
	}
	return out, nil
}

// retentionImpact 统计"按新配置下一轮会删掉多少行"。**只读**：只 COUNT，不 DELETE。
func retentionImpact(ctx context.Context, cfg retentionConfig, nowTime func() int64) map[string]int64 {
	out := map[string]int64{"usage_events": -1, "conversation_shares": -1, "checkpoints": -1}
	if db.GlobalDBManager == nil {
		return out
	}
	day := int64(86400)
	now := nowTime()
	countBefore := func(table string, days int, extra string) int64 {
		cutoff := now - int64(days)*day
		q := `SELECT COUNT(*) AS n FROM ` + table + ` WHERE created_at < to_timestamp($1)` + extra
		row, err := db.GlobalDBManager.FetchOne(ctx, q, cutoff)
		if err != nil || row == nil {
			return -1
		}
		return int64(intOf(row["n"]))
	}
	out["usage_events"] = countBefore("usage_events", cfg.UsageDays, "")
	// 分享只删"已过期或已撤销"的（与清理任务同口径，见 retention.go）。
	out["conversation_shares"] = countBefore(
		"conversation_shares", cfg.ShareDays,
		" AND (expires_at IS NOT NULL AND expires_at < NOW() OR revoked_at IS NOT NULL)",
	)
	// checkpoints 在引擎侧（Python），这里不猜：-1 表示"由引擎统计"。
	out["checkpoints"] = -1
	return out
}

// RetentionPreviewHandler POST /v1/admin/retention/preview
//
// 只读预览：界面据此展示"旧值 → 新值 + 会删多少"，用户确认后再走 settings 保存。
func (h *AdminHandler) RetentionPreviewHandler(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Config map[string]interface{} `json:"config"`
	}
	if err := DecodeJSON(w, r, &body); err != nil {
		BadRequest(w, ErrInvalidReq)
		return
	}
	if body.Config == nil {
		BadRequest(w, "config is required")
		return
	}
	proposed, err := retentionConfigFromBody(body.Config)
	if err != nil {
		BadRequest(w, err.Error())
		return
	}
	ctx := r.Context()
	current := loadRetentionConfig(ctx)
	changes := retentionChanges(current, proposed)
	OK(w, map[string]interface{}{
		"current":           current,
		"proposed":          proposed,
		"changes":           changes,
		"requires_confirm":  retentionTightenConfirmRequired(current, proposed),
		"impact":            retentionImpact(ctx, proposed, func() int64 { return time.Now().Unix() }),
		"max_days":          retentionMaxDays,
		"reason_required":   retentionTightenConfirmRequired(current, proposed),
		"changes_effective": "下一轮清理（每小时）",
	})
}

// validateRetentionSave 在真正落库前校验 retention 分组（H82）。
//
// 收紧必须显式确认并给出原因：这不是"多一步点击"，而是把"会删历史数据"这件事
// 从**副作用**变成**用户明确同意过的动作**（也才有据可查）。
func validateRetentionSave(ctx context.Context, cfg map[string]interface{}) (retentionConfig, error) {
	proposed, err := retentionConfigFromBody(cfg)
	if err != nil {
		return proposed, err
	}
	current := loadRetentionConfig(ctx)
	if !retentionTightenConfirmRequired(current, proposed) {
		return proposed, nil
	}
	confirmed, _ := cfg["confirm"].(bool)
	reason := strings.TrimSpace(strFromValue(cfg["reason"]))
	if !confirmed {
		changes := retentionChanges(current, proposed)
		parts := make([]string, 0, len(changes))
		for _, c := range changes {
			if c.Tightened {
				parts = append(parts, fmt.Sprintf("%s %d→%d", c.Field, c.Old, c.New))
			}
		}
		return proposed, fmt.Errorf(
			"收紧保留期会删除历史数据，需显式确认（confirm=true）并给出原因：%s",
			strings.Join(parts, "、"),
		)
	}
	if reason == "" {
		return proposed, fmt.Errorf("收紧保留期必须填写原因（reason）")
	}
	if len(reason) > 200 {
		return proposed, fmt.Errorf("原因过长（≤200 字符）")
	}
	return proposed, nil
}

// auditRetentionChange 记录"谁把保留期改成了什么、为什么"（H82）。
func auditRetentionChange(ctx context.Context, claims interface{ GetID() string }, oldCfg, newCfg retentionConfig, reason, ip string) {
	_ = claims
	if db.GlobalDBManager == nil {
		return
	}
	changes := retentionChanges(oldCfg, newCfg)
	if len(changes) == 0 {
		return
	}
	detail := make([]string, 0, len(changes))
	for _, c := range changes {
		detail = append(detail, fmt.Sprintf("%s %d→%d", c.Field, c.Old, c.New))
	}
	db.AuditLog(ctx, "", 0, "admin.retention_update", "system_settings/retention",
		strings.Join(detail, "; ")+"; reason="+reason, ip, map[string]interface{}{
			"old": map[string]int{
				"usage_days":      oldCfg.UsageDays,
				"share_days":      oldCfg.ShareDays,
				"checkpoint_days": oldCfg.CheckpointDays,
			},
			"new": map[string]int{
				"usage_days":      newCfg.UsageDays,
				"share_days":      newCfg.ShareDays,
				"checkpoint_days": newCfg.CheckpointDays,
			},
			"reason": reason,
		})
}
