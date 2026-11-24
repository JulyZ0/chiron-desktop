package api

// 已受理但仍在跑的 run（H12）。
//
// 为什么需要这个登记表：`/v1/agent/submit` 先把 202 回给客户端，再把真正的回合交给
// **detached goroutine** 跑（见 routes_agent.go）。`http.Server.Shutdown` 只看得到
// 活跃连接 —— 这条 goroutine 不在任何连接的生命周期里，于是：
//
//   - 停机时它被直接掐断（进程退出），而网关已经回过"已受理"；
//   - 运维侧看到的是"优雅停机成功"，用户侧看到的是"这一轮没结果"。
//
// 登记表让停机流程能**看见**这些 run：要么等它们跑完（drain），要么明确承认没等完，
// 由引擎侧的 checkpoint 与 run 租约（TTL）接管恢复 —— 关键是不留"不知道发生了什么"。

import (
	"context"
	"log/slog"
	"sync"
)

// InFlightRuns 统计"已回 202、还没跑完"的回合。
//
// 用互斥量 + 计数而不是 `sync.WaitGroup`：WaitGroup 的 `Wait` 与 `Add` 并发是不允许的
// （停机时正好有新提交进来就会 panic），而这里必须允许"停机中途仍有新 run"。
type InFlightRuns struct {
	mu      sync.Mutex
	runs    int
	idle    chan struct{} // runs 归零时关闭；有新 run 时重建
	waiting bool
}

var (
	inFlightOnce sync.Once
	inFlightRuns *InFlightRuns
)

// InFlight 返回进程级登记表（惰性初始化，便于测试与多入口复用）。
func InFlight() *InFlightRuns {
	inFlightOnce.Do(func() {
		inFlightRuns = &InFlightRuns{idle: make(chan struct{})}
		close(inFlightRuns.idle)
	})
	return inFlightRuns
}

// Track 登记一次 run，返回的 done 必须在 goroutine 结束时调用（幂等）。
func (r *InFlightRuns) Track() func() {
	if r == nil {
		return func() {}
	}
	r.mu.Lock()
	if r.runs == 0 {
		// 从空闲转入忙碌：换一个新的 idle 通道（旧的那个已经关闭，等待者已被唤醒）。
		r.idle = make(chan struct{})
	}
	r.runs++
	r.mu.Unlock()

	var once sync.Once
	return func() {
		once.Do(func() {
			r.mu.Lock()
			defer r.mu.Unlock()
			r.runs--
			if r.runs <= 0 {
				r.runs = 0
				select {
				case <-r.idle: // 已经关过
				default:
					close(r.idle)
				}
			}
		})
	}
}

// Count 返回当前在跑数（管理端/日志/测试用）。
func (r *InFlightRuns) Count() int {
	if r == nil {
		return 0
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.runs
}

// Drain 等待在跑回合结束，最多等到 ctx 到期。
//
// 返回 (drained, remaining)：
//   - drained=true 表示全部跑完（可以干净退出）；
//   - drained=false 时 remaining>0，调用方**必须**把"这一轮将由 checkpoint/租约恢复"
//     说清楚（不是静默丢弃）。这正是 H12 要求的"要么 drain，要么明确进入恢复态"。
func (r *InFlightRuns) Drain(ctx context.Context) (bool, int) {
	if r == nil {
		return true, 0
	}
	for {
		r.mu.Lock()
		runs := r.runs
		idle := r.idle
		r.mu.Unlock()

		if runs == 0 {
			return true, 0
		}
		select {
		case <-idle:
			// 归零后可能又有新 run 进来：回到循环顶部复查。
		case <-ctx.Done():
			r.mu.Lock()
			remaining := r.runs
			r.mu.Unlock()
			return remaining == 0, remaining
		}
	}
}

// DrainInFlightRuns 是给停机流程用的薄封装（带上"没等完"的显式日志）。
//
// 日志措辞是刻意的：它必须能让值班的人一眼判断"接下来由谁接手"，而不是一句
// "shutdown timeout"。
func DrainInFlightRuns(ctx context.Context) {
	r := InFlight()
	count := r.Count()
	if count == 0 {
		return
	}
	slog.Info("waiting for in-flight agent runs", "runs", count)
	drained, remaining := r.Drain(ctx)
	if drained {
		slog.Info("in-flight agent runs drained")
		return
	}
	slog.Warn(
		"in-flight agent runs did NOT drain before shutdown "+"—— they are handed to recovery: "+
			"engine checkpoints are persisted per turn and the session run lock expires by TTL (5min), "+
			"so the next submit resumes from the last checkpoint instead of silently losing the turn",
		"remaining", remaining)
}
