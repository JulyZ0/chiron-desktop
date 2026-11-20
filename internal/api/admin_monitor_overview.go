package api

// admin_monitor_overview.go —— 后台「运行时监控」页的统一数据源。
//
// 设计约束（写这些类型时一直在守的几条）：
//
//  1. **一次请求给全部三个面板**：性能 / 队列 / 缓存各自可能要打 PG、Redis、引擎，
//     但页面每 10s 刷新一次，拆成三个端点就是三倍连接与三份时间戳。这里一次采完，
//     只在顶层带一个 generated_at。
//  2. **可解释**：每个数字都带口径 —— window_seconds 说明时间窗，samples 说明样本量，
//     source 说明来自哪张表 / 哪条命令。算不出来的字段一律 JSON null，不写 0：
//     0 会被读成"确实是零"，而真相是"没采到"。
//  3. **单项降级**：PG 挂了不影响 Redis 面板，Redis 挂了不影响 Go runtime 面板。
//     每个区块自带 status（ok / partial / unavailable）与 errors（具体组件名 + 原因）。

import (
	"context"
	"fmt"
	"net/http"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
)

// ── 响应结构 ──

const (
	monitorStatusOK          = "ok"
	monitorStatusPartial     = "partial"
	monitorStatusUnavailable = "unavailable"
)

// MonitorComponentError 说明"哪一块没采到、为什么"。
// component 用**运维认得的组件名**（postgres / redis / python-engine），而不是内部函数名。
type MonitorComponentError struct {
	Component string `json:"component"`
	Message   string `json:"message"`
}

// MonitorSectionMeta 是三个区块共用的"体检结果"。
type MonitorSectionMeta struct {
	// GeneratedAt 是该区块的采集时刻。块级时间戳是有意的：一次 overview 里
	// 三块是顺序采集的，慢的那块可能比快的晚几百毫秒，排障时对得上。
	GeneratedAt time.Time               `json:"generated_at"`
	Status      string                  `json:"status"`
	Sources     []string                `json:"sources"`
	Errors      []MonitorComponentError `json:"errors"`
}

func newSectionMeta(sources ...string) MonitorSectionMeta {
	return MonitorSectionMeta{
		GeneratedAt: time.Now().UTC(),
		Status:      monitorStatusOK,
		Sources:     sources,
		Errors:      []MonitorComponentError{},
	}
}

func (m *MonitorSectionMeta) fail(component, msg string) {
	m.Errors = append(m.Errors, MonitorComponentError{Component: component, Message: msg})
	// 不把 unavailable 降级成 partial：一整块都读不到时，多记几条失败原因
	// 不改变"整块不可用"这个结论。
	if m.Status == monitorStatusOK {
		m.Status = monitorStatusPartial
	}
}

func (m *MonitorSectionMeta) unavailable(component, msg string) {
	m.Errors = append(m.Errors, MonitorComponentError{Component: component, Message: msg})
	m.Status = monitorStatusUnavailable
}

func (m *MonitorSectionMeta) note(component, msg string) {
	m.Errors = append(m.Errors, MonitorComponentError{Component: component, Message: msg})
	if m.Status == monitorStatusOK {
		m.Status = monitorStatusPartial
	}
}

// MonitorRequestStats 是「近 N 分钟」的请求统计。
type MonitorRequestStats struct {
	// HasSamples=false 时前端必须显示"无采样数据"，而不是 0。
	HasSamples       bool                   `json:"has_samples"`
	Samples          int64                  `json:"samples"`
	Success          int64                  `json:"success"`
	ClientErrors     int64                  `json:"client_errors"` // 4xx
	ServerErrors     int64                  `json:"server_errors"` // 5xx
	ErrorRatePercent float64                `json:"error_rate_percent"`
	P50Ms            *float64               `json:"p50_ms"`
	P95Ms            *float64               `json:"p95_ms"`
	P99Ms            *float64               `json:"p99_ms"`
	MaxMs            *float64               `json:"max_ms"`
	Note             string                 `json:"note,omitempty"`
	Buckets          []monitor.WindowBucket `json:"buckets"`
	// RollupSamples / TailSamples 说明 samples 的构成（P2）：
	// rollup 来自 PG 分钟聚合（多副本、跨重启），tail 是本进程当前分钟的实时采样。
	RollupSamples int64 `json:"rollup_samples"`
	TailSamples   int64 `json:"tail_samples"`
	// Sources 逐个列出本次合成的数据来源，避免"多副本合计"被误读成"本进程数字"。
	Sources []string `json:"sources"`
}

// MonitorDBPool 是 pgxpool.Stat() 的投影。
type MonitorDBPool struct {
	TotalConns        int32   `json:"total_conns"`
	IdleConns         int32   `json:"idle_conns"`
	AcquiredConns     int32   `json:"acquired_conns"`
	MaxConns          int32   `json:"max_conns"`
	ConstructingConns int32   `json:"constructing_conns"`
	EmptyAcquireCount int64   `json:"empty_acquire_count"`
	AcquireDurationMs float64 `json:"acquire_duration_ms"`
	UtilizationPct    float64 `json:"utilization_percent"`
	Source            string  `json:"source"`
}

