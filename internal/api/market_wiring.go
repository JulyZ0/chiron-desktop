package api

import (
	"context"
	"encoding/json"
	"log/slog"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/market"
	"github.com/athenavi/chiron/internal/settings"
)

// settingsCategoryMarket 是后台「资源市场」配置所在的 system_settings 分组。
// 必须同时存在于 admin_extras.go 的 settingsCategories 里，否则保存会被 400 拒绝。
const settingsCategoryMarket = "market"

// 后台配置键名 —— **与前端分支约定的契约，不可自行改名**。
//
//	catalog_sources 是 JSON 字符串数组（前端传数组，历史数据可能是 JSON 文本）
const (
	settingMarketRemoteEnabled    = "remote_enabled"
	settingMarketCatalogSources   = "catalog_sources"
	settingMarketAllowUserSources = "allow_user_sources"
	settingMarketAllowRemoteMCP   = "allow_remote_mcp"
	settingMarketMaxSources       = "max_sources"
)

// newMarketService 构造市场聚合服务。
//
// **内置目录永远可用**：它随二进制发布（`internal/market/catalog` 的 `//go:embed`），
// 不读配置、不联网、不依赖 DB。因此这里不存在"市场不可用"的分支 ——
// 最坏情况只是少了几个外部源。
//
// 配置来源分两层：
//   - env（`MARKET_*`）是**初值/兜底**；
//   - `store`（system_settings 的 market 分类）是**后台保存的覆盖层**，优先级更高。
//     store 为 nil（无 DB 的本地模式）时纯用 env —— 这也是初装形态：外部源关闭。
//
// 外部源**默认关闭**是刻意的发布语义：连外部源意味着本机开始访问第三方 URL，
// 必须由部署方或用户在设置页显式打开。
func newMarketService(cfg *config.Config, py *engine.PythonClient, store *settings.Store) *market.Service {
	mcfg := buildMarketConfig(cfg, loadMarketSettings(store))

	// 抓取层**有引擎客户端就注入**，不在这里判断总开关。
	//
	// 为什么把开关判断下移到 Service：fetcher 是构造期注入的，若在这里按开关
	// 决定是否注入，那"打开外部源"就永远和 fetcher 是否存在绑死。改成
	// `Service.LoadFor` 每次读取时按配置裁决后，同一个 Service 实例上
	// `Configure(cfg{RemoteEnabled:true})` 就能让外部源开始工作，无需重建。
	//
	// 当前后台保存路径没有接线到 Configure（保存 market 分类后需要重启才生效，
	// 与 redis 之外的分类一致）；TTL / TrustedKeys 同样属构造期参数。
	var fetcher market.Fetcher
	if py != nil {
		fetcher = market.NewRemoteFetcher(engineDocumentFetcher{py: py}, market.RemoteOptions{
			TTL:         marketTTL(cfg),
			Timeout:     marketFetchTimeout(cfg),
			TrustedKeys: mcfg.TrustedKeys,
		})
	}

	svc := market.NewService(fetcher)
	svc.Configure(mcfg)
	// 用户自建源落在插件数据目录下的 `market/` 子目录（`{PluginDataDir}/market`）：
	// 与 plugins.json 同级同构，复用同一套 per-user 目录约定与权限策略。
	svc.SetUserStore(market.NewFileSourceStore(marketUserSourceDir(cfg)))
	return svc
}

// marketUserSourceDir 返回用户源的存储根目录。cfg 为 nil 时交给
// market.NewFileSourceStore 自己回落到 `data/market`。
func marketUserSourceDir(cfg *config.Config) string {
	if cfg == nil || strings.TrimSpace(cfg.PluginDataDir) == "" {
		return ""
	}
	return filepath.Join(cfg.PluginDataDir, "market")
}

// marketTTL / marketFetchTimeout 取抓取层的时间参数（cfg 为 nil 时交给
// market.NewRemoteFetcher 用默认值）。
func marketTTL(cfg *config.Config) time.Duration {
	if cfg == nil {
		return 0
	}
	return cfg.MarketCatalogTTL
}

func marketFetchTimeout(cfg *config.Config) time.Duration {
	if cfg == nil {
		return 0
	}
	return cfg.MarketFetchTimeout
}

// loadMarketSettings 读取后台保存的 market 配置。
//
// 读失败只告警并回落 env：一次 DB 抖动不该让市场页变空（内置目录仍可用），
// 更不该让网关起不来。
func loadMarketSettings(store *settings.Store) map[string]interface{} {
	if store == nil {
		return nil
	}
	dbCfg, err := store.LoadConfig(context.Background(), settingsCategoryMarket)
	if err != nil {
		slog.Warn("market: load settings from db failed, falling back to env", "error", err)
		return nil
	}
	return dbCfg
}

