package api

// ── 用量事件的 provider 归属 ──
//
// **为什么需要它**
//
// `usage_events.provider` 一直是**可空且经常为空**的：写入点拿的是"这一次请求配置了
// 哪个 provider"，而实际跑的那个模型可能来自 fallback、子 Agent 或 auto 路由 —— 引擎
// 回包没带 provider 时，这一列就是 NULL。用户实测形态：`deepseek-v4.1-flash` 的行
// provider 为空、`cost_cents` 为 NULL。
//
// 空的 provider 有两个后果，第二个更糟：
//
//  1. 覆盖率 / 归因面板无法回答"钱花在哪个厂商"，只能看到一个空列；
//  2. 一旦将来按 (model, provider) 维度做对账，这些行只能被归进"未知"档 ——
//     而事实是**可查的**：同一个模型名在 `llm_models`（模型目录）与
//     `model_prices`（价格表）里都带着 provider。
//
// **口径：只做"查得到就回填"，查不到就留空。**
//
// 绝不按模型名前缀"猜"厂商（`gpt-*` ⇒ openai 之类）：同一个名字可以经聚合网关
// 走完全不同的 provider，猜出来的值会被后续所有统计当成事实。查不到时保持空串，
// 由读路径在响应里标 `provider_missing`，让"我们不知道"这件事可见 —— 与计费侧
// 「缺价写 NULL 而不是 0」是同一条原则。

import (
	"context"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/db"
)

// providerCacheTTL 是归属查询的缓存时长。
//
// 比价格缓存（5 分钟）长一倍：provider 归属是**比价格更慢的变量**（模型目录由
// 发现任务写入、一天变不了几次），而它的调用点更热 —— 每一次记账都会问一次。
const providerCacheTTL = 10 * time.Minute

// providerCacheMaxEntries 是缓存条目上限。
//
// 模型名数量是个位到百位量级；给 4096 只是为了挡住"模型名里带随机后缀"的异常
// 流量把 map 撑大。超限直接整体清空（比实现一个 LRU 便宜，且代价只是一次回源）。
const providerCacheMaxEntries = 4096

type providerCacheEntry struct {
	provider  string // 空串 = 查过但没有（负缓存，避免每次记账都回源）
	fetchedAt time.Time
}

var (
	providerCacheMu sync.RWMutex
	providerCache   = map[string]providerCacheEntry{}
)

// resolveProviderForModel 按模型名查它的 provider；查不到返回空串（**不猜**）。
//
// 查询顺序（先权威、后参考）：
//
//  1. `llm_models` —— 模型目录，管理员/模型发现写入的权威归属；
//  2. `model_prices` —— 价格表也带 provider（同步脚本按厂商写入）。
//
// 两步都空 ⇒ 返回 ""。任何查询错误同样返回 ""（**不缓存**）：一次库抖动不该让
// 这个模型在 10 分钟内永久归为"未知"。
func resolveProviderForModel(ctx context.Context, model string) string {
	model = strings.TrimSpace(model)
	if model == "" || db.GlobalDBManager == nil {
		return ""
	}

	now := time.Now()
	providerCacheMu.RLock()
	cached, ok := providerCache[model]
	providerCacheMu.RUnlock()
	if ok && now.Sub(cached.fetchedAt) < providerCacheTTL {
		return cached.provider
	}

	provider, failed := lookupProviderForModel(ctx, model)
	if failed {
		// 查询失败：不写缓存（负缓存只用于"查过了，确实没有"）。
		return provider
	}

	providerCacheMu.Lock()
	if len(providerCache) >= providerCacheMaxEntries {
		providerCache = make(map[string]providerCacheEntry)
	}
	providerCache[model] = providerCacheEntry{provider: provider, fetchedAt: now}
	providerCacheMu.Unlock()
	return provider
}

// lookupProviderForModel 是真正回源的那一步；第二个返回值表示"查询失败"。
func lookupProviderForModel(ctx context.Context, model string) (string, bool) {
	// ① 模型目录：`name` 是模型名，`id` 是 uuid（发现任务两者都写），两个都试一次
	// 比"只认其中一个"稳 —— 老行可能只有 id 对得上。
	row, err := db.GlobalDBManager.FetchOne(ctx, `
		SELECT COALESCE(NULLIF(provider, ''), '') AS provider
		  FROM llm_models
		 WHERE lower(COALESCE(name, '')) = lower($1)
		    OR lower(id) = lower($1)
		 ORDER BY enabled DESC NULLS LAST
		 LIMIT 1`, model)
	if err != nil {
		return "", true
	}
	if row != nil {
		if provider := strings.TrimSpace(stringOf(row["provider"])); provider != "" {
			return provider, false
		}
	}

	// ② 价格表：同步脚本按厂商写入；命中即用。
	row, err = db.GlobalDBManager.FetchOne(ctx, `
		SELECT COALESCE(NULLIF(provider, ''), '') AS provider
		  FROM model_prices
		 WHERE lower(model) = lower($1)
		   AND COALESCE(provider, '') <> ''
		 ORDER BY verified_at DESC NULLS LAST
		 LIMIT 1`, model)
	if err != nil {
		return "", true
	}
	if row != nil {
		if provider := strings.TrimSpace(stringOf(row["provider"])); provider != "" {
			return provider, false
		}
	}
	return "", false
}

// clearProviderCache 清空归属缓存（不传参：只在测试与运维重置时用）。
func clearProviderCache() {
	providerCacheMu.Lock()
	providerCache = map[string]providerCacheEntry{}
	providerCacheMu.Unlock()
}
