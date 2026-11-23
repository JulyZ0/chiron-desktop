package api

// admin_engines.go —— 引擎池存活快照（R6 观测）。

import (
	"net/http"

	"github.com/athenavi/chiron/internal/db"
	"github.com/athenavi/chiron/internal/engine"
)

// ListEngines GET /v1/admin/engines
//
// 数据源是引擎自注册的 Redis 心跳（含在途任务与进程资源），不是逐个 HTTP 探测：
// 心跳过期即视为下线，与网关发现同一条判据。Redis 不可用时明确 503，
// 而不是返回空列表让运营以为"池空了"。
func (h *AdminHandler) ListEngines(w http.ResponseWriter, r *http.Request) {
	instances, err := engine.ListLiveInstances(r.Context(), db.Redis)
	if err != nil {
		JSONWithCode(w, http.StatusServiceUnavailable, CodeServiceUnavailable, nil, "engine registry unavailable")
		return
	}
	OK(w, map[string]interface{}{
		"instances": instances,
		"count":     len(instances),
	})
}
