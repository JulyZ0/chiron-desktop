package api

import (
	"log/slog"
	"net/http"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/settings"
)

// AgentConfig GET /v1/internal/agent-config
//
// 引擎侧「Agent 运行配置」（`agent` 分类）的权威下发口
// （消费方 `app/agent/runtime_config.py::hydrate`）。
//
// 为什么不能并进 `engine-config`：那个端点只下发 `python` 分类（模型密钥 / 沙箱等），
// 而这条配置属于 `agent` 分类 —— 它同时被后台「全局配置」页与 Redis 广播
// （`chiron:settings:changed` 的 `agent`）消费。两条路径读的必须是**同一份**数据，
// 否则会出现"保存后页面上是新值、引擎重启后又变回旧值"。
//
// 引擎对失败的处理与 knowledge-config 同一条纪律：网关不可达就沿用 env 兜底，
// 绝不因为读不到配置而把正在服务的行为策略重置成默认值。
func AgentConfig(cfg *config.Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if db.Pool == nil {
			NotFound(w, "database unavailable")
			return
		}
		store := settings.New(db.Pool, cfg.AppSecret)
		m, err := store.LoadConfig(r.Context(), "agent")
		if err != nil {
			slog.Error("agent config load failed", "error", err)
			InternalError(w, "failed to load agent config")
			return
		}
		if m == nil {
			// 空表 = "还没人在后台改过" ⇒ 回空对象，引擎据此沿用 env 兜底。
			m = map[string]interface{}{}
		}
		OK(w, m)
	}
}
