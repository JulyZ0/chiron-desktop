package monitor

import (
	"sync"
	"time"
)

// ── 队列消费速率（每分钟 XACK 数） ──
//
// 为什么单独一层：队列面板此前只有 XLEN / XPENDING / lag 三个**瞬时**读数，
// 它们回答的是「现在积压多少」，回答不了「有没有在消费」。一个 lag 稳定在 200
// 的队列可能正在稳定消费（进来的和出去的一样多），也可能消费者全挂了 ——
// 两者在快照上一样。速率必须来自**时间窗采样**。
//
// 采样点选 XACK：它是「一条消息被成功处理完」的唯一明确信号。XLEN 下降会被
// MAXLEN 修剪污染，entries-read 只代表投递。代价是**只有本进程自己的消费者**
// 的 ack 能被看到（引擎侧消费由 python-engine 负责），所以面板上必须标注
// 来源与样本数，0 不能读成「没在消费」，只能读成「本进程没有 ack 采样」。

// XackWindowRetention 是 XACK 采样的保留期（够画 15 分钟趋势）。
const XackWindowRetention = 15 * time.Minute

// XackWindowMax 是采样的环缓冲容量。XACK 是批量调用（一次几十上百条）时
// 每条消息记一个样本，20k 覆盖常见吞吐。
const XackWindowMax = 20000

type xackSample struct {
	at     time.Time
	stream string
}

// XackWindow 是 XACK 事件的环形采样器（并发安全）。
type XackWindow struct {
	mu      sync.Mutex
	samples []xackSample
	pos     int
	count   int
}

// NewXackWindow 创建容量为 max 的采样器；max<=0 时回落到 XackWindowMax。
func NewXackWindow(max int) *XackWindow {
	if max <= 0 {
		max = XackWindowMax
	}
	return &XackWindow{samples: make([]xackSample, max)}
}

// XackStats 是网关进程内共享的 XACK 采样器，由 XAck 调用点写入。
var XackStats = NewXackWindow(XackWindowMax)

// RecordXack 记录一条 XACK（stream 用实际 Redis key）。
func RecordXack(stream string) {
	XackStats.Record(stream)
}

// Record 记录一条 XACK 事件并丢弃保留期外的旧样本。
func (x *XackWindow) Record(stream string) {
	now := time.Now()
	x.mu.Lock()
	defer x.mu.Unlock()

	x.samples[x.pos] = xackSample{at: now, stream: stream}
	x.pos = (x.pos + 1) % len(x.samples)
	if x.count < len(x.samples) {
		x.count++
	}
	for x.count > 0 {
		oldest := x.samples[x.oldestIdx()]
		if now.Sub(oldest.at) <= XackWindowRetention {
			break
		}
		x.count--
	}
}

func (x *XackWindow) oldestIdx() int {
	start := x.pos - x.count
	for start < 0 {
		start += len(x.samples)
	}
	return start % len(x.samples)
}

// XackRate 是某条流的消费速率读数。
//
// PerMinuteSeconds 说明「每分钟」是按哪个窗口折算的：窗口不足一分钟时
// （进程刚启动）用实际跨度折算，而不是硬除 60 —— 否则第一个 10 秒里
// ack 了 30 条会显示成 0.5/min。
type XackRate struct {
	PerMinutePerSecond float64 `json:"per_minute"`
	Samples            int64   `json:"samples"`
	WindowSeconds      int     `json:"window_seconds"`
	HasSamples         bool    `json:"has_samples"`
}

// Rate 统计 stream 在最近 window 内的 XACK 速率（折算成每分钟条数）。
//
// stream 为空表示不区分流（面板上的"全部"）。窗口 <=0 或超过保留期按保留期处理。
func (x *XackWindow) Rate(stream string, window time.Duration) XackRate {
	if window <= 0 || window > XackWindowRetention {
		window = XackWindowRetention
	}
	now := time.Now()
	cutoff := now.Add(-window)

	x.mu.Lock()
	var n int64
	var earliest time.Time
	for i := 0; i < x.count; i++ {
		s := x.samples[(x.oldestIdx()+i)%len(x.samples)]
		if s.at.Before(cutoff) {
			continue
		}
		if stream != "" && s.stream != stream {
			continue
		}
		n++
		if earliest.IsZero() || s.at.Before(earliest) {
			earliest = s.at
		}
	}
	x.mu.Unlock()

	out := XackRate{Samples: n, WindowSeconds: int(window.Seconds()), HasSamples: n > 0}
	if n == 0 {
		return out
	}
	// 实际跨度：从最早样本到现在。至少 1 秒，避免除零把速率炸成 +Inf。
	span := now.Sub(earliest)
	if span < time.Second {
		span = time.Second
	}
	out.PerMinutePerSecond = round2(float64(n) / span.Minutes())
	return out
}

// PerMinute 返回最近 minutes 个**自然分钟桶**的 XACK 数（最后一个桶是当前
// 未走完的这一分钟，不代表完整速率）。空桶保留 0。
func (x *XackWindow) PerMinute(stream string, minutes int) []int64 {
	if minutes <= 0 {
		minutes = 15
	}
	now := time.Now()
	// 当前整分钟是桶的右边界；桶 i 覆盖 [minuteStart-(minutes-i)*1m, ...)。
	minuteStart := now.Truncate(time.Minute)
	from := minuteStart.Add(-time.Duration(minutes-1) * time.Minute)
	out := make([]int64, minutes)

	x.mu.Lock()
	for i := 0; i < x.count; i++ {
		s := x.samples[(x.oldestIdx()+i)%len(x.samples)]
		if s.at.Before(from) || s.at.After(now) {
			continue
		}
		if stream != "" && s.stream != stream {
			continue
		}
		idx := int(s.at.Sub(from) / time.Minute)
		if idx < 0 || idx >= minutes {
			continue
		}
		out[idx]++
	}
	x.mu.Unlock()
	return out
}

func round2(v float64) float64 {
	return float64(int64(v*100+0.5)) / 100
}
