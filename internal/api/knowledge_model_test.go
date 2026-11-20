package api

import (
	"strings"
	"testing"
)

// 知识库检索服务的模型来源：管理员在 /models 配的模型（llm_models + llm_provider_keys）
// 必须能被启动器取到。这里的用例只打**纯选择逻辑**（不连库）：
//   - 已配置 key 的云端 provider 可用；
//   - 缺 key 的云端 provider **不能**被选中（否则每次检索都是 401）；
//   - 无需 key 的自托管 provider（ollama/vllm）可以被选中；
//   - 一个都不可用时，原因必须是"去 /models 配置"，而不是"未配置自托管模型"。

func testCatalog() knowledgeCatalogLookup {
	return knowledgeCatalogLookupFromPresets([]llmProviderPreset{
		{ID: "openai", BaseURL: "https://api.openai.com/v1", RequiresKey: true},
		{ID: "ollama", BaseURL: "http://localhost:11434/v1", RequiresKey: false},
	})
}

func TestPickKnowledgeModelChoosesFirstKeyedProvider(t *testing.T) {
	rows := []knowledgeModelRow{
		{Provider: "openai", Model: "gpt-4o-mini"},
		{Provider: "ollama", Model: "qwen2.5:7b"},
	}
	cands := buildKnowledgeModelCandidates(rows, nil, map[string]int{"openai": 2}, testCatalog(), "")
	got, ok, reason := pickKnowledgeModel(cands)
	if !ok {
		t.Fatalf("有 key 的 openai 应当被选中，实际失败：%s", reason)
	}
	if got.Provider != "openai" || got.Model != "gpt-4o-mini" {
		t.Fatalf("选错候选：%+v", got)
	}
	// 目录默认端点必须被填上：没有 base_url 时 litellm 会去连默认上游
	if got.BaseURL != "https://api.openai.com/v1" {
		t.Fatalf("base_url 应回落到目录默认值，得到 %q", got.BaseURL)
	}
}

func TestPickKnowledgeModelSkipsProviderWithoutKey(t *testing.T) {
	// openai 有模型但没 key ⇒ 跳过；ollama 不需要 key ⇒ 选中
	rows := []knowledgeModelRow{
		{Provider: "openai", Model: "gpt-4o"},
		{Provider: "ollama", Model: "llama3.1:8b"},
	}
	cands := buildKnowledgeModelCandidates(rows, nil, nil, testCatalog(), "")
	got, ok, reason := pickKnowledgeModel(cands)
	if !ok {
		t.Fatalf("ollama 无需 key，应当可用，实际失败：%s", reason)
	}
	if got.Provider != "ollama" {
		t.Fatalf("应跳过缺 key 的 openai，实际选中 %+v", got)
	}
}

func TestPickKnowledgeModelRespectsBaseURLOverride(t *testing.T) {
	rows := []knowledgeModelRow{{Provider: "openai", Model: "gpt-4o"}}
	cands := buildKnowledgeModelCandidates(
		rows,
		map[string]string{"openai": "https://relay.example.com/v1/"},
		map[string]int{"openai": 1},
		testCatalog(),
		"",
	)
	got, ok, reason := pickKnowledgeModel(cands)
	if !ok {
		t.Fatalf("应当可用：%s", reason)
	}
	// 覆盖值优先于目录默认值，且尾部斜杠被裁掉（与引擎侧拼 URL 的口径一致）
	if got.BaseURL != "https://relay.example.com/v1" {
		t.Fatalf("覆盖的 base_url 未生效：%q", got.BaseURL)
	}
}

func TestPickKnowledgeModelPrefersDefaultModel(t *testing.T) {
	rows := []knowledgeModelRow{
		{Provider: "ollama", Model: "a-model"},
		{Provider: "ollama", Model: "b-model"},
	}
	cands := buildKnowledgeModelCandidates(rows, nil, nil, testCatalog(), "b-model")
	got, ok, reason := pickKnowledgeModel(cands)
	if !ok {
		t.Fatalf("应当可用：%s", reason)
	}
	if got.Model != "b-model" {
		t.Fatalf("default_model 应提到最前，实际 %q", got.Model)
	}
}

func TestPickKnowledgeModelFailureHintPointsToModelsPage(t *testing.T) {
	// 库里一个模型都没有：提示必须把人指到 /models，而不是让他去翻 env
	_, ok, reason := pickKnowledgeModel(nil)
	if ok {
		t.Fatal("没有候选时不该返回可用")
	}
	if !strings.Contains(reason, "/models") {
		t.Fatalf("失败原因必须指向 /models，得到 %q", reason)
	}

	// 有模型但缺 key：原因里要带上 provider 名，否则用户不知道去补哪一家
	cands := buildKnowledgeModelCandidates(
		[]knowledgeModelRow{{Provider: "openai", Model: "gpt-4o"}},
		nil, nil, testCatalog(), "",
	)
	_, ok, reason = pickKnowledgeModel(cands)
	if ok {
		t.Fatal("缺 key 时不该返回可用")
	}
	if !strings.Contains(reason, "openai") || !strings.Contains(reason, "/models") {
		t.Fatalf("缺 key 的原因要指名 provider 并指向 /models，得到 %q", reason)
	}
}

func TestPickKnowledgeModelEmptyProviderOrModelSkipped(t *testing.T) {
	rows := []knowledgeModelRow{
		{Provider: "", Model: "orphan"},
		{Provider: "ollama", Model: "  "},
		{Provider: "ollama", Model: "qwen2.5:7b"},
	}
	cands := buildKnowledgeModelCandidates(rows, nil, nil, testCatalog(), "")
	if len(cands) != 1 {
		t.Fatalf("空 provider/model 应被过滤，得到 %d 个候选：%+v", len(cands), cands)
	}
	got, ok, _ := pickKnowledgeModel(cands)
	if !ok || got.Model != "qwen2.5:7b" {
		t.Fatalf("应选中唯一有效候选，实际 %+v ok=%v", got, ok)
	}
}