// MonitorGoRuntime 是本进程（Go 网关）的运行时读数。
type MonitorGoRuntime struct {
	Goroutines    int     `json:"goroutines"`
	HeapAllocMB   float64 `json:"heap_alloc_mb"`
	HeapSysMB     float64 `json:"heap_sys_mb"`
	HeapInuseMB   float64 `json:"heap_inuse_mb"`
	GCRuns        uint32  `json:"gc_runs"`
	GOMAXPROCS    int     `json:"gomaxprocs"`
	CPUPercent    float64 `json:"cpu_percent"`
	UptimeSeconds int64   `json:"uptime_seconds"`
	Source        string  `json:"source"`
	CPUNote       string  `json:"cpu_note"`
}

// MonitorEngineProcess 是 Python 引擎进程级的资源读数（复用 /info）。
type MonitorEngineProcess struct {
	Configured    bool    `json:"configured"`
	Reachable     bool    `json:"reachable"`
	Instances     int     `json:"instances"`
	Version       string  `json:"version"`
	UptimeSeconds int64   `json:"uptime_seconds"`
	MemoryMB      float64 `json:"memory_mb"`
	CPUPercent    float64 `json:"cpu_percent"`
	ActiveTasks   int     `json:"active_tasks"`
	Source        string  `json:"source"`
	CPUNote       string  `json:"cpu_note"`
}

// MonitorPerformance 是「性能」面板。
type MonitorPerformance struct {
	MonitorSectionMeta
	WindowSeconds int                   `json:"window_seconds"`
	Window        string                `json:"window"`
	Requests      MonitorRequestStats   `json:"requests"`
	DBPool        *MonitorDBPool        `json:"db_pool"`
	GoRuntime     MonitorGoRuntime      `json:"go_runtime"`
	Engine        *MonitorEngineProcess `json:"engine"`
	RedisLatency  *float64              `json:"redis_latency_ms"`
	DBLatency     *float64              `json:"db_latency_ms"`
}

// MonitorStreamConsumption 是按时间窗采样的**消费速率**（P2）。
//
// 为什么需要它：XLEN / XPENDING / lag 都是瞬时快照，回答不了「有没有在消费」。
// 这里统计的是 XACK 事件（一条消息处理成功的唯一明确信号）。
//
// 边界：只有**本进程**的 XACK 可见 —— 引擎队列由 python-engine 消费，
// 网关看不到。所以 HasSamples=false 只代表「本副本没有 ack 采样」，
// 不代表队列没在消费；必须如实标注，否则会把正常流转读成消费者挂了。
type MonitorStreamConsumption struct {
	HasSamples     bool    `json:"has_samples"`
	XackPerMinute  float64 `json:"xack_per_minute"`
	Samples        int64   `json:"samples"`
	WindowSeconds  int     `json:"window_seconds"`
	PerMinute      []int64 `json:"per_minute"`
	Source         string  `json:"source"`
}

// MonitorStreamStats 是一条 Redis Stream 队列的真实读数。
type MonitorStreamStats struct {
	Name              string   `json:"name"`
	Key               string   `json:"key"`
	Depth             *int64   `json:"depth"`
	Pending           *int64   `json:"pending"`
	Lag               *int64   `json:"lag"`
	Groups            *int     `json:"groups"`
	Consumers         *int     `json:"consumers"`
	DLQDepth          *int64   `json:"dlq_depth"`
	OldestWaitSeconds *float64 `json:"oldest_wait_seconds"`
	OldestTaskAt      string   `json:"oldest_task_at,omitempty"`
	Source            string   `json:"source"`
	// Consumption 是窗口内的 XACK 速率（见 MonitorStreamConsumption 的边界说明）。
	Consumption *MonitorStreamConsumption `json:"consumption"`
}

// MonitorRunCounts 是 run 状态计数（DB）与 run 归属租约（Redis）两个视角。
type MonitorRunCounts struct {
	Active          *int64 `json:"active"`
	AwaitingInput   *int64 `json:"awaiting_input"`
	FailedLastHour  *int64 `json:"failed_last_hour"`
	TimeoutLastHour *int64 `json:"timeout_last_hour"`
	RunLeases       *int64 `json:"run_leases"`
	Source          string `json:"source"`
}

// MonitorQueue 是「队列」面板。
type MonitorQueue struct {
	MonitorSectionMeta
	Streams  []MonitorStreamStats `json:"streams"`
	RunState *MonitorRunCounts    `json:"run_state"`
}

// MonitorRedisCache 是 Redis 侧缓存/键空间读数（INFO 命令）。
type MonitorRedisCache struct {
	UsedMemoryMB    *float64 `json:"used_memory_mb"`
	MaxMemoryMB     *float64 `json:"max_memory_mb"`
	HitRatePercent  *float64 `json:"hit_rate_percent"`
	KeyspaceHits    *int64   `json:"keyspace_hits"`
	KeyspaceMisses  *int64   `json:"keyspace_misses"`
	Keys            *int64   `json:"keys"`
	KeysWithTTL     *int64   `json:"keys_with_ttl"`
	AvgTTLMs        *int64   `json:"avg_ttl_ms"`
	EvictedKeys     *int64   `json:"evicted_keys"`
	ExpiredKeys     *int64   `json:"expired_keys"`
	MaxmemoryPolicy string   `json:"maxmemory_policy"`
	Source          string   `json:"source"`
}

