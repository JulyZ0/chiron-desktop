package monitor

import (
	"math"
	"sort"
	"sync"
	"time"
)

// ── 请求窗口采样（可解释的 P50/P95/P99 与错误率） ──
//
// 为什么需要这一层：`Histogram`（metrics.go）只保留**最近 N 条**样本，没有时间维度。
// 于是看板上写「近 15 分钟 P99」时，实际算的是「最近 1000 个请求」—— 流量一低，
// 那 1000 条可能横跨几小时，窗口是假的；而「近 15 分钟请求量 / 错误率」更是完全
// 拿不出来（计数器只有进程启动以来的累计值）。
//
// 这里用固定容量环形缓冲保留**带时间戳的状态码与耗时**：
//   - Record 由 MonitoringMiddleware 在请求结束时调用（一次写）；
//   - Snapshot(window) 按时间过滤后现算分位数与错误率，返回样本数，调用方据此
//     区分「0」与「无采样数据」。
//
// 保留期 60 分钟、容量 20000 条：15 分钟档要覆盖得住的真实流量（>20rps 会被截断，
// 此时样本数本身就是答案：面板上标注「最近 N 条样本」而不是假装覆盖全窗口）。

// RequestSampleWindowMax 是环缓冲容量。
const RequestSampleWindowMax = 20000

// RequestSampleRetention 是样本保留期。超过它的样本在 Record 时被丢弃。
const RequestSampleRetention = 60 * time.Minute

// RequestSample 是一次已完成请求的采样。
//
// Class 是路由分类（见 api.routeClassOf），供分钟级落库按 route_class 分组；
// 为空时由落库方归一到 "default"。它是**低基数**的分类（路径首段），
// 不会像完整路径那样把聚合表撑成每请求一行。
type RequestSample struct {
	At       time.Time
	Duration time.Duration
	Status   int
	Class    string
}

// WindowBucket 是趋势图的一个时间桶。
type WindowBucket struct {
	At      time.Time `json:"at"`
	Count   int64     `json:"count"`
	Errors  int64     `json:"errors"` // 5xx
	P95Ms   float64   `json:"p95_ms"`
	AvgMs   float64   `json:"avg_ms"`
}

// WindowSnapshot 是某个时间窗内的请求统计。
//
// HasSamples=false 表示**窗口内没有任何样本**。调用方必须把它显示成
// 「无采样数据」而不是 0 —— 0 会被读成「窗口内没有错误 / 延迟为 0」。
type WindowSnapshot struct {
	WindowSeconds int
	From          time.Time
	To            time.Time
	HasSamples    bool
	Samples       int64
	Success       int64 // 2xx/3xx
	ClientErrors  int64 // 4xx
	ServerErrors  int64 // 5xx
	ErrorRatePct  float64
	P50Ms         float64
	P95Ms         float64
	P99Ms         float64
	MaxMs         float64
	Buckets       []WindowBucket
}

// RequestWindow 是环形缓冲的请求采样器（并发安全）。
type RequestWindow struct {
	mu      sync.Mutex
	samples []RequestSample
	pos     int
	count   int
}

// NewRequestWindow 创建容量为 max 的采样器；max<=0 时回落到 RequestSampleWindowMax。
func NewRequestWindow(max int) *RequestWindow {
	if max <= 0 {
		max = RequestSampleWindowMax
	}
	return &RequestWindow{samples: make([]RequestSample, max)}
}

// RequestWindowStats 是网关进程内共享的请求采样器，由 MonitoringMiddleware 写入。
var RequestWindowStats = NewRequestWindow(RequestSampleWindowMax)

// Record 记录一条样本，并顺带丢弃超过保留期的旧样本。
func (w *RequestWindow) Record(d time.Duration, status int) {
	w.RecordClass("", d, status)
}

