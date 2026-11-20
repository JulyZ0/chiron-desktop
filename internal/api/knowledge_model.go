package api

// ── 知识库检索服务（pageindex-service）的模型来源 ──
//
// 背景（实测过的故障链）：桌面单机 / 本机直跑时，pageindex-service 的
// `KNOWLEDGE_SELFHOSTED_CHAT_MODEL` / `_INDEX_MODEL` 只从启动器的环境变量继承，
// 而**真正配过模型的地方是网关 PG 的 `llm_models`**（管理员在「模型服务」页添加，
// key 在 `llm_provider_keys`）。两条来源互不相通，于是出现：
//   * 服务起来了、端口也通，但 `/healthz` 回 `status=disabled`；
//   * 「测试连接」按钮显示"未配置自托管模型，也未配置 Cloud 问答模型"；
//   * 而 /models 页面里明明有可用模型。
//
// 这个端点把「这台机器上第一个可用 chat 模型 + 对应 provider 的 base_url/key」
// 暴露给**启动器**（run.py / Electron local-stack），由它注入 pageindex 的 env。
// 只允许内部令牌调用（`internalTokenMW`），且只读。
//
// 优先级（由调用方实现，这里只给"库里有什么"）：
//   1. 管理员在「知识库」页显式写的 `KNOWLEDGE_SELFHOSTED_*`（env，最高）；
//   2. 本端点（llm_models + llm_provider_keys 的权威组合）；
//   3. 启动器继承的 `LLM_MODEL` / `OPENAI_MODEL` 等（开发态便利，最低）。

import (
	"context"
	"log/slog"
	"net/http"
	"sort"
	"strings"

	"github.com/athenavi/chiron/config"
	"github.com/athenavi/chiron/internal/db"
)

// knowledgeModelHintNoModel 是"库里一个可用模型都没有"时的**可操作**说明。
//
// 不能笼统写"未配置自托管模型"：用户照着那句话会去翻 env，而正确答案是去
// /models 页面加一个 provider + 模型（这是唯一权威来源）。
const knowledgeModelHintNoModel = "这台机器还没有可用模型，请先在 /models 配置（模型服务 → 添加服务商与模型）"

// knowledgeModelPayload 是 `/v1/internal/knowledge-model` 的响应体。
//
// 字段名与 pageindex 的 env 一一对应，启动器拿到后直接映射，不再做二次翻译 ——
// 两处各写一份映射关系，迟早会在改名时漂移。
type knowledgeModelPayload struct {
	Available bool   `json:"available"`
	Provider  string `json:"provider"`
	ChatModel string `json:"chat_model"`
	BaseURL   string `json:"base_url"`
	APIKey    string `json:"api_key"`
	// Reason 只在 Available=false 时非空（给人看的中文原因）。
	Reason string `json:"reason,omitempty"`
}

// knowledgeModelRow 是 llm_models 里与选择有关的列。
type knowledgeModelRow struct {
	Provider string
	Model    string
}

// knowledgeModelCandidate 是"可能可用"的一个候选（已带上 base_url 与 key 配置状态）。
type knowledgeModelCandidate struct {
	Provider    string
	Model       string
	BaseURL     string
	RequiresKey bool
	HasKey      bool
}

// knowledgeCatalogLookup 是选择逻辑需要的目录信息（纯函数参数，便于单测）。
type knowledgeCatalogLookup struct {
	// RequiresKey[id] = 该 provider 是否必须配 key（自托管推理可以没有）
	RequiresKey map[string]bool
	// DefaultBaseURL[id] = 目录默认端点
	DefaultBaseURL map[string]string
}

