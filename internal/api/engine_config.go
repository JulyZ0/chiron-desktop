package api

import (
	"crypto/subtle"
	"log/slog"
	"net"
	"net/http"

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