// buildMarketConfig 把 env 与 DB 合并成一份 market.Config。
//
// 合并规则（有意区分两种键，别统一成"DB 覆盖 env"）：
//   - `remote_enabled`：**env 或 DB 任一为真即开启**。env 打开代表部署方已经
//     决定连外部源，后台页面不该能把它关掉（那会让运维的显式决定凭空失效）；
//   - 其余键：DB 有值即覆盖 env。页面上的开关/源列表/上限是运维当场改的意图，
//     比 env 里可能过期的初值更可信。
func buildMarketConfig(cfg *config.Config, dbCfg map[string]interface{}) market.Config {
	out := market.Config{}
	if cfg != nil {
		out.RemoteEnabled = cfg.MarketRemoteEnabled
		out.OperatorSources = cfg.MarketCatalogSources
		out.AllowUserSources = cfg.MarketAllowUserSources
		out.AllowRemoteMCP = cfg.MarketAllowRemoteMCP
		out.MaxSources = cfg.MarketMaxSources
		out.TrustedKeys = cfg.MarketTrustedKeys
	}

	if v, ok := dbBool(dbCfg[settingMarketRemoteEnabled]); ok {
		out.RemoteEnabled = out.RemoteEnabled || v
	}
	if list, ok := dbStringList(dbCfg[settingMarketCatalogSources]); ok {
		out.OperatorSources = list
	}
	if v, ok := dbBool(dbCfg[settingMarketAllowUserSources]); ok {
		out.AllowUserSources = v
	}
	if v, ok := dbBool(dbCfg[settingMarketAllowRemoteMCP]); ok {
		out.AllowRemoteMCP = v
	}
	if v, ok := dbInt(dbCfg[settingMarketMaxSources]); ok {
		out.MaxSources = v
	}
	// Normalized 负责把 max_sources 截到硬上限 20 —— 后台填多大都没用，
	// 这是刻意的：上限若可配大就等于没有上限。
	return out.Normalized()
}

// dbBool 从 LoadConfig 的任意值里取布尔。ok=false 表示"该键未配置/类型不认识"，
// 调用方据此保留 env 的值（"没配"与"配成 false"必须能区分）。
func dbBool(v interface{}) (val bool, ok bool) {
	switch t := v.(type) {
	case bool:
		return t, true
	case string:
		switch strings.ToLower(strings.TrimSpace(t)) {
		case "1", "true", "yes", "on":
			return true, true
		case "0", "false", "no", "off":
			return false, true
		}
	case float64:
		return t != 0, true
	}
	return false, false
}

// dbInt 从任意值里取整数（前端 JSON number 解出来是 float64）。
func dbInt(v interface{}) (int, bool) {
	switch t := v.(type) {
	case float64:
		return int(t), true
	case int:
		return t, true
	case string:
		if i, err := strconv.Atoi(strings.TrimSpace(t)); err == nil {
			return i, true
		}
	}
	return 0, false
}

// dbStringList 从任意值里取字符串数组。
//
// 两种落库形态都要支持：前端传的是**数组**（LoadConfig 还原成 []interface{}），
// 而人工/脚本写入的可能是**JSON 文本**（`["https://a", "https://b"]`）。
// 只认其中一种会让另一种配置"看起来保存成功、实际没生效"。
func dbStringList(v interface{}) ([]string, bool) {
	switch t := v.(type) {
	case []interface{}:
		out := make([]string, 0, len(t))
		for _, item := range t {
			if s, ok := item.(string); ok && strings.TrimSpace(s) != "" {
				out = append(out, s)
			}
		}
		return out, true
	case []string:
		return append([]string(nil), t...), true
	case string:
		trimmed := strings.TrimSpace(t)
		if trimmed == "" {
			return []string{}, true
		}
		var list []string
		if err := json.Unmarshal([]byte(trimmed), &list); err == nil {
			return list, true
		}
		// 退化形态：逗号/换行分隔的纯文本（运维手工填 env 时的习惯）。
		parts := strings.FieldsFunc(trimmed, func(r rune) bool {
			return r == ',' || r == '\n' || r == ';'
		})
		out := make([]string, 0, len(parts))
		for _, p := range parts {
			if p = strings.TrimSpace(p); p != "" {
				out = append(out, p)
			}
		}
		return out, true
	}
	return nil, false
}