// MonitorLLMCache 是引擎侧多级缓存（L1/L2/L3）的命中读数。
type MonitorLLMCache struct {
	L1Hits         int64   `json:"l1_hits"`
	L2Hits         int64   `json:"l2_hits"`
	L3Hits         int64   `json:"l3_hits"`
	Misses         int64   `json:"misses"`
	HitRatePercent float64 `json:"hit_rate_percent"`
	Source         string  `json:"source"`
}

// MonitorCache 是「缓存」面板。
type MonitorCache struct {
	MonitorSectionMeta
	Redis *MonitorRedisCache `json:"redis"`
	LLM   *MonitorLLMCache   `json:"llm"`
}

// MonitorOverview 是 GET /v1/admin/monitor/overview 的响应。
type MonitorOverview struct {
	GeneratedAt   time.Time          `json:"generated_at"`
	WindowSeconds int                `json:"window_seconds"`
	Window        string             `json:"window"`
	Partial       bool               `json:"partial"`
	Performance   MonitorPerformance `json:"performance"`
	Queue         MonitorQueue       `json:"queue"`
	Cache         MonitorCache       `json:"cache"`
}

// ── 时间窗 ──

// parseMonitorWindow 解析 ?window=5m|15m|60m。默认 15 分钟。
// 只接受三个固定档位：前端是下拉切换，自由取值会让"这个数到底是什么窗口"无法解释。
func parseMonitorWindow(raw string) (time.Duration, string, bool) {
	switch strings.TrimSpace(strings.ToLower(raw)) {
	case "", "15m":
		return 15 * time.Minute, "15m", true
	case "5m":
		return 5 * time.Minute, "5m", true
	case "60m", "1h":
		return 60 * time.Minute, "60m", true
	}
	return 0, "", false
}

// GetMonitorOverview GET /v1/admin/monitor/overview?window=15m
//
// 平台级读端点（RequirePlatformOwner + admin:read）：回的是全平台运行事实，
// 租户 admin 的 admin:read 不应看到别家租户的队列与 run 计数。
func (h *AdminHandler) GetMonitorOverview(w http.ResponseWriter, r *http.Request) {
	window, label, ok := parseMonitorWindow(r.URL.Query().Get("window"))
	if !ok {
		BadRequest(w, "invalid window: must be one of 5m, 15m, 60m")
		return
	}
	// 整体 8s：页面 10s 刷新一次，一次采集卡住不该把下一次也堵住。
	ctx, cancel := context.WithTimeout(r.Context(), 8*time.Second)
	defer cancel()

	out := MonitorOverview{
		GeneratedAt:   time.Now().UTC(),
		WindowSeconds: int(window.Seconds()),
		Window:        label,
	}
	// 引擎 /info 只拉一次：性能面板要 RSS/CPU、缓存面板要 gateway.cache，
	// 分头再拉会让两个面板的数字来自不同瞬间（而且引擎不可达时白打两次）。
	engineInfo := h.fetchPyEngineInfo(ctx)
	out.Performance = h.collectMonitorPerformance(ctx, window, engineInfo)
	out.Queue = h.collectMonitorQueue(ctx)
	out.Cache = h.collectMonitorCache(ctx, engineInfo)
	out.Partial = out.Performance.Status != monitorStatusOK ||
		out.Queue.Status != monitorStatusOK ||
		out.Cache.Status != monitorStatusOK
	OK(w, out)
}

// ── 性能 ──

