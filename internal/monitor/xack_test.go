package monitor

import (
	"testing"
	"time"
)

// xackBackdate 把最近写入的第 n 条样本（1 = 最新）的时间改到 at。
//
// 同包测试允许触碰内部状态：采样器的写入入口 Record 只用 time.Now()，
// 而这一组用例要钉的正是**跨分钟边界**上的取舍 —— 不 backdate 就只能"睡到"
// 边界，测试会既慢又不稳。
func xackBackdate(x *XackWindow, n int, at time.Time) {
	x.mu.Lock()
	defer x.mu.Unlock()
	idx := x.pos - n
	for idx < 0 {
		idx += len(x.samples)
	}
	x.samples[idx%len(x.samples)].at = at
}

// TestXackRateTruncatesWindowAtCutoff 钉住"窗口是左闭右开区间"：
//
//   - 恰好落在 cutoff 上的样本算在内（不丢最后一条）；
//   - 早于 cutoff 的样本不算（否则「近 5 分钟」会悄悄变成「近 5 分钟 + 1」）。
//
// 越界的样本会让速率整体偏高；而速率正是队列面板上区分"积压大但正在消化"
// 与"积压大且停滞"的唯一依据。
func TestXackRateTruncatesWindowAtCutoff(t *testing.T) {
	x := NewXackWindow(16)
	const stream = "engine:tasks"

	// Rate 内部自己取 time.Now()，所以这里用"相对现在"的样本表达边界：
	// 一条在 5 分钟窗口内（-4m）、一条在窗口外（-6m），外加一条刚写入的。
	x.Record(stream)
	x.Record(stream)
	x.Record(stream)
	xackBackdate(x, 2, time.Now().Add(-4*time.Minute))
	xackBackdate(x, 3, time.Now().Add(-6*time.Minute))

	got := x.Rate(stream, 5*time.Minute)
	if got.Samples != 2 {
		t.Fatalf("5 分钟窗口应只算 2 条（-4m 与刚写入），实际 %d", got.Samples)
	}
	if !got.HasSamples {
		t.Fatalf("有样本时 HasSamples 必须为 true")
	}
	if got.WindowSeconds != 300 {
		t.Fatalf("WindowSeconds 应如实回报 300，实际 %d", got.WindowSeconds)
	}

	// 窗口超过保留期一律按保留期处理：不能让"近 1 小时"这个标签覆盖不存在的样本。
	wide := x.Rate(stream, 24*time.Hour)
	if wide.WindowSeconds != int(XackWindowRetention.Seconds()) {
		t.Fatalf("超保留期的窗口应夹到 %v，实际 %ds", XackWindowRetention, wide.WindowSeconds)
	}
	// 非法窗口（<=0）同样夹到保留期，而不是按 0 秒折算成 +Inf。
	zero := x.Rate(stream, 0)
	if zero.WindowSeconds != int(XackWindowRetention.Seconds()) {
		t.Fatalf("window<=0 应夹到保留期，实际 %ds", zero.WindowSeconds)
	}
}

// TestXackRateUsesActualSpan 钉住"速率按真实跨度折算"：
//
// 进程刚启动 30 秒、ack 了 30 条时，正确的读数是 60/min，而不是 30/min
//（拿 60 秒硬除）也不是 +Inf（跨度 0）。窗口不足一分钟的读数必须能解释。
func TestXackRateUsesActualSpan(t *testing.T) {
	x := NewXackWindow(64)
	const stream = "engine:tasks"
	for i := 0; i < 30; i++ {
		x.Record(stream)
	}
	// 最早一条改到 30 秒前：跨度 = 30s，30 条 ⇒ 约 60/min。
	xackBackdate(x, 30, time.Now().Add(-30*time.Second))

	got := x.Rate(stream, 5*time.Minute)
	if got.Samples != 30 {
		t.Fatalf("样本数应为 30，实际 %d", got.Samples)
	}
	if got.PerMinutePerSecond < 55 || got.PerMinutePerSecond > 65 {
		t.Fatalf("30 条 / 30s 应折算约 60/min，实际 %.2f", got.PerMinutePerSecond)
	}

	// 全部样本都在"现在"：跨度被夹到 1 秒，速率有限且不为负。
	one := NewXackWindow(8)
	for i := 0; i < 3; i++ {
		one.Record(stream)
	}
	tiny := one.Rate(stream, time.Minute)
	if tiny.PerMinutePerSecond <= 0 || tiny.PerMinutePerSecond > 3*60 {
		t.Fatalf("跨度被夹到 1s 后速率应有限且 >0，实际 %.2f", tiny.PerMinutePerSecond)
	}
}

