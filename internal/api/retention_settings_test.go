package api

import (
	"context"
	"strings"
	"testing"
)

// ── H82：收紧保留期必须显式确认（PRD 9.10）──────────────────────────────────
//
// 缺陷形态：三个 `*_days` 曾经和普通配置一样"填了就存"，而**收紧**意味着
// 下一轮清理会把更近的历史数据删掉。界面上没有旧值/新值、没有影响范围、
// 没有确认步骤；后端只做上下限夹取，也不记"谁因为什么收紧到了多少"。
//
// 这一组钉住三件事：
//   1. 严格解析：非法值必须报错，不能被静默夹回默认值（那比报错更糟）；
//   2. 收紧判定：任一字段变小即需确认，且必须带原因；
//   3. 预览/变更描述：旧值、新值、哪些是收紧，逐条给出（UI 与错误信息复用同一份）。
//
// 全部为纯函数测试，不依赖 PG/Redis。

func cfg(usage, share, checkpoint int) retentionConfig {
	return retentionConfig{UsageDays: usage, ShareDays: share, CheckpointDays: checkpoint}
}

func TestRetentionTightenRequiresConfirmOnlyWhenShrinking(t *testing.T) {
	base := cfg(180, 90, 90)

	cases := []struct {
		name string
		next retentionConfig
		want bool
	}{
		{"放宽不需确认", cfg(365, 90, 90), false},
		{"完全相同不需确认", cfg(180, 90, 90), false},
		{"任一字段变小即收紧", cfg(7, 90, 90), true},
		{"share 变小也算收紧", cfg(180, 30, 90), true},
		{"checkpoint 变小也算收紧", cfg(180, 90, 7), true},
		{"一放一收紧仍算收紧", cfg(365, 30, 90), true},
	}
	for _, c := range cases {
		if got := retentionTightenConfirmRequired(base, c.next); got != c.want {
			t.Fatalf("%s: 期望 %v，得到 %v", c.name, c.want, got)
		}
	}
}

func TestRetentionConfigFromBodyIsStrict(t *testing.T) {
	// 合法值
	got, err := retentionConfigFromBody(map[string]interface{}{
		"usage_days": float64(30), "share_days": float64(15), "checkpoint_days": float64(7),
	})
	if err != nil {
		t.Fatalf("合法值不该报错: %v", err)
	}
	if got != cfg(30, 15, 7) {
		t.Fatalf("解析结果不对: %+v", got)
	}

	// 未提供的字段沿用默认（部分保存）
	partial, err := retentionConfigFromBody(map[string]interface{}{"usage_days": float64(30)})
	if err != nil {
		t.Fatalf("部分保存不该报错: %v", err)
	}
	def := defaultRetentionConfig()
	if partial.UsageDays != 30 || partial.ShareDays != def.ShareDays {
		t.Fatalf("部分保存语义不对: %+v", partial)
	}

	// 非法值必须**报错**，而不是夹回默认
	for _, bad := range []interface{}{float64(0), float64(-1), float64(retentionMaxDays + 1)} {
		if _, err := retentionConfigFromBody(map[string]interface{}{"usage_days": bad}); err == nil {
			t.Fatalf("usage_days=%v 应该报错（不能静默夹取）", bad)
		}
	}
	if _, err := retentionConfigFromBody(map[string]interface{}{"usage_days": "30"}); err == nil {
		t.Fatal("字符串类型的 days 应该报错")
	}
}

func TestValidateRetentionSaveDemandsConfirmAndReason(t *testing.T) {
	ctx := context.Background()

	// 没有任何配置库时，loadRetentionConfig 回落默认值（180/90/90）。
	// 收紧但不确认 → 必须拒绝，且错误里点出差在哪。
	_, err := validateRetentionSave(ctx, map[string]interface{}{"usage_days": float64(7)})
	if err == nil {
		t.Fatal("收紧且未确认时必须拒绝")
	}
	if msg := err.Error(); !strings.Contains(msg, "usage_days") || !strings.Contains(msg, "confirm") {
		t.Fatalf("错误信息要指出字段与「需要确认」: %q", msg)
	}

	// 确认了但没有原因 → 仍拒绝
	if _, err := validateRetentionSave(ctx, map[string]interface{}{
		"usage_days": float64(7), "confirm": true,
	}); err == nil {
		t.Fatal("收紧必须填写原因")
	}

	// 原因过长 → 拒绝
	if _, err := validateRetentionSave(ctx, map[string]interface{}{
		"usage_days": float64(7), "confirm": true, "reason": string(make([]byte, 300)),
	}); err == nil {
		t.Fatal("原因过长必须拒绝")
	}

	// 确认 + 原因 → 通过
	if _, err := validateRetentionSave(ctx, map[string]interface{}{
		"usage_days": float64(7), "confirm": true, "reason": "合规要求缩短留存",
	}); err != nil {
		t.Fatalf("确认 + 原因应通过: %v", err)
	}

	// 放宽不需要确认
	if _, err := validateRetentionSave(ctx, map[string]interface{}{"usage_days": float64(365)}); err != nil {
		t.Fatalf("放宽不该要求确认: %v", err)
	}
}

func TestRetentionChangesDescribeOnlyActualDiffs(t *testing.T) {
	changes := retentionChanges(cfg(180, 90, 90), cfg(7, 90, 30))
	if len(changes) != 2 {
		t.Fatalf("只应给出两条变化，得到 %d: %+v", len(changes), changes)
	}
	if changes[0].Field != "usage_days" || changes[0].Old != 180 || changes[0].New != 7 || !changes[0].Tightened {
		t.Fatalf("usage_days 变化描述不对: %+v", changes[0])
	}
	if changes[1].Field != "checkpoint_days" || changes[1].Tightened != true {
		t.Fatalf("checkpoint_days 变化描述不对: %+v", changes[1])
	}

	// 完全没变 → 空切片（审计据此跳过）
	if got := retentionChanges(cfg(90, 90, 90), cfg(90, 90, 90)); len(got) != 0 {
		t.Fatalf("无变化时不该产生条目: %+v", got)
	}
}
