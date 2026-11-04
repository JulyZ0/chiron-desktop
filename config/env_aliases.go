package config

import (
	"log/slog"
	"strconv"
	"strings"
)

// ── 环境变量别名读取（配置漂移兜底）──────────────────────────────────
//
// 背景：历史文档与运行时告警文案曾把网关 PG 连接池上限写成 POSTGRES_MAX_CONNS
// （复数），而代码、compose 与部署文档读的是 POSTGRES_MAX_CONN（单数）。
// 名字不一致的后果不是报错而是**静默回落默认值**：运维按文档把预算从 20 下调到
// 10 之后，进程仍按 20 建池，N 个副本会直接打满 PostgreSQL 的 max_connections
// （默认 100），这正是 docs/deployment-multi-instance.md 第 8 节要防的场景。
//
// 因此：规范名优先，别名仍被接受，但命中别名时必须告警一次（不静默）。

// getIntAliases 按顺序读取候选环境变量名，返回首个有效值（正整数）。
// names[0] 是规范名；命中后续别名时打印弃用告警；全部未设置或无效时返回 def。
func getIntAliases(def int, names ...string) int {
	for i, name := range names {
		raw := strings.TrimSpace(getEnv(name, ""))
		if raw == "" {
			continue
		}
		n, err := strconv.Atoi(raw)
		if err != nil || n <= 0 {
			continue
		}
		if i > 0 {
			slog.Warn("environment variable name is deprecated, use the canonical name",
				"deprecated", name, "canonical", names[0], "value", n)
		}
		return n
	}
	return def
}
