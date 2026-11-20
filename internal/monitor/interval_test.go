package monitor

import (
	"testing"
	"time"
)

// requestWindowBackdate 把最近写入的第 n 条请求样本（1 = 最新）的时间改到 at。
// 同包测试允许触碰内部状态（与 xack_test.go 的 xackBackdate 同一手法）：
// RecordClass 只写 time.Now()，而这里要钉的是**区间端点**的取舍。
func requestWindowBackdate(w *RequestWindow, n int, at time.Time) {
	w.mu.Lock()
	defer w.mu.Unlock()
	idx := w.pos - n
	for idx < 0 {
		idx += len(w.samples)
	}
	w.samples[idx%len(w.samples)].At = at
}

// TestIntervalStatsClassifiesClientErrorsWithoutInflatingErrorRate 钉住分钟聚合的
// 错误率口径：**4xx 计数但不计错误率**，5xx 同时进 count 与 errors。
//
// 为什么这条最要紧：rollup 表只有 count / errors 两列，历史趋势上的错误率就是
// errors/count。如果把 4xx 也计入 errors，一次客户端 401 风暴会把面板变成
// "服务端故障"，而它明明不是。
func TestIntervalStatsClassifiesClientErrorsWithoutInflatingErrorRate(t *testing.T) {
	w := NewRequestWindow(64)
	const class = "/v1/chat"
	for i := 0; i < 5; i++ {
		w.RecordClass(class, 10*time.Millisecond, 200)
	}
	for i := 0; i < 3; i++ {
		w.RecordClass(class, 20*time.Millisecond, 404) // 4xx：只计数
	}
	for i := 0; i < 2; i++ {
		w.RecordClass(class, 30*time.Millisecond, 500) // 5xx：计数 + errors
	}

	from, to := time.Now().Add(-time.Minute), time.Now().Add(time.Minute)
	st := w.SnapshotIntervalByClass(from, to)[class]
	if st.Count != 10 {
		t.Fatalf("4xx 必须计入 count，实际 count=%d", st.Count)
	}
	if st.Errors != 2 {
		t.Fatalf("errors 只统计 5xx，实际 %d", st.Errors)
	}
	if !st.HasSamples {
		t.Fatalf("有样本时 HasSamples 必须为 true")
	}
	// 错误率 = 5xx / 全部样本 = 20%，不是 50%（把 4xx 算进去）。
	if rate := float64(st.Errors) / float64(st.Count) * 100; rate != 20 {
		t.Fatalf("错误率应为 20%%，实际 %v", rate)
	}

	// 合并视图（不分分类）与分分类合计必须一致：面板上"总计"与表格逐行相加要对得上。
	all := w.SnapshotInterval(from, to)
	if all.Count != st.Count || all.Errors != st.Errors {
		t.Fatalf("合并视图 %+v 与分类视图 %+v 不一致", all, st)
	}
}

// TestIntervalStatsP95Boundaries 覆盖 p95 的三种边界：单请求 / 多请求 / 空窗口。
//
// 单请求是最容易写错的：`pick(1, 95)` 必须回落到唯一样本，而不是越界 panic
// 或返回 0（0 会被读成"这一分钟非常快"）。
func TestIntervalStatsP95Boundaries(t *testing.T) {
	now := time.Now()
	from, to := now.Add(-time.Minute), now.Add(time.Minute)

	t.Run("单请求", func(t *testing.T) {
		w := NewRequestWindow(8)
		w.RecordClass("/healthz", 7*time.Millisecond, 200)
		st := w.SnapshotInterval(from, to)
		if st.P95Ms != 7 {
			t.Fatalf("单请求的 p95 就是它自己（7ms），实际 %v", st.P95Ms)
		}
	})

	t.Run("多请求", func(t *testing.T) {
		w := NewRequestWindow(256)
		for i := 1; i <= 100; i++ {
			w.RecordClass("/v1/chat", time.Duration(i)*time.Millisecond, 200)
		}
		st := w.SnapshotInterval(from, to)
		// nearest-rank：ceil(0.95*100)=95 号样本 ⇒ 95ms。
		if st.P95Ms != 95 {
			t.Fatalf("1..100ms 均匀分布时 p95 应为 95ms，实际 %v", st.P95Ms)
		}
	})

	t.Run("空窗口", func(t *testing.T) {
		w := NewRequestWindow(8)
		w.RecordClass("/v1/chat", 10*time.Millisecond, 200)
		// 窗口整段在过去：不返回 0 延迟 + HasSamples=true 的组合。
		st := w.SnapshotInterval(now.Add(-2*time.Hour), now.Add(-time.Hour))
		if st.HasSamples || st.Count != 0 || st.P95Ms != 0 {
			t.Fatalf("窗口外无样本时应报空（HasSamples=false, p95=0），实际 %+v", st)
		}
		if byClass := w.SnapshotIntervalByClass(now.Add(-2*time.Hour), now.Add(-time.Hour)); len(byClass) != 0 {
			t.Fatalf("空窗口不该返回任何分类键，实际 %#v", byClass)
		}
		// 真实空采样器同理。
		if st := NewRequestWindow(8).SnapshotInterval(from, to); st.HasSamples || st.P95Ms != 0 {
			t.Fatalf("空采样器应报空，实际 %+v", st)
		}
	})

	t.Run("只有不可计耗时的响应", func(t *testing.T) {
		w := NewRequestWindow(8)
		w.RecordClass("/ws", 5*time.Millisecond, 101)
		w.RecordClass("/cached", 5*time.Millisecond, 304)
		st := w.SnapshotInterval(from, to)
		if !st.HasSamples || st.Count != 2 {
			t.Fatalf("101/304 仍是样本（count=2），实际 %+v", st)
		}
		if st.P95Ms != 0 {
			t.Fatalf("没有可计耗时的样本时 p95 应保持 0，实际 %v", st.P95Ms)
		}
	})
}

