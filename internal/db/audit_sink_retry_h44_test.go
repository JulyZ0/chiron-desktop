package db

import (
	"context"
	"errors"
	"testing"
	"time"
)

// ── H44：审计 sink 失败必须**回队重试**，不能整批丢弃 ────────────────────────
//
// 旧行为：`writer` 失败 → slog.Error + 丢掉整批。审计是事后追溯的唯一凭据，
// 一次 PG 抖动就能让那段时间的登录/导出/删除记录永久消失，日志里只有一行 error。

func newTestSink(writer func(ctx context.Context, entries []AuditEntry) error) *AuditSink {
	// window 给大一点：测试里手动 Flush，不靠后台 ticker 干扰。
	return NewAuditSink(writer, 100, time.Hour)
}

func entry(action string) AuditEntry {
	return AuditEntry{Action: action, UserID: "u1", TenantID: "1", Resource: "/v1/x", Success: true}
}

func TestAuditSinkRetriesFailedBatchInsteadOfDropping(t *testing.T) {
	ctx := context.Background()
	var attempts int
	var got []AuditEntry
	sink := newTestSink(func(_ context.Context, entries []AuditEntry) error {
		attempts++
		if attempts == 1 {
			return errors.New("pg down")
		}
		got = append(got, entries...)
		return nil
	})
	defer sink.Close()

	_ = sink.Handle(ctx, entry("login"))
	sink.Flush(ctx) // 第一次：失败 → 回队

	stats := sink.Stats()
	if stats.Dropped != 0 {
		t.Fatalf("首次失败不该丢数据: %+v", stats)
	}
	if stats.Buffered != 1 {
		t.Fatalf("失败批应回到缓冲: %+v", stats)
	}
	if stats.Failures != 1 {
		t.Fatalf("失败次数应可见: %+v", stats)
	}

	sink.Flush(ctx) // 第二次：成功
	stats = sink.Stats()
	if stats.Written != 1 || stats.Buffered != 0 {
		t.Fatalf("重试成功后应写入并清空缓冲: %+v", stats)
	}
	if len(got) != 1 || got[0].Action != "login" {
		t.Fatalf("重试必须带上原条目: %+v", got)
	}
}

func TestAuditSinkKeepsOrderWhenRequeueing(t *testing.T) {
	ctx := context.Background()
	failFirst := true
	var orders []string
	sink := newTestSink(func(_ context.Context, entries []AuditEntry) error {
		if failFirst {
			failFirst = false
			return errors.New("pg down")
		}
		for _, e := range entries {
			orders = append(orders, e.Action)
		}
		return nil
	})
	defer sink.Close()

	_ = sink.Handle(ctx, entry("a"))
	sink.Flush(ctx) // a 失败回队
	_ = sink.Handle(ctx, entry("b"))
	sink.Flush(ctx) // 重试 a + 写入 b

	if len(orders) != 2 || orders[0] != "a" || orders[1] != "b" {
		t.Fatalf("重试批次必须排在新条目之前（时间顺序）: %v", orders)
	}
}

func TestAuditSinkBufferIsBoundedAndDropsOldest(t *testing.T) {
	ctx := context.Background()
	sink := newTestSink(func(_ context.Context, _ []AuditEntry) error {
		return errors.New("pg down forever")
	})
	defer sink.Close()

	limit := sink.batchSize * maxBufferFactor
	for i := 0; i < limit+5; i++ {
		_ = sink.Handle(ctx, entry("x"))
		sink.Flush(ctx) // 每次都失败 ⇒ 不断回队
	}

	stats := sink.Stats()
	if stats.Buffered > int64(limit) {
		t.Fatalf("缓冲必须有界（上限 %d），实际 %d", limit, stats.Buffered)
	}
	if stats.Dropped == 0 {
		t.Fatal("超上限时必须记录丢弃数量（否则丢失不可见）")
	}
}

func TestAuditSinkStatsCountsSuccesses(t *testing.T) {
	ctx := context.Background()
	sink := newTestSink(func(_ context.Context, _ []AuditEntry) error { return nil })
	defer sink.Close()

	for i := 0; i < 3; i++ {
		_ = sink.Handle(ctx, entry("ok"))
	}
	sink.Flush(ctx)

	stats := sink.Stats()
	if stats.Written != 3 || stats.Dropped != 0 || stats.Failures != 0 {
		t.Fatalf("成功路径统计不对: %+v", stats)
	}
}

func TestAuditSinkHandleNeverBlocksCaller(t *testing.T) {
	// Handle 必须返回 nil（消费者据此 ACK）：失败由 Flush 内部回队处理，
	// 不能把错误抛回去导致毒消息在消费组里无限重投。
	ctx := context.Background()
	sink := newTestSink(func(_ context.Context, _ []AuditEntry) error {
		return errors.New("boom")
	})
	defer sink.Close()

	if err := sink.Handle(ctx, entry("login")); err != nil {
		t.Fatalf("Handle 必须恒返回 nil，得到 %v", err)
	}
}
