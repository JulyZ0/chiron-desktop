package api

// retention_test.go —— 保留期清理的边界/分批/dry_run 契约。
//
// 这三个行为一旦错，后果是不可逆的：多删一天是数据丢失，少删一天只是下一轮
// 再删。所以这里不测"能不能删"，只测**边界与节奏**：
//
//  1. 恰好等于 cutoff 的行不算过期（严格 <）；
//  2. 每批不超过 limit，循环直到不足一批；
//  3. dry_run 只统计不删除，且 Matched 是真实命中数；
//  4. 达到批次上限时如实标 Truncated，而不是假装删完了。

import (
	"context"
	"errors"
	"sort"
	"testing"
	"time"
)

// fakeRetentionTable 是内存版 retentionExecutor，按时间边界模拟真实表。
type fakeRetentionTable struct {
	rows []time.Time
	// alwaysFull 模拟"边删边写"：每批都返回 limit 行且不真正减少数据，
	// 用来验证批次上限（不必构造上万行）。
	alwaysFull bool
	failCount  error
	failDelete error
	countCalls int
	delCalls   int
}

func (f *fakeRetentionTable) CountBefore(_ context.Context, cutoff time.Time) (int64, error) {
	f.countCalls++
	if f.failCount != nil {
		return 0, f.failCount
	}
	var n int64
	for _, r := range f.rows {
		if r.Before(cutoff) {
			n++
		}
	}
	return n, nil
}

func (f *fakeRetentionTable) DeleteBatchBefore(_ context.Context, cutoff time.Time, limit int) (int64, error) {
	f.delCalls++
	if f.failDelete != nil {
		return 0, f.failDelete
	}
	if f.alwaysFull {
		return int64(limit), nil
	}
	if limit <= 0 {
		limit = retentionBatchLimit
	}
	// 按时间升序取前 limit 条（与 SQL 的 ORDER BY created_at LIMIT 同序），
	// 然后按**下标降序**删除，避免切片位移让后续下标失效。
	idx := make([]int, 0, len(f.rows))
	for i, r := range f.rows {
		if r.Before(cutoff) {
			idx = append(idx, i)
		}
	}
	sort.Slice(idx, func(a, b int) bool { return f.rows[idx[a]].Before(f.rows[idx[b]]) })
	if len(idx) > limit {
		idx = idx[:limit]
	}
	sort.Ints(idx)
	for i := len(idx) - 1; i >= 0; i-- {
		f.rows = append(f.rows[:idx[i]], f.rows[idx[i]+1:]...)
	}
	return int64(len(idx)), nil
}

func TestRetentionDefaultsAre180_90_30(t *testing.T) {
	cfg := defaultRetentionConfig()
	if cfg.UsageDays != 180 || cfg.ShareDays != 90 || cfg.CheckpointDays != 30 {
		t.Fatalf("默认保留期必须是 180/90/30，实际 %#v", cfg)
	}
	// 未配置（零值）经 normalize 必须回落到默认，而不是被当成"保留 0 天 = 全删"。
	if got := (retentionConfig{}).normalize(); got != cfg {
		t.Fatalf("零值配置应回落默认值，实际 %#v", got)
	}
}

func TestRetentionConfigClampsOutOfRange(t *testing.T) {
	cfg := retentionConfig{UsageDays: -5, ShareDays: 999999, CheckpointDays: 1}.normalize()
	if cfg.UsageDays != retentionUsageDaysDefault {
		t.Fatalf("负数应回落默认，实际 %d", cfg.UsageDays)
	}
	if cfg.ShareDays != retentionMaxDays {
		t.Fatalf("超大值应夹到 %d，实际 %d", retentionMaxDays, cfg.ShareDays)
	}
	if cfg.CheckpointDays != 1 {
		t.Fatalf("合法值不应被改动，实际 %d", cfg.CheckpointDays)
	}
}

