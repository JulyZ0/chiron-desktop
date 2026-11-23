package api

// S4：每用户并发配额（无 Redis 时回退进程内信号量）。

import (
	"context"
	"testing"
)

func TestUserSemaphorePoolPerUserLimit(t *testing.T) {
	pool := NewUserSemaphorePool(nil, 2) // nil Redis → 进程内
	ctx := context.Background()

	r1, ok1 := pool.TryAcquire(ctx, "t", "u1")
	r2, ok2 := pool.TryAcquire(ctx, "t", "u1")
	if !ok1 || !ok2 {
		t.Fatal("同一用户前两个槽位应获取成功")
	}
	if _, ok := pool.TryAcquire(ctx, "t", "u1"); ok {
		t.Fatal("同一用户第三个槽位应被拒绝")
	}
	// 另一个用户不受影响
	r3, ok3 := pool.TryAcquire(ctx, "t", "u2")
	if !ok3 {
		t.Fatal("不同用户应各自独立计数")
	}
	// 释放后可重新获取
	r1()
	if _, ok := pool.TryAcquire(ctx, "t", "u1"); !ok {
		t.Fatal("释放后应能重新获取")
	}
	r2()
	r3()
}

func TestUserSemaphorePoolUnlimited(t *testing.T) {
	pool := NewUserSemaphorePool(nil, 0)
	for i := 0; i < 10; i++ {
		release, ok := pool.TryAcquire(context.Background(), "t", "u")
		if !ok {
			t.Fatalf("limit=0 应不限制，第 %d 次被拒", i+1)
		}
		release()
	}
}
