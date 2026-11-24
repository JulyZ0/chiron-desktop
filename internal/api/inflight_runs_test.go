package api

import (
	"context"
	"sync"
	"testing"
	"time"
)

// ── H12：停机必须看见 detached run ───────────────────────────────────────────
//
// `/v1/agent/submit` 先回 202，再把回合交给 detached goroutine。`http.Server.Shutdown`
// 只等活跃连接，看不见这条 goroutine —— 于是停机时它被直接掐断，而客户端早已收到
// "已受理"。这一组把"看得见 / 等得到 / 等不到要说清楚"三条钉住。

func TestInFlightTrackAndDrain(t *testing.T) {
	r := &InFlightRuns{idle: make(chan struct{})}
	close(r.idle)

	if got := r.Count(); got != 0 {
		t.Fatalf("初始应为 0，得到 %d", got)
	}

	done1 := r.Track()
	done2 := r.Track()
	if got := r.Count(); got != 2 {
		t.Fatalf("登记两条后应为 2，得到 %d", got)
	}

	drained := make(chan bool, 1)
	go func() {
		ok, _ := r.Drain(context.Background())
		drained <- ok
	}()

	time.Sleep(20 * time.Millisecond)
	select {
	case <-drained:
		t.Fatal("还有 run 没跑完时 Drain 不应返回")
	default:
	}

	done1()
	if got := r.Count(); got != 1 {
		t.Fatalf("完成一条后应为 1，得到 %d", got)
	}
	done2()
	// done 幂等：重复调用不应把计数打到负数。
	done2()

	select {
	case ok := <-drained:
		if !ok {
			t.Fatal("全部完成后 Drain 应返回 true")
		}
	case <-time.After(2 * time.Second):
		t.Fatal("全部完成后 Drain 应被唤醒")
	}
	if got := r.Count(); got != 0 {
		t.Fatalf("结束后应为 0，得到 %d", got)
	}
}

func TestInFlightDrainTimesOutWithRemaining(t *testing.T) {
	r := &InFlightRuns{idle: make(chan struct{})}
	close(r.idle)

	_ = r.Track() // 故意不结束：模拟"跑太久"
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel()

	ok, remaining := r.Drain(ctx)
	if ok {
		t.Fatal("超时后不应报 drain 成功")
	}
	if remaining != 1 {
		t.Fatalf("应该报出剩余 1 条，得到 %d", remaining)
	}
}

func TestInFlightAllowsNewRunsWhileDraining(t *testing.T) {
	// 停机过程中仍可能有新提交进来（HTTP server 尚未完全停下）。
	// 用 WaitGroup 会 panic（Add 与 Wait 并发是禁止的），这个实现必须允许。
	r := &InFlightRuns{idle: make(chan struct{})}
	close(r.idle)

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		_, _ = r.Drain(ctx)
	}()

	for i := 0; i < 5; i++ {
		done := r.Track()
		go func() { time.Sleep(5 * time.Millisecond); done() }()
	}
	wg.Wait()

	if got := r.Count(); got != 0 {
		t.Fatalf("所有 run 结束后计数应归零，得到 %d", got)
	}
}

func TestInFlightNilReceiverIsSafe(t *testing.T) {
	var r *InFlightRuns
	r.Track()() // 不应 panic
	if got := r.Count(); got != 0 {
		t.Fatalf("nil 接收者 Count 应为 0，得到 %d", got)
	}
	if ok, remaining := r.Drain(context.Background()); !ok || remaining != 0 {
		t.Fatalf("nil 接收者 Drain 应视为已排空，得到 ok=%v remaining=%d", ok, remaining)
	}
}

func TestInFlightSingleton(t *testing.T) {
	if InFlight() == nil {
		t.Fatal("InFlight() 不应返回 nil")
	}
	if InFlight() != InFlight() {
		t.Fatal("InFlight() 必须是同一实例（停机流程与 submit 入口共享）")
	}
}
