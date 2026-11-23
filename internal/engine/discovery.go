package engine

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"sort"
	"strings"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// ── 引擎实例自动发现（批 E1）──
//
// 背景：网关的引擎地址来自静态 PYTHON_ENGINE_ADDRESS，扩容/缩容需人工同步并滚动重启。
// 本模块每 15s 从 Redis 引擎注册表（python-engine 启动后写入
// {REDIS_KEY_PREFIX}engine:instance:{instanceID}，TTL 60s + 心跳续期）拉取存活引擎，
// 动态更新 PythonClient 地址表：
//   - 注册表非空 → 动态优先（全量替换，扩容缩容自动生效）；
//   - 注册表为空 / Redis 不可用 → 回退构造时的静态地址（PYTHON_ENGINE_ADDRESS），
//     与未启用发现时的行为一致。

const (
	engineDiscoveryInterval = 15 * time.Second
	engineDiscoveryTimeout  = 3 * time.Second
)

// engineInstanceRecord 与 python-engine/app/engine_registry.py 写入的 JSON 对应。
//
// 后四个字段是 R6 的池观测补充：实例 id、启动时间、在途任务与进程资源。
// 旧引擎不写它们时为零值 —— 展示层按"未知"处理，不猜测。
type engineInstanceRecord struct {
	URL         string  `json:"url"`
	Version     string  `json:"version,omitempty"`
	LastSeen    string  `json:"last_seen,omitempty"`
	InstanceID  string  `json:"instance_id,omitempty"`
	StartedAt   int64   `json:"started_at,omitempty"`
	ActiveTasks int     `json:"active_tasks,omitempty"`
	CPUPercent  float64 `json:"cpu_percent,omitempty"`
	MemoryMB    float64 `json:"memory_mb,omitempty"`
}

// EngineInstance 是管理端看到的存活引擎快照。
type EngineInstance struct {
	// ID 是注册键的后缀（进程级实例标识），URL 可能因 --scale 重复。
	ID          string  `json:"id"`
	URL         string  `json:"url"`
	Version     string  `json:"version,omitempty"`
	LastSeen    string  `json:"last_seen,omitempty"`
	StartedAt   int64   `json:"started_at,omitempty"`
	ActiveTasks int     `json:"active_tasks"`
	CPUPercent  float64 `json:"cpu_percent"`
	MemoryMB    float64 `json:"memory_mb"`
}

// ListLiveInstances 读取 Redis 注册表里的存活引擎（TTL 由引擎心跳维持）。
//
// 不做 HTTP 探测：心跳本身就是"存活"的判据，且管理页不应因某个引擎卡住而被拖慢。
// Redis 不可用返回错误而不是空列表 —— 两者在管理端的含义完全不同。
func ListLiveInstances(ctx context.Context, rdb db.RedisClient) ([]EngineInstance, error) {
	if rdb == nil {
		return nil, errEngineRegistryUnavailable
	}
	prefix := db.RedisKey("engine:instance:")
	keys, err := rdb.ScanAll(ctx, prefix+"*", 200)
	if err != nil {
		return nil, err
	}
	out := make([]EngineInstance, 0, len(keys))
	for _, key := range keys {
		data, err := rdb.Get(ctx, key).Bytes()
		if err != nil {
			continue // 扫描与读取之间过期
		}
		var rec engineInstanceRecord
		if json.Unmarshal(data, &rec) != nil || rec.URL == "" {
			continue
		}
		out = append(out, EngineInstance{
			ID:          strings.TrimPrefix(key, prefix),
			URL:         rec.URL,
			Version:     rec.Version,
			LastSeen:    rec.LastSeen,
			StartedAt:   rec.StartedAt,
			ActiveTasks: rec.ActiveTasks,
			CPUPercent:  rec.CPUPercent,
			MemoryMB:    rec.MemoryMB,
		})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].ID < out[j].ID })
	return out, nil
}

var errEngineRegistryUnavailable = errors.New("engine registry unavailable")


// StartEngineDiscovery 启动引擎发现循环（每网关实例一次；Redis/client 为空则跳过）。
func StartEngineDiscovery(ctx context.Context, rdb db.RedisClient, client *PythonClient) {
	// 顺带注入 session→实例归属映射（engine:run:*，批次 4）读取所用的同一 Redis 客户端。
	// Redis 不可用时为 nil，RunOwner 直接返回 false，路由回退一致性哈希。
	SetAffinityRedis(rdb)
	if rdb == nil || client == nil {
		return
	}
	go func() {
		ticker := time.NewTicker(engineDiscoveryInterval)
		defer ticker.Stop()
		slog.Info("engine discovery started",
			"interval_seconds", int(engineDiscoveryInterval.Seconds()))
		for {
			select {
			case <-ctx.Done():
				slog.Info("engine discovery stopped")
				return
			case <-ticker.C:
				applyDiscovery(ctx, rdb, client)
			}
		}
	}()
}

// applyDiscovery 扫描注册表并应用动态/静态地址。
func applyDiscovery(ctx context.Context, rdb db.RedisClient, client *PythonClient) {
	dctx, cancel := context.WithTimeout(ctx, engineDiscoveryTimeout)
	defer cancel()

	prefix := db.RedisKey("engine:instance:")
	urls := make([]string, 0, 4)
	var cursor uint64
	for {
		keys, next, err := rdb.Scan(dctx, cursor, prefix+"*", 100).Result()
		if err != nil {
			slog.Debug("engine discovery scan failed", "error", err)
			break
		}
		for _, k := range keys {
			data, err := rdb.Get(dctx, k).Bytes()
			if err != nil {
				continue // TTL 过期/被删
			}
			var rec engineInstanceRecord
			if err := json.Unmarshal(data, &rec); err != nil || rec.URL == "" {
				continue
			}
			urls = append(urls, rec.URL)
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}

	urls = sortUnique(urls)
	if len(urls) > 0 {
		if client.SetAddresses(urls) {
			slog.Info("engine addresses updated via discovery", "instances", len(urls))
		}
		return
	}
	// 注册表为空（引擎未启用注册/全部下线）：回退静态地址
	static := client.StaticAddresses()
	if len(static) > 0 && client.SetAddresses(static) {
		slog.Warn("engine registry empty; falling back to static addresses", "instances", len(static))
	}
}
