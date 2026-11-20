package monitor

import (
	"testing"
	"time"
)

// TestRequestWindowReportsNoSamples 钉住"0 ≠ 没数据"：
// 空窗口必须 HasSamples=false，让调用方显示「无采样数据」，而不是 P99=0ms。
func TestRequestWindowReportsNoSamples(t *testing.T) {
	w := NewRequestWindow(16)
	snap := w.Snapshot(5 * time.Minute)
	if snap.HasSamples {
		t.Fatalf("空采样器不该报告有样本: %#v", snap)
	}
	if snap.Samples != 0 {
		t.Fatalf("空采样器样本数应为 0，得到 %d", snap.Samples)
	}
	if len(snap.Buckets) != 0 {
		t.Errorf("无样本时不生成趋势桶，得到 %d 个", len(snap.Buckets))
	}
}

// TestRequestWindowPercentilesAndErrorRate：分位数与错误率来自真实样本。
func TestRequestWindowPercentilesAndErrorRate(t *testing.T) {
	w := NewRequestWindow(100)
	for i := 1; i <= 100; i++ {
		status := 200
		if i > 95 {
			status = 500
		}
		w.Record(time.Duration(i)*time.Millisecond, status)
	}
	snap := w.Snapshot(15 * time.Minute)
	if snap.Samples != 100 {
		t.Fatalf("样本数应为 100，得到 %d", snap.Samples)
	}
	if snap.ServerErrors != 5 {
		t.Errorf("第 96-100 条是 500，server_errors 应为 5，得到 %d", snap.ServerErrors)
	}
	if snap.ErrorRatePct != 5 {
		t.Errorf("错误率应为 5%%，得到 %v", snap.ErrorRatePct)
	}
	// 1..100ms 均匀分布：P50≈50-51、P95≈95-96、P99≈99-100。
	if snap.P50Ms < 49 || snap.P50Ms > 52 {
		t.Errorf("P50 应约为 50ms，得到 %v", snap.P50Ms)
	}
	if snap.P95Ms < 94 || snap.P95Ms > 97 {
		t.Errorf("P95 应约为 95ms，得到 %v", snap.P95Ms)
	}
	if snap.P99Ms < 98 || snap.P99Ms > 100 {
		t.Errorf("P99 应约为 99ms，得到 %v", snap.P99Ms)
	}
	if snap.MaxMs != 100 {
		t.Errorf("Max 应为 100ms，得到 %v", snap.MaxMs)
	}
	if len(snap.Buckets) != 24 {
		t.Errorf("趋势应为固定 24 桶，得到 %d", len(snap.Buckets))
	}
}

// TestRequestWindowExcludesWindowBoundarySamples：窗口外的样本不计入。
func TestRequestWindowExcludesWindowBoundarySamples(t *testing.T) {
	w := NewRequestWindow(8)
	w.Record(time.Millisecond, 200)
	// 直接把内部样本改老（同包测试，允许触碰内部状态）。
	w.mu.Lock()
	w.samples[(w.pos-1+len(w.samples))%len(w.samples)].At = time.Now().Add(-2 * time.Hour)
	w.mu.Unlock()

	if snap := w.Snapshot(5 * time.Minute); snap.HasSamples {
		t.Fatalf("2 小时前的样本不该进入 5 分钟窗口: %#v", snap)
	}
}

// TestRequestWindowSkipsUpgradeResponses：101/304 不计入延迟分位数。
func TestRequestWindowSkipsUpgradeResponses(t *testing.T) {
	w := NewRequestWindow(8)
	w.Record(10*time.Millisecond, 101)
	snap := w.Snapshot(time.Minute)
	if !snap.HasSamples {
		t.Fatal("101 仍是一条样本，HasSamples 应为 true")
	}
	if snap.P99Ms != 0 {
		t.Errorf("没有可统计延迟的样本时 P99 应为 0（前端按 HasSamples 显示未知），得到 %v", snap.P99Ms)
	}
}