// buildKnowledgeModelCandidates 把 DB 行 + 覆盖 + key 计数 + 目录拼成候选列表。
//
// **纯函数**：不查库、不解密、不日志。选择规则的单元测试全部打在它身上 ——
// handler 只负责取数据（那部分与 DB 的契约由迁移与 live test 兜住）。
//
// defaultModel 非空时，同名行被提到最前（管理员在 system_settings 里设的默认模型
// 优先于"按 provider 名字排序的第一个"）。其余按 (provider, model) 稳定排序，
// 保证同一份数据每次给出的结果**逐字一致**（否则"今天能索引、明天不能"这种漂移无从解释）。
func buildKnowledgeModelCandidates(
	rows []knowledgeModelRow,
	baseURLOverrides map[string]string,
	keyCounts map[string]int,
	catalog knowledgeCatalogLookup,
	defaultModel string,
) []knowledgeModelCandidate {
	out := make([]knowledgeModelCandidate, 0, len(rows))
	for _, row := range rows {
		provider := strings.ToLower(strings.TrimSpace(row.Provider))
		model := strings.TrimSpace(row.Model)
		if provider == "" || model == "" {
			continue
		}
		baseURL := strings.TrimRight(strings.TrimSpace(baseURLOverrides[provider]), "/")
		if baseURL == "" {
			baseURL = strings.TrimRight(strings.TrimSpace(catalog.DefaultBaseURL[provider]), "/")
		}
		out = append(out, knowledgeModelCandidate{
			Provider:    provider,
			Model:       model,
			BaseURL:     baseURL,
			RequiresKey: catalog.RequiresKey[provider],
			HasKey:      keyCounts[provider] > 0,
		})
	}
	// 排序 = 优先级（先到先得，pickKnowledgeModel 取第一个能跑的）：
	//   ① 管理员设的 default_model；② **配了 key** 的 provider（明确配置过，最可能真能用）；
	//   ③ 无需 key 的自托管 provider（ollama/vllm：目录里有默认 loopback 端点，但服务
	//      未必在跑 —— 所以只能排在"配了 key"之后，不能因为它字母序靠前就抢走首选）；
	//   ④ 需要 key 却还没配的（pickKnowledgeModel 会跳过，但保留在列表里供原因说明）。
	rank := func(c knowledgeModelCandidate) int {
		switch {
		case c.HasKey:
			return 0
		case !c.RequiresKey:
			return 1
		default:
			return 2
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if defaultModel != "" {
			mi := out[i].Model == defaultModel
			mj := out[j].Model == defaultModel
			if mi != mj {
				return mi
			}
		}
		if ri, rj := rank(out[i]), rank(out[j]); ri != rj {
			return ri < rj
		}
		if out[i].Provider != out[j].Provider {
			return out[i].Provider < out[j].Provider
		}
		return out[i].Model < out[j].Model
	})
	return out
}

// pickKnowledgeModel 选出第一个**真正能跑**的候选。
//
// "能跑"的判据（缺一不可）：
//   - 有 base_url：没有端点时 litellm 会去连默认上游（错的地方），报错点在 SDK 深处；
//   - 需要 key 的 provider 确实有 key：否则每次检索都是 401，而页面只会说"服务不可用"。
//
// 返回 (选中, ok, 原因)。原因为空 = 没有可以解释的缺点（正常路径）。
func pickKnowledgeModel(cands []knowledgeModelCandidate) (knowledgeModelCandidate, bool, string) {
	var missingKey []string
	var missingBase []string
	for _, cand := range cands {
		if cand.BaseURL == "" {
			missingBase = append(missingBase, cand.Provider)
			continue
		}
		if cand.RequiresKey && !cand.HasKey {
			missingKey = append(missingKey, cand.Provider)
			continue
		}
		return cand, true, ""
	}
	if len(missingKey) > 0 {
		return knowledgeModelCandidate{}, false,
			knowledgeModelHintNoModel + "（已有模型但服务商缺 API key：" + strings.Join(uniqueStrings(missingKey), ", ") + "）"
	}
	if len(missingBase) > 0 {
		return knowledgeModelCandidate{}, false,
			knowledgeModelHintNoModel + "（服务商缺 base_url：" + strings.Join(uniqueStrings(missingBase), ", ") + "）"
	}
	return knowledgeModelCandidate{}, false, knowledgeModelHintNoModel
}

// uniqueStrings 去重保序（只为拼一条人读的原因，不值得引一个依赖）。
func uniqueStrings(in []string) []string {
	seen := make(map[string]struct{}, len(in))
	out := make([]string, 0, len(in))
	for _, v := range in {
		if _, ok := seen[v]; ok {
			continue
		}
		seen[v] = struct{}{}
		out = append(out, v)
	}
	return out
}

// knowledgeCatalogLookupFromPresets 把内建目录转成纯函数参数。
func knowledgeCatalogLookupFromPresets(presets []llmProviderPreset) knowledgeCatalogLookup {
	lookup := knowledgeCatalogLookup{
		RequiresKey:    make(map[string]bool, len(presets)),
		DefaultBaseURL: make(map[string]string, len(presets)),
	}
	for _, p := range presets {
		lookup.RequiresKey[p.ID] = p.RequiresKey
		lookup.DefaultBaseURL[p.ID] = p.BaseURL
	}
	return lookup
}

