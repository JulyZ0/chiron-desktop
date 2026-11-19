package api

import (
	"crypto/subtle"
	"log/slog"
	"net"
	"net/http"
	"strings"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// internalTokenMW 校验 X-Internal-Token，供 Go 网关内部端点（引擎配置下发）使用。
// 缺失/不匹配时返回 401，绝不透出解密后的敏感配置。
// P0-3: 使用常量时间比较防止时序攻击
// 对于写操作端点（db/execute, db/batch-execute），额外限制仅允许本地回环地址调用，
// 防止 internal_token 泄露后任意 SQL 写入。
func internalTokenMW(cfg *config.Config, next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		token := r.Header.Get("X-Internal-Token")
		if cfg.InternalToken == "" || token == "" || subtle.ConstantTimeCompare([]byte(cfg.InternalToken), []byte(token)) != 1 {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}

		// 对 DB 写操作端点额外限制仅回环地址可调用
		if r.URL.Path == "/v1/internal/db/execute" || r.URL.Path == "/v1/internal/db/batch-execute" {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			if err != nil {
				host = r.RemoteAddr
			}
			ip := net.ParseIP(host)
			if ip == nil || !(ip.IsLoopback() || ip.IsPrivate()) {
				slog.Warn("internal db write rejected from non-local address",
					"remote", r.RemoteAddr, "path", r.URL.Path)
				http.Error(w, "forbidden: db write only allowed from local/private network", http.StatusForbidden)
				return
			}
		}

		next(w, r)
	}
}

// envFirstKeys 是**由启动器按运行时端口注入**的键（Electron local-stack / run.py 先起
// 检索服务、拿到随机端口后才拼得出地址）。
//
// 后台「系统设置」里这些键留空只代表"没配"，不代表"要清空"：若把空串下发，引擎会用
// `model_copy(update=...)` 把它当成显式值，于是明明起了内置 pageindex-service，
// 引擎侧 `KNOWLEDGE_SERVICE_URL` 仍为空 ⇒ 知识库整页"服务不可用"。
// 因此这些键的空值一律不下发，让引擎回落到环境变量。
var envFirstKeys = map[string]struct{}{
	"knowledge_service_url": {},
	"internal_token":        {},
	"jwt_secret":            {},
	"gateway_internal_url":  {},
	"sandbox_service_url":   {},
}

// pruneEnvFirstEmpty 去掉 envFirstKeys 里的空值（只处理空字符串，其它类型原样保留）。
func pruneEnvFirstEmpty(in map[string]interface{}) map[string]interface{} {
	if len(in) == 0 {
		return in
	}
	out := make(map[string]interface{}, len(in))
	for k, v := range in {
		if _, guarded := envFirstKeys[k]; guarded {
			if s, ok := v.(string); ok && strings.TrimSpace(s) == "" {
				continue
			}
		}
		out[k] = v
	}
	return out
}

// EngineConfig GET /v1/internal/engine-config
// 供 Python AI 引擎启动时拉取「python」分类的后台配置（敏感键已由 APP_SECRET 解密）。
// 仅接受带 X-Internal-Token 的内部调用，防止配置外泄。
func EngineConfig(cfg *config.Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if db.Pool == nil {
			NotFound(w, "database unavailable")
			return
		}
		store := settings.New(db.Pool, cfg.AppSecret)
		m, err := store.LoadConfig(r.Context(), "python")
		if err != nil {
			slog.Error("engine config load failed", "error", err)
			InternalError(w, "failed to load engine config")
			return
		}
		// 启动器注入的键不允许被"空值"覆盖（见 envFirstKeys 的说明）。
		m = pruneEnvFirstEmpty(m)
		// 服务提供商目录（权威源在网关侧）：引擎按目录注册 provider，
		// 避免引擎内硬编码 provider 列表与服务端漂移。
		m["llm_provider_catalog"] = llmProviderCatalogPayload()
		OK(w, m)
	}
}

// KnowledgeConfig GET /v1/internal/knowledge-config
//
// 引擎侧「知识库用本地模式还是外部 pageindex 服务」的权威配置下发口
// （消费方 `app/knowledge/runtime.py::hydrate`）。
//
// 为什么需要它，而 engine-config 不够：`engine-config` 只下发 `python` 分类，
// 而这条配置属于 `knowledge` 分类 —— 它同时被后台「知识库」页与 Redis 广播
// （`chiron:settings:changed` 的 `knowledge`）消费。两条路径读的必须是**同一份**
// 数据，否则会出现"保存后页面上是新值、引擎重启后又变回旧值"。
//
// 引擎不缓存失败：网关没起来 / DB 不可用都返回错误码，引擎据此沿用启动注入地址
// （见 runtime.py 的 fail-open 说明），绝不因为读不到配置而清空当前服务地址。
func KnowledgeConfig(cfg *config.Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if db.Pool == nil {
			NotFound(w, "database unavailable")
			return
		}
		store := settings.New(db.Pool, cfg.AppSecret)
		m, err := store.LoadConfig(r.Context(), "knowledge")
		if err != nil {
			slog.Error("knowledge config load failed", "error", err)
			InternalError(w, "failed to load knowledge config")
			return
		}
		if m == nil {
			m = map[string]interface{}{}
		}
		OK(w, m)
	}
}