func (h *AdminHandler) collectMonitorPerformance(ctx context.Context, window time.Duration, engineInfo *pyEngineInfo) MonitorPerformance {
	meta := newSectionMeta(
		"monitor.RequestWindowStats（MonitoringMiddleware 逐请求采样）",
		"monitor.Snapshot() + runtime.ReadMemStats",
		"pgxpool.Stat()",
		"python-engine GET /info",
	)
	p := MonitorPerformance{
		MonitorSectionMeta: meta,
		WindowSeconds:      int(window.Seconds()),
		Window:             windowLabel(window),
	}

	// ① 请求窗口：P2 起改为「窗口内 rollup 汇总（多副本合计）+ 本进程实时尾巴」。
	//
	// 拆分点固定在**整分钟边界**：rollup 只写"已走完的分钟"，所以
	// [now-window, 上一分钟末) 查 PG，[上一分钟末, now) 用本进程采样。
	// 两边不重叠 —— 否则同一批请求被算两次，错误率会看起来比实际好。
	snap := monitor.RequestWindowStats.Snapshot(window)
	now := time.Now()
	rollupTo := now.Truncate(time.Minute)
	rollupFrom := rollupTo.Add(-window)
	tail := monitor.RequestWindowStats.SnapshotInterval(rollupTo, now)
	rollupAgg, rollupErr := queryRollupWindow(ctx, rollupFrom, rollupTo)
	usedRollup := rollupErr == nil && rollupAgg.Rows > 0

	p.Requests = MonitorRequestStats{
		RollupSamples: rollupAgg.Count,
		TailSamples:   tail.Count,
	}
	switch {
	case usedRollup:
		merged := mergeRollupWithTail(rollupAgg, tail)
		p.Requests.HasSamples = merged.HasSamples
		p.Requests.Samples = merged.Count
		// rollup 只存 errors(5xx)，没有 4xx 列：成功数按"总数 - 5xx"给，
		// 前端口径文案里明确写出"含 4xx"。
		p.Requests.ServerErrors = merged.Errors
		p.Requests.Success = merged.Count - merged.Errors
		p.Requests.ErrorRatePercent = 0
		if merged.Count > 0 {
			p.Requests.ErrorRatePercent = round2(float64(merged.Errors) / float64(merged.Count) * 100)
		}
		if merged.P95Ms > 0 {
			p.Requests.P95Ms = floatPtr(merged.P95Ms)
		}
		// P50/P99/Max 无法从 rollup 复原（表里只有 p95），只能用本进程采样补；
		// 没有本进程样本时保持 null，而不是把 p95 抄成 p50。
		if snap.HasSamples {
			p.Requests.P50Ms = floatPtr(snap.P50Ms)
			p.Requests.P99Ms = floatPtr(snap.P99Ms)
			p.Requests.MaxMs = floatPtr(snap.MaxMs)
		}
		buckets, err := queryRollupBuckets(ctx, rollupFrom, rollupTo, bucketWidthSeconds(window, 24))
		if err != nil {
			p.note("postgres", "rollup 趋势读取失败: "+err.Error())
		} else {
			p.Requests.Buckets = appendTailBucket(buckets, rollupTo, tail)
		}
		p.Requests.Sources = []string{
			"postgres metrics_rollup_minute（已走完的分钟，跨副本合计、跨重启）",
			"monitor.RequestWindowStats（本进程当前分钟的实时尾巴）",
		}
		p.Sources = append(p.Sources,
			"postgres metrics_rollup_minute（窗口内分钟聚合）")
		p.Requests.Note = fmt.Sprintf(
			"合计 = rollup %d 条 + 实时尾巴 %d 条；错误率 = 5xx / 合计（rollup 只存 5xx，成功数含 4xx）。"+
				"p95 为 rollup 与尾巴的较大值（分位数不可加，属保守近似）；p50/p99/max 仅来自本进程样本。",
			rollupAgg.Count, tail.Count)
	case rollupErr == nil:
		// PG 连得上但窗口内没有 rollup 行（刚部署 / 落库循环还没跑满一分钟）。
		// 退回纯实时窗口，并如实说明覆盖范围，而不是报 0。
		fillLocalRequestStats(&p, snap, window)
		p.Requests.Sources = []string{"monitor.RequestWindowStats（本进程实时窗口）"}
		p.Requests.Note = fmt.Sprintf(
			"近 %s 无 rollup 行（分钟落库可能刚启用），以下仅本进程实时窗口：%d 条样本。",
			windowLabel(window), snap.Samples)
	default:
		fillLocalRequestStats(&p, snap, window)
		p.Requests.Sources = []string{"monitor.RequestWindowStats（本进程实时窗口）"}
		p.note("postgres", "rollup 查询失败，退回本进程实时窗口: "+rollupErr.Error())
	}
	if snap.HasSamples && snap.Samples >= int64(monitor.RequestSampleWindowMax) {
		p.note("request-sampler", "样本量已达环形缓冲上限，分位数只覆盖最近 20000 条请求")
	}

	// ② Go 运行时（进程级）。
	runtimeSnap := monitor.Snapshot()
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	p.GoRuntime = MonitorGoRuntime{
		Goroutines:    int(snapInt64(runtimeSnap["go_goroutines"])),
		HeapAllocMB:   round2(float64(mem.Alloc) / (1024 * 1024)),
		HeapSysMB:     round2(float64(mem.Sys) / (1024 * 1024)),
		HeapInuseMB:   round2(float64(mem.HeapInuse) / (1024 * 1024)),
		GCRuns:        mem.NumGC,
		GOMAXPROCS:    runtime.GOMAXPROCS(0),
		CPUPercent:    processCPUPercent(),
		UptimeSeconds: int64(snapFloat64(runtimeSnap["uptime_seconds"])),
		Source:        "runtime.ReadMemStats + runtime/metrics",
		CPUNote:       "本进程 CPU 占用率 = 两次采样间 CPU 秒差 / 墙钟 / GOMAXPROCS；首次与长时间无请求时可读作 0。",
	}

	// ③ DB 连接池。
	if pool, err := db.GlobalDBManager.GetPool(); err != nil {
		p.fail("postgres", "连接池不可用: "+err.Error())
	} else {
		st := pool.Stat()
		util := 0.0
		if st.MaxConns() > 0 {
			util = round2(float64(st.AcquiredConns()) / float64(st.MaxConns()) * 100)
		}
		p.DBPool = &MonitorDBPool{
			TotalConns:        st.TotalConns(),
			IdleConns:         st.IdleConns(),
			AcquiredConns:     st.AcquiredConns(),
			MaxConns:          st.MaxConns(),
			ConstructingConns: st.ConstructingConns(),
			EmptyAcquireCount: st.EmptyAcquireCount(),
			AcquireDurationMs: round2(float64(st.AcquireDuration().Microseconds()) / 1000),
			UtilizationPct:    util,
			Source:            "pgxpool.Stat()",
		}
	}

	// ④ 引擎进程（RSS / CPU / 在途请求）。
	if h.pythonClient == nil {
		p.Engine = &MonitorEngineProcess{
			Configured: false, Reachable: false,
			Source: "python-engine 未配置（PYTHON_ENGINE_ADDRESS 为空）",
		}
	} else {
		instances := len(h.pythonClient.StaticAddresses())
		info := engineInfo
		if info == nil {
			p.Engine = &MonitorEngineProcess{
				Configured: true, Reachable: false, Instances: instances,
				Source: "python-engine GET /info 不可达",
			}
			p.fail("python-engine", "GET /info 不可达，引擎 RSS/CPU 未知")
		} else {
			p.Engine = &MonitorEngineProcess{
				Configured:    true,
				Reachable:     true,
				Instances:     instances,
				Version:       info.Version,
				UptimeSeconds: info.UptimeSeconds,
				MemoryMB:      round2(info.MemoryMB),
				CPUPercent:    round2(info.CPUPercent),
				ActiveTasks:   info.ActiveTasks,
				Source:        "python-engine GET /info（psutil 进程 RSS/CPU）",
				CPUNote:       "CPU 为 psutil 自上次调用以来的均值；刚启动或空闲时偏低。",
			}
		}
	}

	// ⑤ 依赖往返延迟（真实 PING）。失败只影响各自字段。
	if db.Redis != nil {
		start := time.Now()
		if err := db.Redis.Ping(ctx).Err(); err != nil {
			p.fail("redis", "PING 失败: "+err.Error())
		} else {
			p.RedisLatency = floatPtr(round2(float64(time.Since(start).Microseconds()) / 1000))
		}
	} else {
		p.fail("redis", "Redis 未配置")
	}
	start := time.Now()
	if err := db.GlobalDBManager.Ping(ctx); err != nil {
		p.fail("postgres", "PING 失败: "+err.Error())
	} else {
		p.DBLatency = floatPtr(round2(float64(time.Since(start).Microseconds()) / 1000))
	}

	return p
}