// knowledgeModelRows 读启用中的模型行（provider 与 name 都非空）。
func knowledgeModelRows(ctx context.Context) ([]knowledgeModelRow, error) {
	rows, err := db.Pool.Query(ctx,
		`SELECT COALESCE(provider, ''), COALESCE(name, '')
		   FROM llm_models
		  WHERE enabled = true
		  ORDER BY provider, name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []knowledgeModelRow
	for rows.Next() {
		var r knowledgeModelRow
		if rows.Scan(&r.Provider, &r.Model) != nil {
			continue
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// knowledgeActiveKeyCounts 统计各 provider 的**可用** key 条数（不解密，不碰明文）。
func knowledgeActiveKeyCounts(ctx context.Context) map[string]int {
	out := map[string]int{}
	if db.Pool == nil {
		return out
	}
	rows, err := db.Pool.Query(ctx,
		`SELECT provider, COUNT(*) FROM llm_provider_keys WHERE status = 'active' GROUP BY provider`)
	if err != nil {
		// 全新库、迁移还没跑：没有 key 可查 ≠ 端点不可用（下面会给出"去 /models 配置"的说明）
		slog.Debug("knowledge model: key counts unavailable", "error", err)
		return out
	}
	defer rows.Close()
	for rows.Next() {
		var provider string
		var n int
		if rows.Scan(&provider, &n) != nil {
			continue
		}
		out[strings.ToLower(strings.TrimSpace(provider))] = n
	}
	return out
}

// knowledgeDefaultModel 读 system_settings(python) 的 default_model（可为空）。
func knowledgeDefaultModel(ctx context.Context) string {
	if db.Pool == nil {
		return ""
	}
	var raw string
	err := db.Pool.QueryRow(ctx,
		`SELECT value::text FROM system_settings WHERE category = 'python' AND key = 'default_model'`).Scan(&raw)
	if err != nil {
		return ""
	}
	return llmSettingsString(raw)
}

// knowledgeProviderFirstKey 解密该 provider 的第一把 active key。
//
// **只为选中的 provider 解密**（而不是把所有 key 都解开）：解密面越小越好，
// 这条链路上没有任何需要其它 provider 明文的理由。
func knowledgeProviderFirstKey(cfg *config.Config, provider string) string {
	if db.Pool == nil || provider == "" {
		return ""
	}
	var encrypted string
	err := db.Pool.QueryRow(context.Background(),
		`SELECT encrypted_key FROM llm_provider_keys
		  WHERE provider = $1 AND status = 'active'
		  ORDER BY created_at, key_hash
		  LIMIT 1`, provider).Scan(&encrypted)
	if err != nil {
		return ""
	}
	plain, err := llmEnc(cfg).DecryptString(encrypted)
	if err != nil {
		slog.Warn("knowledge model: provider key decrypt failed", "provider", provider, "error", err)
		return ""
	}
	return plain
}

// KnowledgeModel GET /v1/internal/knowledge-model
//
// 供启动器（run.py / Electron local-stack）在拉起 pageindex-service **之前**取模型配置。
// 失败一律返回 available=false + 可操作原因（而不是 5xx）：启动器据此保留自己的 env
// 回退链，缺模型时 pageindex 的 healthz 也能把同一句话讲给用户。
func KnowledgeModel(cfg *config.Config) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if db.Pool == nil {
			OK(w, knowledgeModelPayload{Reason: "数据库不可用，无法读取模型配置"})
			return
		}
		ctx := r.Context()
		rows, err := knowledgeModelRows(ctx)
		if err != nil {
			slog.Error("knowledge model: query llm_models failed", "error", err)
			OK(w, knowledgeModelPayload{Reason: knowledgeModelHintNoModel})
			return
		}
		cands := buildKnowledgeModelCandidates(
			rows,
			llmProviderBaseURLOverrides(ctx),
			knowledgeActiveKeyCounts(ctx),
			knowledgeCatalogLookupFromPresets(llmProviderCatalogPayload()),
			knowledgeDefaultModel(ctx),
		)
		chosen, ok, reason := pickKnowledgeModel(cands)
		if !ok {
			OK(w, knowledgeModelPayload{Reason: reason})
			return
		}
		apiKey := ""
		if chosen.RequiresKey {
			apiKey = knowledgeProviderFirstKey(cfg, chosen.Provider)
			if apiKey == "" {
				// counts 说有 key 但实际解不出来（换了 APP_SECRET / 行被并发删）：
				// 不静默返回"可用"，否则检索会在 provider 侧 401，用户只觉得"知识库坏了"。
				OK(w, knowledgeModelPayload{
					Reason: knowledgeModelHintNoModel + "（服务商 " + chosen.Provider + " 的 key 无法解密）",
				})
				return
			}
		}
		OK(w, knowledgeModelPayload{
			Available: true,
			Provider:  chosen.Provider,
			ChatModel: chosen.Model,
			BaseURL:   chosen.BaseURL,
			APIKey:    apiKey,
		})
	}
}