func TestRetentionTimeBoundary(t *testing.T) {
	cutoff := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	table := &fakeRetentionTable{rows: []time.Time{
		cutoff.Add(-time.Second), // 过期 1 秒 → 删
		cutoff,                   // 恰好等于边界 → **不删**
		cutoff.Add(time.Second),  // 未过期 → 不删
	}}
	stats := runRetentionPurge(context.Background(), table, "usage_events", cutoff, 100, false)
	if stats.Deleted != 1 {
		t.Fatalf("只应删除严格早于 cutoff 的 1 行，实际 deleted=%d", stats.Deleted)
	}
	if len(table.rows) != 2 {
		t.Fatalf("未过期与边界行必须保留，剩余 %d 行", len(table.rows))
	}
	for _, r := range table.rows {
		if r.Before(cutoff) {
			t.Fatalf("过期行仍残留: %v", r)
		}
	}
}

func TestRetentionDeletesInBatches(t *testing.T) {
	cutoff := time.Now().UTC()
	table := &fakeRetentionTable{}
	for i := 0; i < 1050; i++ {
		table.rows = append(table.rows, cutoff.Add(-time.Duration(i+1)*time.Hour))
	}
	table.rows = append(table.rows, cutoff.Add(time.Hour)) // 未过期，必须留下

	stats := runRetentionPurge(context.Background(), table, "usage_events", cutoff, 200, false)
	if stats.Deleted != 1050 {
		t.Fatalf("应删 1050 行，实际 %d", stats.Deleted)
	}
	// 1050 / 200 = 5 满批 + 1 不满批 → 6 批（最后一次返回 50 行后结束）。
	if stats.Batches != 6 {
		t.Fatalf("期望 6 批，实际 %d", stats.Batches)
	}
	if table.delCalls != 6 {
		t.Fatalf("每批应恰好一次 DELETE，实际 %d 次", table.delCalls)
	}
	if stats.Truncated {
		t.Fatalf("未达批次上限时不应标 Truncated")
	}
	if len(table.rows) != 1 || table.rows[0].Before(cutoff) {
		t.Fatalf("只剩未过期那 1 行，实际 %v", table.rows)
	}
}

func TestRetentionDryRunDeletesNothing(t *testing.T) {
	cutoff := time.Now().UTC()
	table := &fakeRetentionTable{}
	for i := 0; i < 250; i++ {
		table.rows = append(table.rows, cutoff.Add(-time.Duration(i+1)*time.Minute))
	}
	stats := runRetentionPurge(context.Background(), table, "conversation_shares", cutoff, 100, true)
	if stats.Matched != 250 {
		t.Fatalf("dry_run 应报告命中 250，实际 %d", stats.Matched)
	}
	if stats.Deleted != 0 {
		t.Fatalf("dry_run 不得删除，实际 deleted=%d", stats.Deleted)
	}
	if table.delCalls != 0 {
		t.Fatalf("dry_run 不得调用 DELETE，实际 %d 次", table.delCalls)
	}
	if len(table.rows) != 250 {
		t.Fatalf("dry_run 后行数不变，实际 %d", len(table.rows))
	}
}

func TestRetentionBatchCapMarksTruncated(t *testing.T) {
	table := &fakeRetentionTable{alwaysFull: true}
	stats := runRetentionPurge(context.Background(), table, "usage_events", time.Now(), 10, false)
	if stats.Batches != retentionMaxBatches {
		t.Fatalf("应停在批次上限 %d，实际 %d", retentionMaxBatches, stats.Batches)
	}
	if !stats.Truncated {
		t.Fatalf("达到批次上限必须标 Truncated（下一轮继续），不能假装删完")
	}
	if stats.Deleted != int64(retentionMaxBatches*10) {
		t.Fatalf("已删行数应如实累计，实际 %d", stats.Deleted)
	}
}

func TestRetentionErrorStopsAndReports(t *testing.T) {
	table := &fakeRetentionTable{failDelete: errors.New("boom")}
	stats := runRetentionPurge(context.Background(), table, "usage_events", time.Now(), 100, false)
	if stats.Error != "boom" {
		t.Fatalf("错误应写进 stats.Error，实际 %q", stats.Error)
	}
	if stats.Batches != 0 {
		t.Fatalf("失败的那一批不应计入批次数，实际 %d", stats.Batches)
	}
}