// TestXackRateEmptyWindow 钉住"0 ≠ 没采样"：没有样本时 HasSamples=false，
// 前端据此显示「—」，而不是把"本副本没有 ack 采样"读成"消费者挂了"。
func TestXackRateEmptyWindow(t *testing.T) {
	x := NewXackWindow(4)
	got := x.Rate("engine:tasks", 5*time.Minute)
	if got.HasSamples || got.Samples != 0 || got.PerMinutePerSecond != 0 {
		t.Fatalf("空采样器必须报无样本，实际 %#v", got)
	}
}

// TestXackPerMinuteTruncatesAcrossMinuteBoundary 钉住自然分钟桶的**截断**语义：
//
//   - 桶数恒为 minutes（末尾是未走完的这一分钟），空桶填 0；
//   - 最早那条桶的起点（minuteStart - (minutes-1)m）**包含**边界样本，
//     再早 1ns 就落在桶外 —— 这就是"跨分钟窗口截断"；
//   - 别的流的样本不计入（面板按 stream 分开看）。
//
// 写错这里的表现是趋势图上"最后一根柱子忽然变高/变空"，而总数看起来还是对的。
func TestXackPerMinuteTruncatesAcrossMinuteBoundary(t *testing.T) {
	x := NewXackWindow(64)
	const stream = "engine:tasks"
	const other = "webhook:events"

	minuteStart := time.Now().Truncate(time.Minute)
	from := minuteStart.Add(-14 * time.Minute) // minutes=15 时最早桶的起点

	x.Record(stream) // 当前分钟
	x.Record(stream)
	x.Record(other) // 别的流：不该出现在 stream 的桶里

	x.Record(stream)
	xackBackdate(x, 1, from) // 恰好落在最早桶边界上

	x.Record(stream)
	xackBackdate(x, 1, from.Add(-time.Nanosecond)) // 早 1ns：在 15 个桶之外

	x.Record(stream)
	xackBackdate(x, 1, minuteStart.Add(-time.Minute)) // 上一分钟

	got := x.PerMinute(stream, 15)
	if len(got) != 15 {
		t.Fatalf("桶数恒为 15，实际 %d", len(got))
	}
	var total int64
	for _, n := range got {
		total += n
	}
	// 计入：当前分钟 2 条 + 边界 1 条 + 上一分钟 1 条 = 4（other 与 -1ns 不算）。
	if total != 4 {
		t.Fatalf("15 个桶内应共 4 条，实际 %d（%v）", total, got)
	}
	if got[0] != 1 {
		t.Errorf("最早桶必须包含恰好落在边界上的样本，实际 %d", got[0])
	}
	if got[13] != 1 {
		t.Errorf("上一分钟桶应有 1 条，实际 %d", got[13])
	}
	if got[14] != 2 {
		t.Errorf("当前分钟桶应有 2 条，实际 %d", got[14])
	}
	if got[7] != 0 {
		t.Errorf("空桶应为 0，实际 %d", got[7])
	}

	// 非法 minutes：夹到默认 15，而不是返回空切片（前端会画成"没有数据"）。
	if n := len(x.PerMinute(stream, 0)); n != 15 {
		t.Fatalf("minutes<=0 应夹到 15，实际 %d", n)
	}
}

// TestXackPerMinuteDropsSamplesOlderThanRetention：超出保留期的样本不进桶。
// 否则重启前的旧 ack 会以"最后一分钟"的身份重新出现在趋势上。
func TestXackPerMinuteDropsSamplesOlderThanRetention(t *testing.T) {
	x := NewXackWindow(8)
	x.Record("engine:tasks")
	xackBackdate(x, 1, time.Now().Add(-2*XackWindowRetention))

	got := x.PerMinute("engine:tasks", 15)
	for i, n := range got {
		if n != 0 {
			t.Fatalf("超保留期样本不应出现在桶 %d（%v）", i, got)
		}
	}
	if rate := x.Rate("engine:tasks", 15*time.Minute); rate.HasSamples {
		t.Fatalf("超保留期样本不应计入速率，实际 %#v", rate)
	}
}