// fillLocalRequestStats 用本进程实时窗口填充请求统计（rollup 不可用时的退回路径）。
//
// 单独一个函数是为了让"退回路径与 rollup 路径的字段覆盖范围"在代码里可比：
// 退回路径能给出 4xx/成功数/全部分位数，rollup 路径只能给 5xx 与近似 p95。
func fillLocalRequestStats(p *MonitorPerformance, snap monitor.WindowSnapshot, window time.Duration) {
	p.Requests.HasSamples = snap.HasSamples
	p.Requests.Samples = snap.Samples
	p.Requests.Success = snap.Success
	p.Requests.ClientErrors = snap.ClientErrors
	p.Requests.ServerErrors = snap.ServerErrors
	p.Requests.ErrorRatePercent = snap.ErrorRatePct
	p.Requests.TailSamples = snap.Samples
	p.Requests.RollupSamples = 0
	p.Requests.Buckets = snap.Buckets
	if len(snap.Buckets) == 0 {
		p.Requests.Buckets = []monitor.WindowBucket{}
	}
	if snap.HasSamples {
		p.Requests.P50Ms = floatPtr(snap.P50Ms)
		p.Requests.P95Ms = floatPtr(snap.P95Ms)
		p.Requests.P99Ms = floatPtr(snap.P99Ms)
		p.Requests.MaxMs = floatPtr(snap.MaxMs)
		p.Requests.Note = fmt.Sprintf("错误率 = 5xx / 全部样本；窗口内共 %d 条样本（上限 %d）。",
			snap.Samples, monitor.RequestSampleWindowMax)
		return
	}
	p.Requests.Note = fmt.Sprintf("近 %s 无请求采样数据（MonitoringMiddleware 未采到任何请求）。", windowLabel(window))
	p.note("request-sampler", "无采样数据")
}

// ── 队列 ──

// monitorStreams 是要观测的队列。前两条是 python-engine 的任务流
// （见 python-engine/app/queue/streams.py 的 ENGINE_QUEUE / KB_QUEUE），
// 第三条是网关自己的事件流（webhook:events，沿用原有队列面板的口径）。
type monitorStreamSpec struct {
	name string
	key  string
	dlq  string
}

func monitorStreamSpecs() []monitorStreamSpec {
	return []monitorStreamSpec{
		{name: "engine", key: db.RedisKey("engine:tasks"), dlq: db.RedisKey("engine:tasks:dlq")},
		{name: "kb", key: db.RedisKey("engine:tasks:kb"), dlq: db.RedisKey("engine:tasks:kb:dlq")},
		{name: "webhook", key: db.RedisKey(webhookStreamName)},
	}
}

