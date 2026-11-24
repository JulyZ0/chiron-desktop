package engine

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/url"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
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
	// Sig 是可选签名：配置 ENGINE_DISCOVERY_SECRET 后，注册记录必须带上
	// HMAC(secret, instance_id|url)，否则不被采纳（见 verifyRegistrationSignature）。
	Sig string `json:"sig,omitempty"`
}

// ── 注册地址的最小信任（H35）──
//
// 旧实现直接信任 Redis 里任意 `url` 字段：任何能写这个键的主体（同 VPC 的
// 其它服务、被攻陷的引擎实例、配置错误的共享 Redis）都能把网关的任务流量
// 整体改道到自己的地址 —— 网关随后会把用户对话与凭据按该地址发出去。
// 现在地址必须过一层白名单式校验：协议、主机形态、端口、可选 host 白名单、
// 可选 HMAC 签名；不合规的注册记录只记日志、不进地址表。

var (
	errEngineURLInvalid    = errors.New("engine registration url invalid")
	errEngineURLNotAllowed = errors.New("engine registration url not allowed")
)

// engineAllowedHosts 读取 ENGINE_DISCOVERY_ALLOWED_HOSTS（逗号分隔，留空=不限制）。
func engineAllowedHosts() map[string]bool {
	raw := strings.TrimSpace(os.Getenv("ENGINE_DISCOVERY_ALLOWED_HOSTS"))
	if raw == "" {
		return nil
	}
	out := map[string]bool{}
	for _, part := range strings.Split(raw, ",") {
		if h := strings.ToLower(strings.TrimSpace(part)); h != "" {
			out[h] = true
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// engineLoopbackAllowed：内网/环回地址是否可注册。
// 默认允许（桌面本地栈就是 127.0.0.1），多机部署建议设为 0 并配合白名单。
func engineLoopbackAllowed() bool {
	v := strings.ToLower(strings.TrimSpace(os.Getenv("ENGINE_DISCOVERY_ALLOW_LOOPBACK")))
	return !(v == "0" || v == "false" || v == "no")
}

// ValidateEngineRegistrationURL 校验并归一化引擎注册地址。
//
// 返回值为归一化后的 `<scheme>://<host>[:port]`（去掉尾部斜杠/path），
// 由 applyDiscovery 直接写入地址表。任何不合规输入都返回错误，调用方必须跳过。
func ValidateEngineRegistrationURL(raw string) (string, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return "", errEngineURLInvalid
	}
	u, err := url.Parse(raw)
	if err != nil {
		return "", errEngineURLInvalid
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return "", errEngineURLInvalid
	}
	// 拒绝 userinfo（`http://evil@engine/` 这类直觉错位）与不透明部分，
	// 以及带 query/fragment 的地址：它们只可能是拼装错误或被刻意构造。
	if u.User != nil || u.Opaque != "" || u.RawQuery != "" || u.Fragment != "" {
		return "", errEngineURLInvalid
	}
	if u.Path != "" && u.Path != "/" {
		return "", errEngineURLInvalid
	}

	host := strings.ToLower(u.Hostname())
	if host == "" || strings.ContainsAny(host, " \t\r\n\\/@") {
		return "", errEngineURLInvalid
	}
	if port := u.Port(); port != "" {
		p, err := strconv.Atoi(port)
		if err != nil || p < 1 || p > 65535 {
			return "", errEngineURLInvalid
		}
	}

	if allowed := engineAllowedHosts(); allowed != nil && !allowed[host] {
		return "", errEngineURLNotAllowed
	}
	if isLocalOrPrivateHost(host) && !engineLoopbackAllowed() {
		return "", errEngineURLNotAllowed
	}
	return u.Scheme + "://" + u.Host, nil
}

// isLocalOrPrivateHost 判断是否环回/私网/链路本地/唯一本地地址。
func isLocalOrPrivateHost(host string) bool {
	if host == "localhost" || strings.HasSuffix(host, ".localhost") {
		return true
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return false
	}
	return ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsUnspecified()
}

// engineRegistrationSecret 返回注册签名密钥（未配置时为空 = 不强制签名）。
func engineRegistrationSecret() string {
	return strings.TrimSpace(os.Getenv("ENGINE_DISCOVERY_SECRET"))
}

// verifyRegistrationSignature 校验注册记录签名。
//
// 未配置密钥时返回 true（向后兼容：老引擎不签）。配置后必须匹配 —— 这是
// 「限制写入者」的手段：拿到 Redis 写权限但没有密钥的主体无法让网关改道。
func verifyRegistrationSignature(instanceID string, rec engineInstanceRecord) bool {
	secret := engineRegistrationSecret()
	if secret == "" {
		return true
	}
	if rec.Sig == "" {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(instanceID + "|" + rec.URL))
	want := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(strings.ToLower(rec.Sig)), []byte(want))
}

// ── 地址变化观测（轮换/撤销必须可观测）──

var engineAddressState struct {
	mu   sync.Mutex
	urls []string
}

// recordAddressChange 记录本次生效的地址集合并返回相对上一次的变化。
func recordAddressChange(current []string) (added, removed []string) {
	engineAddressState.mu.Lock()
	defer engineAddressState.mu.Unlock()

	prev := map[string]bool{}
	for _, u := range engineAddressState.urls {
		prev[u] = true
	}
	now := map[string]bool{}
	for _, u := range current {
		now[u] = true
		if !prev[u] {
			added = append(added, u)
		}
	}
	for _, u := range engineAddressState.urls {
		if !now[u] {
			removed = append(removed, u)
		}
	}
	engineAddressState.urls = append([]string(nil), current...)
	sort.Strings(added)
	sort.Strings(removed)
	return added, removed
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
		instanceID := strings.TrimPrefix(key, prefix)
		// 展示侧同样过滤非法地址：管理页不该把"网关永远不会使用的地址"伪装成存活实例。
		if !verifyRegistrationSignature(instanceID, rec) {
			continue
		}
		if _, err := ValidateEngineRegistrationURL(rec.URL); err != nil {
			continue
		}
		out = append(out, EngineInstance{
			ID:          instanceID,
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
			instanceID := strings.TrimPrefix(k, prefix)
			// ── 最小信任：注册地址必须通过协议/主机/端口校验（H35）──
			//
			// 不合规记录只记日志、不进入地址表：否则任何能写这个 Redis 键的主体
			// 都能把网关的引擎流量整体改道（任务与凭据一起被带走）。
			if !verifyRegistrationSignature(instanceID, rec) {
				slog.Warn("engine registration rejected: signature mismatch",
					"instance", instanceID, "url", rec.URL)
				continue
			}
			normalized, verr := ValidateEngineRegistrationURL(rec.URL)
			if verr != nil {
				slog.Warn("engine registration rejected: invalid url",
					"instance", instanceID, "url", rec.URL, "error", verr)
				continue
			}
			urls = append(urls, normalized)
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}

	urls = sortUnique(urls)
	added, removed := recordAddressChange(urls)
	if len(added) > 0 || len(removed) > 0 {
		// 轮换/撤销可观测：谁进来了、谁被摘掉了，必须能在日志里对齐时间线。
		slog.Info("engine address rotation",
			"added", added, "removed", removed, "active", len(urls))
	}
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
