package api

import (
	"log/slog"
	"strings"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/engine"
	"github.com/athenavi/chiron/internal/market"
)

// defaultMarketMaxSources 是源总数上限的兜底值（与 `MARKET_MAX_SOURCES` 默认一致）。
const defaultMarketMaxSources = 20

// newMarketService 构造市场聚合服务。
//
// **内置目录永远可用**：它随二进制发布（`internal/market/catalog` 的 `//go:embed`），
// 不读配置、不联网、不依赖 DB。因此这里不存在"市场不可用"的分支 ——
// 最坏情况只是少了几个外部源。
//
// 外部源是**默认关闭**的：初装（`MARKET_REMOTE_ENABLED` 未设）时这里不注入任何
// 外部源，市场页只有内置 Tab。这是刻意的发布语义 —— 连外部源意味着本机开始访问
// 第三方 URL，必须由部署方或用户在设置页显式打开。
func newMarketService(cfg *config.Config, py *engine.PythonClient) *market.Service {
	svc := market.NewService(nil)
	if cfg == nil || !cfg.MarketRemoteEnabled {
		return svc
	}

	fetcher := market.NewRemoteFetcher(engineDocumentFetcher{py: py}, market.RemoteOptions{
		TTL:         cfg.MarketCatalogTTL,
		Timeout:     cfg.MarketFetchTimeout,
		TrustedKeys: cfg.MarketTrustedKeys,
	})
	svc = market.NewService(fetcher)
	svc.SetExternals(buildOperatorSources(cfg))
	return svc
}

// buildOperatorSources 把 `MARKET_CATALOG_SOURCES` 转成源列表。
//
// 三个必须的处理：
//   - **去重**：同一个 URL 配两次会产生两个 Tab 显示同一批内容；
//   - **截断到上限**：`MARKET_MAX_SOURCES`（默认 20）是硬约束，超出的部分丢弃并
//     记日志 —— 静默接受会让"上限"变成一句空话；
//   - **优先 https**：非 https 的源直接跳过（manifest 的完整性校验在明文下失去意义）。
func buildOperatorSources(cfg *config.Config) []market.Source {
	max := cfg.MarketMaxSources
	if max <= 0 {
		max = defaultMarketMaxSources
	}

	out := make([]market.Source, 0, len(cfg.MarketCatalogSources))
	seen := make(map[string]bool, len(cfg.MarketCatalogSources))
	for _, raw := range cfg.MarketCatalogSources {
		url := strings.TrimSpace(raw)
		if url == "" || seen[url] {
			continue
		}
		if !strings.HasPrefix(strings.ToLower(url), "https://") {
			slog.Warn("market: skip non-https operator source", "url", url)
			continue
		}
		if len(out) >= max {
			slog.Warn("market: operator source limit reached, ignoring the rest",
				"max", max, "ignored", url)
			break
		}
		seen[url] = true
		out = append(out, market.Source{
			ID:   market.SourceIDFromURL(url),
			Name: market.SourceNameFromURL(url),
			URL:  url,
			Type: market.SourceTypeOperator,
			// Enabled 是**该源是否参与拉取**，不是"外部源总开关"。总开关由
			// MARKET_REMOTE_ENABLED 决定（上面已拦）；走到这里说明外部源已启用，
			// 运维配置的源即视为启用。
			Enabled: true,
		})
	}
	return out
}