func (h *AdminHandler) collectMonitorQueue(ctx context.Context) MonitorQueue {
	meta := newSectionMeta(
		"Redis XLEN/XINFO GROUPS/XRANGE（engine:tasks、engine:tasks:kb、webhook:events）",
		"agent_runs / subagent_runs 状态计数",
		"Redis engine:run:* 归属租约（run_registry）",
	)
	q := MonitorQueue{MonitorSectionMeta: meta, Streams: []MonitorStreamStats{}}

	// ① Stream 真实长度与消费者组。
	if db.Redis == nil {
		q.unavailable("redis", "Redis 未配置，队列深度未知")
	} else {
		for _, spec := range monitorStreamSpecs() {
			st := MonitorStreamStats{
				Name:   spec.name,
				Key:    spec.key,
				Source: "Redis XLEN/XINFO GROUPS/XRANGE",
			}
			if n, err := redisInt64Cmd(ctx, "XLEN", spec.key); err != nil {
				q.fail("redis", fmt.Sprintf("XLEN %s 失败: %v", spec.key, err))
			} else {
				st.Depth = &n
			}
			if spec.dlq != "" {
				if n, err := redisInt64Cmd(ctx, "XLEN", spec.dlq); err == nil {
					st.DLQDepth = &n
				}
			}
			var lastID string
			if g, err := readStreamGroupsStrict(ctx, spec.key); err == nil {
				groups, consumers := g.Groups, g.Consumers
				pending, lag := g.Pending, g.Lag
				st.Groups, st.Consumers = &groups, &consumers
				st.Pending, st.Lag = &pending, &lag
				lastID = g.LastDeliveredID
			}
			// 最老等待只算**尚未投递**的第一条（XRANGE 从 last-delivered-id 之后取）。
			// 直接从 "-" 取会把已被消费、尚未被 MAXLEN 修剪的老消息算成"排队中"，
			// 队列其实已经空了也会显示几小时的等待。
			if wait, at, err := oldestStreamWait(ctx, spec.key, lastID); err == nil {
				st.OldestWaitSeconds, st.OldestTaskAt = wait, at
			}
			// 消费速率：5 分钟窗口折算成每分钟 XACK 数 + 最近 15 个自然分钟的桶。
			// 与上面几个瞬时读数并列给出，才能区分"积压大但正在消化"和"积压大且停滞"。
			rate := monitor.XackStats.Rate(spec.key, 5*time.Minute)
			st.Consumption = &MonitorStreamConsumption{
				HasSamples:    rate.HasSamples,
				XackPerMinute: rate.PerMinutePerSecond,
				Samples:       rate.Samples,
				WindowSeconds: rate.WindowSeconds,
				PerMinute:     monitor.XackStats.PerMinute(spec.key, 15),
				Source: "本进程 XACK 采样（仅统计网关自己 ack 的流；引擎消费不在其中）",
			}
			q.Streams = append(q.Streams, st)
		}
	}

	// ② run 状态计数（DB）+ 归属租约（Redis）。两个来源各自可用，互不阻塞。
	counts := MonitorRunCounts{Source: ""}
	if active, awaiting, failed, err := queryAgentRunCounts(ctx); err != nil {
		q.fail("postgres", "读 agent_runs 状态计数失败: "+err.Error())
	} else {
		counts.Active, counts.AwaitingInput, counts.FailedLastHour = &active, &awaiting, &failed
		counts.Source = "agent_runs：active=running/checkpointed/resuming；failed=status='failed' 且 updated_at 近 1h"
	}
	if timeouts, err := querySubagentTimeouts(ctx); err != nil {
		q.fail("postgres", "读 subagent_runs 超时计数失败: "+err.Error())
	} else {
		counts.TimeoutLastHour = &timeouts
		if counts.Source != "" {
			counts.Source += "；timeout=subagent_runs.error_code='timeout' 且 finished_at 近 1h"
		}
	}
	if db.Redis != nil {
		if leases, err := countRunLeases(ctx); err != nil {
			q.fail("redis", "统计 engine:run:* 租约失败: "+err.Error())
		} else {
			counts.RunLeases = &leases
		}
	}
	if counts.Active != nil || counts.RunLeases != nil {
		q.RunState = &counts
	}

	if len(q.Streams) == 0 && q.RunState == nil && q.Status == monitorStatusOK {
		q.unavailable("redis", "队列与 run 计数均不可用")
	}
	// Redis 不可用但 DB 计数可用 ⇒ 不是"整块不可用"，应显示为部分可用。
	if q.Status == monitorStatusUnavailable && q.RunState != nil {
		q.Status = monitorStatusPartial
	}
	return q
}

func redisInt64Cmd(ctx context.Context, args ...string) (int64, error) {
	if db.Redis == nil {
		return 0, fmt.Errorf("redis 未配置")
	}
	ifaces := make([]interface{}, len(args))
	for i, a := range args {
		ifaces[i] = a
	}
	return db.Redis.Do(ctx, ifaces...).Int64()
}

