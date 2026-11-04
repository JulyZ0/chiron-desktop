package api

import (
	"context"
	"math"
	"runtime"
	"runtime/metrics"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/monitor"
)

// ─────────────────────────────────────────────────────────────
// /admin/monitor 运行时指标采集辅助。
//
// 设计原则：所有数值必须来自真实采集点（Go runtime / Redis / Python 引擎 /info），
// 采集失败时返回零值而不是伪造数据 —— 页面宁可显示 0，也不显示编造的数值。
// ─────────────────────────────────────────────────────────────

func round2(v float64) float64 { return math.Round(v*100) / 100 }

// ── monitor.Snapshot() 取值转换 ──
// Snapshot 的值类型混杂（atomic.Load 得到 int64，runtime.MemStats 得到 uint64/uint32，
// uptime 是 float64），统一走这两个函数安全取值。

func snapInt64(v interface{}) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case int32:
		return int64(n)
	case uint64:
		return int64(n)
	case uint32:
		return int64(n)
	case float64:
		return int64(n)
	}
	return 0
}

func snapFloat64(v interface{}) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case int64:
		return float64(n)
	case int:
		return float64(n)
	case uint64:
		return float64(n)
	}
	return 0
}

// ── 进程 CPU 占用率（runtime/metrics 累计 CPU 秒差值）──
// 不使用第三方依赖：Go 标准库 runtime/metrics 的
// /cpu/classes/total:cpu-seconds 是进程累计 CPU 消耗（秒），两次采样求差即可得占用率。

var (
	cpuSampleMu     sync.Mutex
	lastCPUSeconds  float64
	lastCPUSampleAt time.Time
)

func init() {
	// 记录基线，使首次请求也能给出有意义的差值窗口（进程启动至今的均值）。
	if v, ok := readCPUSeconds(); ok {
		lastCPUSeconds = v
		lastCPUSampleAt = time.Now()
	}
}

func readCPUSeconds() (float64, bool) {
	samples := []metrics.Sample{{Name: "/cpu/classes/total:cpu-seconds"}}
	metrics.Read(samples)
	if samples[0].Value.Kind() != metrics.KindFloat64 {
		return 0, false
	}
	return samples[0].Value.Float64(), true
}

// processCPUPercent 返回本进程 CPU 占用率（0-100，相对 GOMAXPROCS 个可用核心）。
func processCPUPercent() float64 {
	cur, ok := readCPUSeconds()
	if !ok {
		return 0
	}
	now := time.Now()
	cpuSampleMu.Lock()
	defer cpuSampleMu.Unlock()
	if lastCPUSampleAt.IsZero() {
		lastCPUSeconds, lastCPUSampleAt = cur, now
		return 0
	}
	elapsed := now.Sub(lastCPUSampleAt).Seconds()
	delta := cur - lastCPUSeconds
	lastCPUSeconds, lastCPUSampleAt = cur, now
	if elapsed <= 0 || delta < 0 {
		return 0
	}
	cores := float64(runtime.GOMAXPROCS(0))
	if cores <= 0 {
		cores = 1
	}
	return round2(delta / elapsed / cores * 100)
}

// ── 请求吞吐（requests_total 差值采样）──

var (
	qpsMu        sync.Mutex
	lastReqTotal int64
	lastReqAt    time.Time
)

// requestsPerSecond 用 monitor 的累计请求数差值估算窗口内 QPS；首次调用建立基线并返回 0。
func requestsPerSecond(total int64) float64 {
	now := time.Now()
	qpsMu.Lock()
	defer qpsMu.Unlock()
	if lastReqAt.IsZero() || total < lastReqTotal {
		lastReqTotal, lastReqAt = total, now
		return 0
	}
	elapsed := now.Sub(lastReqAt).Seconds()
	delta := total - lastReqTotal
	lastReqTotal, lastReqAt = total, now
	if elapsed <= 0 {
		return 0
	}
	return round2(float64(delta) / elapsed)
}

// ── Redis INFO ──

// redisInfoSection 解析 Redis INFO <section>，返回「字段 → 值」映射。
// Redis 不可用或解析失败返回 nil，调用方必须容忍。
func redisInfoSection(ctx context.Context, section string) map[string]string {
	if db.Redis == nil {
		return nil
	}
	raw, err := db.Redis.Do(ctx, "INFO", section).Text()
	if err != nil {
		return nil
	}
	out := make(map[string]string, 32)
	for _, line := range strings.Split(raw, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		out[strings.TrimSpace(k)] = strings.TrimSpace(v)
	}
	return out
}

func infoInt64(m map[string]string, key string) int64 {
	if m == nil {
		return 0
	}
	n, err := strconv.ParseInt(m[key], 10, 64)
	if err != nil {
		return 0
	}
	return n
}

// ── Redis Stream ──

// streamLen 返回 Stream 长度；键不存在或 Redis 不可用时返回 0。
func streamLen(ctx context.Context, key string) int64 {
	if db.Redis == nil {
		return 0
	}
	n, err := db.Redis.Do(ctx, "XLEN", key).Int64()
	if err != nil {
		return 0
	}
	return n
}

// streamGroups 是某个 Stream 上全部消费者组的汇总。
type streamGroups struct {
	Groups    int
	Pending   int64 // 已投递但未 ACK
	Lag       int64 // 未投递积压
	Consumers int
}

