package api

import (
	"context"
	"strings"
	"testing"
)

// TestSemaphoreRenewRefcount 覆盖续期循环的引用计数：
// 首个槽位启动、后续槽位复用同一个循环、最后一个槽位释放时停止。
func TestSemaphoreRenewRefcount(t *testing.T) {
	s := NewSharedSemaphore(nil, "test", 3)

	s.trackRenewal()
	if s.heldSlots != 1 {
		t.Fatalf("首个槽位后 heldSlots = %d, want 1", s.heldSlots)
	}
	if s.renewCancel == nil {
		t.Fatal("首个槽位应启动续期循环")
	}
	// 循环唯一性无法直接断言（CancelFunc 只能与 nil 比较）；它由 trackRenewal 里
	// `heldSlots != 1 → return` 的守卫保证，这里只验证计数与"归零即停"。

	s.trackRenewal()
	s.trackRenewal()
	if s.heldSlots != 3 {
		t.Fatalf("三个槽位后 heldSlots = %d, want 3", s.heldSlots)
	}
	if s.renewCancel == nil {
		t.Fatal("多个槽位在持有时续期循环应仍然存在")
	}

	s.untrackRenewal()
	s.untrackRenewal()
	if s.heldSlots != 1 {
		t.Fatalf("释放两个后 heldSlots = %d, want 1", s.heldSlots)
	}
	if s.renewCancel == nil {
		t.Fatal("仍有槽位在持有时不应停止续期循环")
	}

	s.untrackRenewal()
	if s.heldSlots != 0 {
		t.Fatalf("全部释放后 heldSlots = %d, want 0", s.heldSlots)
	}
	if s.renewCancel != nil {
		t.Fatal("最后一个槽位释放后应停止续期循环")
	}
}

// TestSemaphoreRenewRefcountNeverGoesNegative release 被重复调用（或未获取就调用）时
// 不得把计数减成负数 —— 否则下一次 track 会误判为"非首个槽位"而不再启动续期。
func TestSemaphoreRenewRefcountNeverGoesNegative(t *testing.T) {
	s := NewSharedSemaphore(nil, "test", 1)

	s.untrackRenewal() // 未持有就释放
	if s.heldSlots != 0 {
		t.Fatalf("多余的释放后 heldSlots = %d, want 0", s.heldSlots)
	}

	s.trackRenewal()
	s.untrackRenewal()
	s.untrackRenewal() // release 闭包虽用 once 保护，这里仍锁住裸方法的行为
	if s.heldSlots != 0 {
		t.Fatalf("重复释放后 heldSlots = %d, want 0", s.heldSlots)
	}
	if s.renewCancel != nil {
		t.Fatal("计数归零后不应残留续期循环")
	}
}

// TestSemaphoreReleaseLuaGuardsAgainstNegativeCount 锁住"先读再减"的释放语义。
//
// 原实现是无条件 DECR：key 已因 TTL 消失时 DECR 会**创建 -1**，后续 acquire 从负数
// 起步 —— 等于白送槽位，而且偏差会一直留着。这是与续期缺陷同源的第二个 bug。
func TestSemaphoreReleaseLuaGuardsAgainstNegativeCount(t *testing.T) {
	if !strings.Contains(semReleaseLua, "GET") {
		t.Fatal("release 脚本必须先读计数：key 过期后无条件 DECR 会创建负值")
	}
	if !strings.Contains(semReleaseLua, "<= 0") {
		t.Fatal("release 脚本必须挡掉已 <= 0 的计数")
	}
}

// TestSemaphoreUnlimitedSkipsRenewal limit = 0 表示不限制，不占用 Redis 槽位。
func TestSemaphoreUnlimitedSkipsRenewal(t *testing.T) {
	s := NewSharedSemaphore(nil, "unlimited", 0)
	release, ok := s.TryAcquire(context.Background())
	if !ok {
		t.Fatal("limit=0 应恒可获取")
	}
	if s.heldSlots != 0 || s.renewCancel != nil {
		t.Fatal("不限制模式不占用槽位，不应登记续期")
	}
	release()
}

// TestSemaphoreLocalFallbackSkipsRenewal rdb = nil 时走进程内兜底：
// 没有 Redis 键需要续期，且本地槽位的获取/释放仍然成对。
func TestSemaphoreLocalFallbackSkipsRenewal(t *testing.T) {
	s := NewSharedSemaphore(nil, "local", 1)

	release, ok := s.TryAcquire(context.Background())
	if !ok {
		t.Fatal("本地槽位应可获取")
	}
	if s.heldSlots != 0 || s.renewCancel != nil {
		t.Fatal("本地兜底路径不应登记 Redis 续期")
	}

	// 容量为 1：未释放前第二次获取必须失败
	if _, ok := s.TryAcquire(context.Background()); ok {
		t.Fatal("本地容量为 1 时第二次获取应失败")
	}

	release()
	release2, ok := s.TryAcquire(context.Background())
	if !ok {
		t.Fatal("释放后应能再次获取")
	}
	release2()
}

// TestSemaphoreRenewIntervalBelowTTL 续期间隔必须显著小于 TTL，
// 否则一次抖动错过 tick 就足以让槽位被回收（这正是修复前的失效路径）。
func TestSemaphoreRenewIntervalBelowTTL(t *testing.T) {
	if semaphoreRenew >= semaphoreTTL {
		t.Fatalf("semaphoreRenew(%v) 必须小于 semaphoreTTL(%v)", semaphoreRenew, semaphoreTTL)
	}
	if semaphoreTTL/semaphoreRenew < 2 {
		t.Fatalf("续期间隔应至少比 TTL 小 2 倍以上（当前 TTL/间隔 = %d）", semaphoreTTL/semaphoreRenew)
	}
}