// readStreamGroupsStrict 与 admin_metrics.go 的 readStreamGroups 同义，
// 但把"读不到"与"0 个组"分开：旧函数两者都回零值，面板上会把 Redis 故障显示成
// "没有消费者组"——那是一个看起来正常的错误结论。
func readStreamGroupsStrict(ctx context.Context, key string) (streamGroups, error) {
	var out streamGroups
	if db.Redis == nil {
		return out, fmt.Errorf("redis 未配置")
	}
	res, err := db.Redis.Do(ctx, "XINFO", "GROUPS", key).Result()
	if err != nil {
		return out, err
	}
	groups, ok := res.([]interface{})
	if !ok {
		return out, fmt.Errorf("XINFO GROUPS 返回形状未知: %T", res)
	}
	out.Groups = len(groups)
	for _, g := range groups {
		fields, ok := g.([]interface{})
		if !ok {
			continue
		}
		for i := 0; i+1 < len(fields); i += 2 {
			name, _ := fields[i].(string)
			switch name {
			case "pending":
				out.Pending += redisToInt64(fields[i+1])
			case "lag":
				out.Lag += redisToInt64(fields[i+1])
			case "consumers":
				out.Consumers += int(redisToInt64(fields[i+1]))
			case "last-delivered-id":
				id, _ := fields[i+1].(string)
				// 多组时取**最小**的 last-delivered-id：那是最慢的一组，
				// 它的下一条就是全局最老的未投递任务。
				if id != "" && (out.LastDeliveredID == "" || streamIDLess(id, out.LastDeliveredID)) {
					out.LastDeliveredID = id
				}
			}
		}
	}
	return out, nil
}

// streamIDLess 比较两个 Stream ID（`<ms>-<seq>`）的大小。
func streamIDLess(a, b string) bool {
	ams, aseq, _ := strings.Cut(a, "-")
	bms, bseq, _ := strings.Cut(b, "-")
	an, aerr := strconv.ParseInt(ams, 10, 64)
	bn, berr := strconv.ParseInt(bms, 10, 64)
	if aerr != nil || berr != nil {
		return a < b
	}
	if an != bn {
		return an < bn
	}
	as, _ := strconv.ParseInt(aseq, 10, 64)
	bs, _ := strconv.ParseInt(bseq, 10, 64)
	return as < bs
}

// oldestStreamWait 读**尚未投递**的第一条消息并换算等待时长。
// afterID 为空（没有消费者组）时退回"保留的第一条"。空 Stream 返回 (nil, "", nil)。
func oldestStreamWait(ctx context.Context, key, afterID string) (*float64, string, error) {
	if db.Redis == nil {
		return nil, "", fmt.Errorf("redis 未配置")
	}
	start := "-"
	if afterID != "" {
		// `(` 前缀是 XRANGE 的排他起点（Redis 6.2+）。
		start = "(" + afterID
	}
	res, err := db.Redis.Do(ctx, "XRANGE", key, start, "+", "COUNT", 1).Result()
	if err != nil && afterID != "" {
		// 老版本 Redis 不认 `(` 语法：退回"保留的第一条"，宁可偏大也不报错。
		res, err = db.Redis.Do(ctx, "XRANGE", key, "-", "+", "COUNT", 1).Result()
	}
	if err != nil {
		return nil, "", err
	}
	entries, ok := res.([]interface{})
	if !ok || len(entries) == 0 {
		return nil, "", nil
	}
	pair, ok := entries[0].([]interface{})
	if !ok || len(pair) < 1 {
		return nil, "", nil
	}
	id, _ := pair[0].(string)
	ms, _, hasDash := strings.Cut(id, "-")
	if !hasDash {
		return nil, "", nil
	}
	n, err := strconv.ParseInt(ms, 10, 64)
	if err != nil {
		return nil, "", nil
	}
	at := time.UnixMilli(n)
	wait := round2(time.Since(at).Seconds())
	if wait < 0 {
		wait = 0
	}
	return &wait, at.UTC().Format(time.RFC3339), nil
}

// queryAgentRunCounts 读 agent_runs 的活跃 / 等人 / 近 1h 失败计数。
func queryAgentRunCounts(ctx context.Context) (active, awaiting, failed int64, err error) {
	err = db.GlobalDBManager.QueryRow(ctx, `
		SELECT
		  COUNT(*) FILTER (WHERE status IN ('running','checkpointed','resuming'))              AS active,
		  COUNT(*) FILTER (WHERE status = 'awaiting_input')                                    AS awaiting,
		  COUNT(*) FILTER (WHERE status = 'failed' AND updated_at > now() - interval '1 hour') AS failed
		FROM agent_runs`).Scan(&active, &awaiting, &failed)
	return
}

// querySubagentTimeouts 读 subagent_runs 近 1h 的超时失败数。
func querySubagentTimeouts(ctx context.Context) (int64, error) {
	var n int64
	err := db.GlobalDBManager.QueryRow(ctx, `
		SELECT COUNT(*) FROM subagent_runs
		 WHERE status = 'failed'
		   AND error_code = 'timeout'
		   AND finished_at > now() - interval '1 hour'`).Scan(&n)
	return n, err
}

