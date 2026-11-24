package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
	"github.com/athenavi/chiron/internal/storage"
)

// ── 系统设置变更跨副本广播（批 B-2′）──
//
// 背景：后台「系统设置」保存到 DB 后，只有收到保存请求的那个网关副本会立即生效
// （rate_limit 热更、redis 热换），其它副本沿用启动快照，造成多副本配置不一致。
//
// 方案：保存成功 → Publish {prefix}chiron:settings:changed；每网关副本的订阅者：
//   - rate_limit → 令牌桶限流器 Configure（即时生效）
//   - cors       → 运行时 CORS 白名单更新（即时生效）
//   - payment    → 支付凭据热重载 + 重建渠道客户端（即时生效）
//   - storage    → 文件后端（local/s3）热切换（即时生效，见 storage_reload.go）
//   - 其余分类   → slog.Warn 提示需滚动重启（如 redis 集群切换等高风险项）
//
// H35：广播**不再携带完整 config**。此前把整份 category config 原样塞进 Pub/Sub，
// 于是 storage/payment 这类含 access_key/secret/私钥的分类，其密文或明文会经由
// Redis 出现在所有订阅者的日志、抓包与 `MONITOR` 输出里 —— 而订阅者真正需要的
// 只是「哪个分类变了、什么版本、去 DB 重载」。现在：
//   - 只广播白名单内的非敏感字段（热更确实需要的最小集合）；
//   - 其余一律以「invalidate + 版本号」通知，订阅者从 DB 重新读取；
//   - 事件里带 `action`/`version`，轮换与撤销在日志里可对齐（可观测）。

type settingsChangedEvent struct {
	Category string `json:"category"`
	// Action 固定为 invalidate：告诉订阅者「以 DB 为准重新加载」，而不是「用这份值」。
	Action string `json:"action"`
	// Version 是配置内容摘要（前 12 位 hex），用于去重、观测与"哪一版生效"。
	Version string `json:"version,omitempty"`
	// Config 只含白名单字段；敏感分类下恒为空。
	Config map[string]interface{} `json:"config,omitempty"`
	Ts     int64                  `json:"ts"` // 毫秒时间戳，用于丢弃重启窗口的过期消息
}

// settingsStaleWindow 超过该时长的广播视为过期（实例长时间暂停恢复后不应重复应用旧配置）。
const settingsStaleWindow = 10 * time.Second

// settingsBroadcastAllowlist 定义每个分类允许进入 Pub/Sub 的字段。
//
// 判据：订阅者要**立即**应用、且泄露后不构成凭据风险的字段。
// 不在表里的分类一律只发失效通知（订阅者从 DB 重载）。
var settingsBroadcastAllowlist = map[string][]string{
	"rate_limit": {"global", "tenant", "user"},
	"cors":       {"origins"},
	"service":    {"service_mode"},
}

func settingsChangedChannel() string {
	return db.RedisKey("chiron:settings:changed")
}