// RecordClass 记录一条带路由分类的样本。分类只用于分钟聚合的分组，
// 不影响 Snapshot 的任何口径。
func (w *RequestWindow) RecordClass(class string, d time.Duration, status int) {
	now := time.Now()
	w.mu.Lock()
	defer w.mu.Unlock()

	w.samples[w.pos] = RequestSample{At: now, Duration: d, Status: status, Class: class}
	w.pos = (w.pos + 1) % len(w.samples)
	if w.count < len(w.samples) {
		w.count++
	}

	// 从最旧一端丢弃过期样本。`count` 只减不加：数据本身留着等覆盖，
	// 但不再参与统计 —— 省掉一次 O(n) 的搬迁。
	for w.count > 0 {
		oldest := w.samples[w.oldestIndex()]
		if now.Sub(oldest.At) <= RequestSampleRetention {
			break
		}
		w.count--
	}
}

// oldestIndex 返回当前窗口最旧样本的下标（调用方必须持锁）。
func (w *RequestWindow) oldestIndex() int {
	start := w.pos - w.count
	for start < 0 {
		start += len(w.samples)
	}
	return start % len(w.samples)
}

// trackable 判断状态码是否计入请求统计。
//
// 101（WebSocket 升级）与 304（协商缓存）不属于「一次带耗时的业务请求」：
// 前者是长连接的开端（记录的是握手耗时，不是请求耗时），后者由客户端缓存触发。
// 把它们算进去会让 P99 看起来突然很好或很不稳，却解释不了。
func trackable(status int) bool {
	return status != 101 && status != 304
}

// Snapshot 汇总窗口内的请求统计。
//
// window<=0 或超过保留期一律按保留期处理：超过保留期的样本已经不存在，
// 贴着窗口边界返回一个「看起来更大」的窗口只会让数字失去意义。
func (w *RequestWindow) Snapshot(window time.Duration) WindowSnapshot {
	if window <= 0 || window > RequestSampleRetention {
		window = RequestSampleRetention
	}
	now := time.Now()
	cutoff := now.Add(-window)

	w.mu.Lock()
	// 只把时间窗内的样本挑出来（数据在锁内复制，排序与统计在锁外做）。
	durations := make([]time.Duration, 0, w.count)
	type marked struct {
		at     time.Time
		status int
		dur    time.Duration
	}
	kept := make([]marked, 0, w.count)
	for i := 0; i < w.count; i++ {
		s := w.samples[(w.oldestIndex()+i)%len(w.samples)]
		if s.At.Before(cutoff) {
			continue
		}
		kept = append(kept, marked{at: s.At, status: s.Status, dur: s.Duration})
		if trackable(s.Status) {
			durations = append(durations, s.Duration)
		}
	}
	w.mu.Unlock()

	snap := WindowSnapshot{
		WindowSeconds: int(window.Seconds()),
		From:          cutoff,
		To:            now,
		Samples:       int64(len(kept)),
		HasSamples:    len(kept) > 0,
	}
	if len(kept) == 0 {
		return snap
	}
	for _, s := range kept {
		switch {
		case s.status >= 500:
			snap.ServerErrors++
		case s.status >= 400:
			snap.ClientErrors++
		default:
			snap.Success++
		}
	}
	snap.ErrorRatePct = round1(float64(snap.ServerErrors) / float64(len(kept)) * 100)

	if len(durations) == 0 {
		// 只有 101/304 这类不可统计的响应：样本不算「无」，但没有延迟分位数。
		return snap
	}
	sort.Slice(durations, func(i, j int) bool { return durations[i] < durations[j] })
	snap.P50Ms = durationMs(durations[pick(len(durations), 50)])
	snap.P95Ms = durationMs(durations[pick(len(durations), 95)])
	snap.P99Ms = durationMs(durations[pick(len(durations), 99)])
	snap.MaxMs = durationMs(durations[len(durations)-1])

	// 趋势：固定 24 个桶，桶宽随窗口变化。空桶保留 0 值，
	// 前端画出来的线才会如实体现「这一段时间没有流量」而不是被压缩掉。
	const bucketCount = 24
	width := window / bucketCount
	if width <= 0 {
		width = time.Second
	}
	snap.Buckets = make([]WindowBucket, bucketCount)
	for i := range snap.Buckets {
		snap.Buckets[i].At = cutoff.Add(time.Duration(i) * width)
	}
	perBucket := make([][]time.Duration, bucketCount)
	for _, s := range kept {
		idx := int(s.at.Sub(cutoff) / width)
		if idx < 0 {
			idx = 0
		}
		if idx >= bucketCount {
			idx = bucketCount - 1
		}
		snap.Buckets[idx].Count++
		if s.status >= 500 {
			snap.Buckets[idx].Errors++
		}
		if trackable(s.status) {
			perBucket[idx] = append(perBucket[idx], s.dur)
		}
	}
	for i, ds := range perBucket {
		if len(ds) == 0 {
			continue
		}
		sort.Slice(ds, func(a, b int) bool { return ds[a] < ds[b] })
		snap.Buckets[i].P95Ms = durationMs(ds[pick(len(ds), 95)])
		var sum time.Duration
		for _, d := range ds {
			sum += d
		}
		snap.Buckets[i].AvgMs = durationMs(sum / time.Duration(len(ds)))
	}
	return snap
}