// readStreamGroups 通过 XINFO GROUPS 汇总消费者组信息（不硬编码 group 名）。
func readStreamGroups(ctx context.Context, key string) streamGroups {
	var out streamGroups
	if db.Redis == nil {
		return out
	}
	res, err := db.Redis.Do(ctx, "XINFO", "GROUPS", key).Result()
	if err != nil {
		return out
	}
	groups, ok := res.([]interface{})
	if !ok {
		return out
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
			}
		}
	}
	return out
}

func redisToInt64(v interface{}) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case string:
		i, _ := strconv.ParseInt(n, 10, 64)
		return i
	}
	return 0
}

// recentStreamTasks 读取 Stream 中最近 limit 条消息并映射为等待队列条目（真实数据）。
func recentStreamTasks(ctx context.Context, key string, limit int) []QueueTask {
	out := []QueueTask{}
	if db.Redis == nil {
		return out
	}
	res, err := db.Redis.Do(ctx, "XREVRANGE", key, "+", "-", "COUNT", limit).Result()
	if err != nil {
		return out
	}
	entries, ok := res.([]interface{})
	if !ok {
		return out
	}
	for i, e := range entries {
		pair, ok := e.([]interface{})
		if !ok || len(pair) < 2 {
			continue
		}
		msgID, _ := pair[0].(string)
		task := QueueTask{
			TaskID:   msgID,
			Position: i + 1, // 1 = 最新
			QueuedAt: streamIDTime(msgID),
		}
		fields, _ := pair[1].([]interface{})
		for j := 0; j+1 < len(fields); j += 2 {
			k, _ := fields[j].(string)
			v, _ := fields[j+1].(string)
			switch k {
			case "type", "event", "event_type":
				if task.Content == "" {
					task.Content = v
				}
			case "user_id", "tenant_id":
				if task.UserID == "" {
					task.UserID = v
				}
			}
		}
		out = append(out, task)
	}
	return out
}

// streamIDTime 从 Redis Stream ID（<毫秒时间戳>-<序号>）解析入队时间。
func streamIDTime(id string) string {
	ms, _, ok := strings.Cut(id, "-")
	if !ok {
		return ""
	}
	n, err := strconv.ParseInt(ms, 10, 64)
	if err != nil {
		return ""
	}
	return time.UnixMilli(n).Format(time.RFC3339)
}

// pyEngineInfo 对应 python-engine「GET /info」的响应结构。
type pyEngineInfo struct {
	Version       string  `json:"version"`
	InstanceID    string  `json:"instance_id"`
	UptimeSeconds int64   `json:"uptime_seconds"`
	MemoryMB      float64 `json:"memory_mb"`
	CPUPercent    float64 `json:"cpu_percent"`
	ActiveTasks   int     `json:"active_tasks"`
	Gateway       struct {
		Providers map[string]struct {
			CircuitState string  `json:"circuit_state"`
			AvgLatencyMs float64 `json:"avg_latency_ms"`
		} `json:"providers"`
		Cache *struct {
			L1Hits  int64   `json:"l1_hits"`
			L2Hits  int64   `json:"l2_hits"`
			L3Hits  int64   `json:"l3_hits"`
			Misses  int64   `json:"misses"`
			HitRate float64 `json:"hit_rate"`
		} `json:"cache"`
		TenantRoutes int `json:"tenant_routes"`
	} `json:"gateway"`
}

// fetchPyEngineInfo 拉取单个可用 Python 引擎实例的运行时指标。
// 引擎未配置或不可用时返回 nil（不伪造数据）。
func (h *AdminHandler) fetchPyEngineInfo(ctx context.Context) *pyEngineInfo {
	if h.pythonClient == nil {
		return nil
	}
	var info pyEngineInfo
	if err := h.pythonClient.GetJSON(ctx, "/info", &info); err != nil {
		return nil
	}
	return &info
}

// ── 看板聚合指标 ──
//
// 供 GET /v1/admin/metrics 使用。三个字段此前都不可用：queue_backlog 被错赋为
// 累计请求数、cache_hit_rate/api_latency_p99 从未返回（前端恒显示 0）。
// 现分别取真实来源，且与 /admin/monitor 各面板复用同一套采集逻辑。

// queueBacklog 返回事件队列（Redis Stream webhook:events）的真实积压消息数，
// 与 /admin/monitor 的「队列」面板同源。
func queueBacklog(ctx context.Context) int64 {
	return streamLen(ctx, db.RedisKey(webhookStreamName))
}

// cacheHitRate 返回引擎侧多级缓存的总命中率（百分比）。
// 与 GetCacheStats 同源：python-engine /info 的 gateway.cache.hit_rate。
// 引擎不可用时返回 0，不伪造。
func (h *AdminHandler) cacheHitRate(ctx context.Context) float64 {
	info := h.fetchPyEngineInfo(ctx)
	if info == nil || info.Gateway.Cache == nil {
		return 0
	}
	return round2(info.Gateway.Cache.HitRate * 100)
}

// requestLatencyP99Ms 返回请求耗时的 P99（毫秒），采样自 MonitoringMiddleware
// 写入的 RequestHistogram。样本不足（count==0）时返回 0。
func requestLatencyP99Ms() int64 {
	snap := monitor.RequestHistogram.Snapshot()
	if snap == nil {
		return 0
	}
	return snapInt64(snap["p99"])
}