// countRunLeases 统计 Redis 中的 run 归属租约（每会话一条，TTL 300s）。
func countRunLeases(ctx context.Context) (int64, error) {
	keys, err := db.Redis.ScanAll(ctx, db.RedisKey("engine:run:")+"*", 500)
	if err != nil {
		return 0, err
	}
	return int64(len(keys)), nil
}

// ── 缓存 ──

func (h *AdminHandler) collectMonitorCache(ctx context.Context, engineInfo *pyEngineInfo) MonitorCache {
	meta := newSectionMeta(
		"Redis INFO memory/stats/keyspace",
		"python-engine GET /info → gateway.cache",
	)
	c := MonitorCache{MonitorSectionMeta: meta}

	if db.Redis == nil {
		c.unavailable("redis", "Redis 未配置，内存与命中率未知")
	} else {
		rc, err := collectRedisCacheStats(ctx)
		if err != nil {
			c.fail("redis", "INFO 采集失败: "+err.Error())
		} else {
			c.Redis = rc
		}
	}

	if h.pythonClient == nil {
		c.fail("python-engine", "引擎未配置，LLM 缓存命中未知")
	} else if engineInfo == nil {
		c.fail("python-engine", "GET /info 不可达，LLM 缓存命中未知")
	} else if engineInfo.Gateway.Cache == nil {
		c.fail("python-engine", "引擎未上报 gateway.cache（旧版本或缓存未启用）")
	} else {
		cache := engineInfo.Gateway.Cache
		c.LLM = &MonitorLLMCache{
			L1Hits:         cache.L1Hits,
			L2Hits:         cache.L2Hits,
			L3Hits:         cache.L3Hits,
			Misses:         cache.Misses,
			HitRatePercent: round2(cache.HitRate * 100),
			Source:         "python-engine GET /info → gateway.cache（hit_rate 为 0-1 小数，这里换成百分比）",
		}
	}

	if c.Redis == nil && c.LLM == nil {
		c.Status = monitorStatusUnavailable
	} else if c.Status == monitorStatusUnavailable {
		// 一侧不可用、另一侧有数据 ⇒ 部分可用，而不是整块不可用。
		c.Status = monitorStatusPartial
	}
	return c
}

func collectRedisCacheStats(ctx context.Context) (*MonitorRedisCache, error) {
	stats := redisInfoSection(ctx, "stats")
	memory := redisInfoSection(ctx, "memory")
	keyspace := redisInfoSection(ctx, "keyspace")
	if stats == nil && memory == nil && keyspace == nil {
		return nil, fmt.Errorf("INFO 全部为空")
	}
	out := &MonitorRedisCache{Source: "Redis INFO memory/stats/keyspace"}
	if memory != nil {
		if v, ok := infoFloat(memory, "used_memory"); ok {
			mb := round2(v / (1024 * 1024))
			out.UsedMemoryMB = &mb
		}
		if v, ok := infoFloat(memory, "maxmemory"); ok && v > 0 {
			mb := round2(v / (1024 * 1024))
			out.MaxMemoryMB = &mb
		}
		out.MaxmemoryPolicy = memory["maxmemory_policy"]
	}
	if stats != nil {
		hits := infoInt64(stats, "keyspace_hits")
		misses := infoInt64(stats, "keyspace_misses")
		out.KeyspaceHits, out.KeyspaceMisses = &hits, &misses
		if total := hits + misses; total > 0 {
			rate := round2(float64(hits) / float64(total) * 100)
			out.HitRatePercent = &rate
		}
		evicted := infoInt64(stats, "evicted_keys")
		expired := infoInt64(stats, "expired_keys")
		out.EvictedKeys, out.ExpiredKeys = &evicted, &expired
	}
	// keyspace：db0:keys=123,expires=100,avg_ttl=5000（多 db 时逐行累加）。
	var keys, expires, totalTTL int64
	var ttlCount int64
	for k, v := range keyspace {
		if !strings.HasPrefix(k, "db") {
			continue
		}
		for _, part := range strings.Split(v, ",") {
			name, val, ok := strings.Cut(part, "=")
			if !ok {
				continue
			}
			n, err := strconv.ParseInt(val, 10, 64)
			if err != nil {
				continue
			}
			switch name {
			case "keys":
				keys += n
			case "expires":
				expires += n
			case "avg_ttl":
				totalTTL += n
				ttlCount++
			}
		}
	}
	out.Keys, out.KeysWithTTL = &keys, &expires
	if ttlCount > 0 {
		avg := totalTTL / ttlCount
		out.AvgTTLMs = &avg
	}
	return out, nil
}

// infoFloat 从 INFO 映射取浮点值。INFO 的数值字段是十进制字符串。
func infoFloat(m map[string]string, key string) (float64, bool) {
	if m == nil {
		return 0, false
	}
	raw, ok := m[key]
	if !ok {
		return 0, false
	}
	v, err := strconv.ParseFloat(raw, 64)
	if err != nil {
		return 0, false
	}
	return v, true
}

func floatPtr(v float64) *float64 { return &v }

func windowLabel(d time.Duration) string {
	if d%time.Hour == 0 {
		return fmt.Sprintf("%dh", int(d.Hours()))
	}
	return fmt.Sprintf("%dm", int(d.Minutes()))
}