// broadcastableSettings 过滤出可安全广播的字段（白名单 + 敏感键双重过滤）。
func broadcastableSettings(category string, config map[string]interface{}) map[string]interface{} {
	keys, ok := settingsBroadcastAllowlist[category]
	if !ok || len(config) == 0 {
		return nil
	}
	out := make(map[string]interface{}, len(keys))
	for _, k := range keys {
		v, present := config[k]
		if !present || v == nil {
			continue
		}
		// 双保险：即使将来有人把敏感键误加进白名单，也不会被广播出去。
		if settings.IsSensitive(k) {
			continue
		}
		out[k] = v
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// settingsConfigVersion 返回配置内容摘要（12 位 hex）。
// json.Marshal 对 map 键排序输出，因此同一份配置必然得到同一个版本号。
func settingsConfigVersion(category string, config map[string]interface{}) string {
	if len(config) == 0 {
		return ""
	}
	data, err := json.Marshal(config)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(append([]byte(category+"\x00"), data...))
	return hex.EncodeToString(sum[:])[:12]
}

// PublishSettingsChanged 后台保存「系统设置」成功后广播到所有网关副本。
func PublishSettingsChanged(ctx context.Context, category string, config map[string]interface{}) error {
	if db.Redis == nil {
		return nil // Redis 不可用：跳过广播（保存方为单副本，退化为现状行为）
	}
	ev := settingsChangedEvent{
		Category: category,
		Action:   "invalidate",
		Version:  settingsConfigVersion(category, config),
		Config:   broadcastableSettings(category, config),
		Ts:       time.Now().UnixMilli(),
	}
	data, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	// 可观测：省略了哪些字段比"发了什么"更值得留痕（轮换/撤销都靠它对齐）。
	slog.Info("settings change broadcast",
		"category", category, "version", ev.Version,
		"fields", len(ev.Config), "fields_omitted", len(config)-len(ev.Config))
	return db.Redis.Publish(ctx, settingsChangedChannel(), data).Err()
}

// StartSettingsSubscriber 每网关副本启动一次，消费系统设置变更广播。
// atomicStore 用于 storage 分类的热切换；store 是它的**回源句柄**
// （订阅者据此从 DB 取与保存方相同的配置，避免广播内容漂移）。
// atomicStore 为 nil 时 storage 分类退化：保持现状。
func StartSettingsSubscriber(ctx context.Context, rdb db.RedisClient, limiter *DistributedRateLimiter, store *settings.Store, atomicStore *storage.AtomicStore) {
	if rdb == nil {
		return
	}
	pubsub := rdb.Subscribe(context.Background(), settingsChangedChannel())
	go func() {
		defer func() {
			if r := recover(); r != nil {
				slog.Error("settings subscriber panic", "panic", r)
			}
		}()
		for msg := range pubsub.Channel() {
			var ev settingsChangedEvent
			if err := json.Unmarshal([]byte(msg.Payload), &ev); err != nil {
				slog.Warn("settings broadcast unmarshal failed", "error", err)
				continue
			}
			if ev.Ts > 0 && time.Since(time.UnixMilli(ev.Ts)) > settingsStaleWindow {
				continue
			}
			// 事件里没有值（或只有白名单字段）时，一律以 DB 为真相源重载。
			reloadFromDB := func(category string) map[string]interface{} {
				if store == nil {
					return nil
				}
				cfg, err := store.LoadConfig(ctx, category)
				if err != nil {
					slog.Warn("settings reload from db failed", "category", category, "error", err)
					return nil
				}
				return cfg
			}
			effective := ev.Config
			switch ev.Category {
			case "rate_limit":
				if limiter == nil {
					continue
				}
				if len(effective) == 0 {
					effective = reloadFromDB("rate_limit")
				}
				limiter.Configure(
					intFromValue(effective["global"], 0),
					intFromValue(effective["tenant"], 0),
					intFromValue(effective["user"], 0),
				)
				slog.Info("rate limiter hot-reloaded via settings broadcast", "version", ev.Version)
			case "cors":
				origins, ok := effective["origins"].(string)
				if !ok && len(effective) == 0 {
					cfg := reloadFromDB("cors")
					origins, ok = cfg["origins"].(string)
				}
				if ok {
					SetCORSAllowOrigin(origins)
					slog.Info("cors allowlist hot-reloaded via settings broadcast", "version", ev.Version)
				}
			case "service":
				// 服务模式开关是运行态，必须跨副本一致：否则 A 副本关了注册、B 还开着。
				mode, ok := effective["service_mode"].(bool)
				if !ok && len(effective) == 0 {
					cfg := reloadFromDB("service")
					mode, ok = cfg["service_mode"].(bool)
				}
				if ok {
					serviceModeEnabled.Store(mode)
					slog.Info("service mode hot-reloaded via settings broadcast",
						"enabled", mode, "version", ev.Version)
				}
				// bind_host / public_base_url 只能重启生效，这里不做热切换。
			case "storage":
				// 从 DB 重载并热切换文件后端（与保存方同一事实源，副本间不会漂移）。
				// 广播里**不含** storage 的任何凭据字段 —— 这正是 H35 要的最小暴露。
				applyStorageFromSettings(ctx, store, atomicStore)
				slog.Info("storage backend hot-reloaded from db", "version", ev.Version)
			default:
				// redis/s3/agent 等：涉及进程级组件（连接池等），
				// 热切高风险，提示滚动重启以消除副本间不一致
				slog.Warn("system settings changed on category; rolling restart required to apply",
					"category", ev.Category, "version", ev.Version)
			}
			// 兜底观测：事件若带了不该出现的敏感字段，这里明确留痕（说明发布方被改坏）。
			for k := range ev.Config {
				if settings.IsSensitive(k) {
					slog.Error("settings broadcast carried sensitive field; ignoring",
						"category", ev.Category, "field", fmt.Sprintf("%v", k))
					delete(ev.Config, k)
				}
			}
		}
	}()
}