// IntervalStats 是某个**左闭右开**区间内的请求统计（分钟级落库与实时尾巴用）。
//
// 与 WindowSnapshot 的区别：这里不做 24 桶趋势，也不区分 4xx/2xx —— 落库表
// 只有 count / errors(5xx) / p95，多了也存不下来。P95Ms 在样本只有
// 101/304 时保持 0（与 WindowSnapshot 同口径：没有可统计的耗时）。
type IntervalStats struct {
	Count      int64   `json:"count"`
	Errors     int64   `json:"errors"`
	P95Ms      float64 `json:"p95_ms"`
	HasSamples bool    `json:"has_samples"`
}

// SnapshotInterval 汇总 [from, to) 内的样本（不按分类拆分）。
func (w *RequestWindow) SnapshotInterval(from, to time.Time) IntervalStats {
	var out IntervalStats
	for _, st := range w.SnapshotIntervalByClass(from, to) {
		out.Count += st.Count
		out.Errors += st.Errors
		out.HasSamples = out.HasSamples || st.HasSamples
		// 多分类合并时 p95 取最大值：分位数不可加，取最大是保守的
		// 「尾延迟不会比任何一类更差」，并把口径写进接口注释与面板说明。
		if st.P95Ms > out.P95Ms {
			out.P95Ms = st.P95Ms
		}
	}
	return out
}

// SnapshotIntervalByClass 汇总 [from, to) 内的样本并按路由分类拆分。
//
// 分类为空的样本落在 ""（落库方归一到 "default"）。返回的 map 只在
// 有样本的分类上出现键 —— 调用方据此区分「这一分钟没流量」与「这一分钟延迟为 0」。
func (w *RequestWindow) SnapshotIntervalByClass(from, to time.Time) map[string]IntervalStats {
	type agg struct {
		count  int64
		errors int64
		durs   []time.Duration
	}
	byClass := make(map[string]*agg, 8)

	w.mu.Lock()
	for i := 0; i < w.count; i++ {
		s := w.samples[(w.oldestIndex()+i)%len(w.samples)]
		if s.At.Before(from) || !s.At.Before(to) {
			continue
		}
		a := byClass[s.Class]
		if a == nil {
			a = &agg{}
			byClass[s.Class] = a
		}
		a.count++
		if s.Status >= 500 {
			a.errors++
		}
		if trackable(s.Status) {
			a.durs = append(a.durs, s.Duration)
		}
	}
	w.mu.Unlock()

	out := make(map[string]IntervalStats, len(byClass))
	for class, a := range byClass {
		st := IntervalStats{Count: a.count, Errors: a.errors, HasSamples: a.count > 0}
		if len(a.durs) > 0 {
			sort.Slice(a.durs, func(i, j int) bool { return a.durs[i] < a.durs[j] })
			st.P95Ms = durationMs(a.durs[pick(len(a.durs), 95)])
		}
		out[class] = st
	}
	return out
}

// pick 返回分位数下标，四舍五入到最近的序号（小样本下也稳定）。
func pick(n, percentile int) int {
	if n <= 1 {
		return 0
	}
	idx := (percentile*n + 99) / 100
	idx-- // 转成 0 基
	if idx < 0 {
		return 0
	}
	if idx >= n {
		return n - 1
	}
	return idx
}

func durationMs(d time.Duration) float64 {
	return math.Round(float64(d.Microseconds())/10) / 100
}

func round1(v float64) float64 {
	return math.Round(v*10) / 10
}