// TestIntervalIsHalfOpen 钉住 [from, to) 的左闭右开语义 —— 这正是"tail 与 rollup
// 按整分钟边界拼接、互不重复"的基础：
//
//   - 恰好等于 from 的样本属于本区间；
//   - 恰好等于 to 的样本属于**下一个**区间（上一分钟的最后一纳秒在 rollup，
//     整分钟那一刻归 tail）；
//   - 两侧各错 1ns 都落在区间外。
func TestIntervalIsHalfOpen(t *testing.T) {
	base := time.Now().Truncate(time.Minute)
	from, to := base.Add(-time.Minute), base

	cases := []struct {
		name string
		at   time.Time
		want bool
	}{
		{"from 边界（含）", from, true},
		{"from 前 1ns（不含）", from.Add(-time.Nanosecond), false},
		{"to 前 1ns（含）", to.Add(-time.Nanosecond), true},
		{"to 边界（不含，属下一区间）", to, false},
	}
	for _, tc := range cases {
		w := NewRequestWindow(8)
		w.RecordClass("/v1/chat", 10*time.Millisecond, 200)
		requestWindowBackdate(w, 1, tc.at)
		if got := w.SnapshotInterval(from, to).Count == 1; got != tc.want {
			t.Errorf("%s: 期望计入=%v，实际 %v", tc.name, tc.want, got)
		}
	}

	// 相邻区间不重叠：同一条样本最多被一个区间看到。
	w := NewRequestWindow(8)
	w.RecordClass("/v1/chat", 10*time.Millisecond, 200)
	requestWindowBackdate(w, 1, to)
	before := w.SnapshotInterval(from, to).Count
	after := w.SnapshotInterval(to, to.Add(time.Minute)).Count
	if before+after != 1 {
		t.Fatalf("相邻半开区间必须恰好覆盖一条样本一次，实际 before=%d after=%d", before, after)
	}
}

// TestSnapshotIntervalMergesClassesWithMaxP95 合并多分类时 p95 取最大值：
// 分位数不可加，取最大是保守近似（"尾延迟不会比任何一类更差"），
// 与本仓接口注释里写的口径一致。
func TestSnapshotIntervalMergesClassesWithMaxP95(t *testing.T) {
	w := NewRequestWindow(32)
	for i := 0; i < 4; i++ {
		w.RecordClass("/v1/chat", 10*time.Millisecond, 200)
	}
	for i := 0; i < 2; i++ {
		w.RecordClass("/v1/admin", 300*time.Millisecond, 200)
	}
	st := w.SnapshotInterval(time.Now().Add(-time.Minute), time.Now().Add(time.Minute))
	if st.Count != 6 || st.Errors != 0 {
		t.Fatalf("合并应相加 count（6）与 errors（0），实际 %+v", st)
	}
	if st.P95Ms != 300 {
		t.Fatalf("合并 p95 应取各分类最大值 300ms，实际 %v", st.P95Ms)
	}
}
