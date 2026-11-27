package market

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"testing"
)

// 内置目录随二进制发布，是市场页在任何网络状态下的底线内容。
// 因此这组断言盯的是"目录本身没有坏掉"，而不是某个具体条目名。
func TestBuiltinCatalogIsUsable(t *testing.T) {
	items := BuiltinItems()
	if len(items) == 0 {
		t.Fatal("内置目录为空：市场页在离线下将没有任何内容")
	}

	seen := make(map[string]bool)
	for _, it := range items {
		if it.Source != SourceBuiltin {
			t.Errorf("item %q source = %q, want %q", it.ID, it.Source, SourceBuiltin)
		}
		if !strings.HasPrefix(it.ID, string(it.Kind)+":") {
			t.Errorf("item %q id must be prefixed with %q", it.ID, string(it.Kind)+":")
		}
		if it.Name == "" {
			t.Errorf("item %q has empty name", it.ID)
		}
		if len(it.Payload) == 0 {
			t.Errorf("item %q has empty payload", it.ID)
		}
		// payload 必须是对象：安装流水线按字段名取值，数组会让它拿到 nil。
		var probe map[string]any
		if err := json.Unmarshal(it.Payload, &probe); err != nil {
			t.Errorf("item %q payload is not an object: %v", it.ID, err)
		}
		if seen[it.ID] {
			t.Errorf("duplicate builtin item id: %q", it.ID)
		}
		seen[it.ID] = true
	}
}

// 四类都必须有内容，否则前端对应 Tab 永远是空状态。
func TestBuiltinCatalogCoversAllKinds(t *testing.T) {
	items := BuiltinItems()
	for _, kind := range Kinds {
		found := false
		for _, it := range items {
			if it.Kind == kind {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("内置目录缺少 kind=%s 的条目", kind)
		}
	}
}

// 市场条目**只声明环境变量键名，不内嵌凭据**。
// 这条是硬约束：条目一旦带密钥进仓库，密钥就进了版本历史。
//
// 检查范围只针对**携带配置的条目**（agent / mcp / knowledge 的 payload 是结构化
// 配置）。技能条目的 payload 是 prompt 正文，正文里出现 "token"/"secret" 这类
// 词属于正常语言，不能按关键词判定泄漏。
func TestBuiltinPayloadCarriesNoSecrets(t *testing.T) {
	for _, it := range BuiltinItems() {
		// requires.env 只能列**键名**：出现 `KEY=value` 形态说明有值被写进了目录。
		for _, key := range it.Requires.Env {
			if strings.ContainsAny(key, "=:") {
				t.Errorf("item %q requires.env 含值（%q）：只能声明键名", it.ID, key)
			}
		}
		if it.Kind == KindSkill {
			continue
		}

		var payload map[string]any
		if err := json.Unmarshal(it.Payload, &payload); err != nil {
			continue // 结构问题由 TestBuiltinCatalogIsUsable 报
		}
		// mcp 可以带 env 段，但值必须是空的占位 —— 实际值由用户安装时填。
		if raw, ok := payload["env"]; ok {
			envMap, ok := raw.(map[string]any)
			if !ok {
				t.Errorf("item %q payload.env 必须是对象", it.ID)
				continue
			}
			for k, v := range envMap {
				if s, _ := v.(string); s != "" {
					t.Errorf("item %q payload.env[%q] 非空：catalog 不能内嵌凭据", it.ID, k)
				}
			}
		}
	}
}

func TestParseKind(t *testing.T) {
	for _, kind := range Kinds {
		got, err := ParseKind(string(kind))
		if err != nil || got != kind {
			t.Errorf("ParseKind(%q) = %q, %v", kind, got, err)
		}
	}
	// 非法值必须报错而不是静默归零：否则前端拼错类型会被渲染成"这个分类没有内容"。
	if _, err := ParseKind("bogus"); err == nil {
		t.Error("ParseKind(bogus) 应该返回错误")
	}
	if _, err := ParseKind(""); err == nil {
		t.Error("ParseKind(\"\") 应该返回错误（空串应在上层被显式跳过）")
	}
}

func TestFilter(t *testing.T) {
	items := []Item{
		{Kind: KindSkill, ID: "skill:a", Name: "alpha", Description: "first one", Tags: []string{"planning"}, Source: SourceBuiltin},
		{Kind: KindSkill, ID: "skill:b", Name: "beta", Description: "second one", Tags: []string{"Review"}, Source: SourceBuiltin},
		{Kind: KindMCP, ID: "mcp:c", Name: "gamma", Description: "alpha helper", Source: "team"},
	}

	cases := []struct {
		name string
		q    Query
		want []string
	}{
		{"空查询全返回", Query{}, []string{"skill:a", "skill:b", "mcp:c"}},
		{"按 kind", Query{Kind: KindSkill}, []string{"skill:a", "skill:b"}},
		{"按 source", Query{Source: SourceBuiltin}, []string{"skill:a", "skill:b"}},
		{"按 tag 不区分大小写", Query{Tag: "review"}, []string{"skill:b"}},
		{"按 q 命中描述", Query{Q: "ALPHA"}, []string{"skill:a", "mcp:c"}},
		{"组合条件", Query{Kind: KindSkill, Q: "alpha"}, []string{"skill:a"}},
		{"无命中", Query{Q: "zzz"}, nil},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := Filter(items, c.q)
			ids := make([]string, 0, len(got))
			for _, it := range got {
				ids = append(ids, it.ID)
			}
			if len(ids) != len(c.want) {
				t.Fatalf("got %v, want %v", ids, c.want)
			}
			for i := range ids {
				if ids[i] != c.want[i] {
					t.Fatalf("got %v, want %v", ids, c.want)
				}
			}
		})
	}
}

func TestFindIsSourceQualified(t *testing.T) {
	items := []Item{
		{Kind: KindSkill, ID: "skill:x", Source: SourceBuiltin},
		{Kind: KindSkill, ID: "skill:x", Source: "team"},
	}
	if it, ok := Find(items, "team", "skill:x"); !ok || it.Source != "team" {
		t.Errorf("Find 未按 source 限定：got %+v ok=%v", it, ok)
	}
	if _, ok := Find(items, "missing", "skill:x"); ok {
		t.Error("Find 在 source 不匹配时不应命中")
	}
	if _, ok := Find(items, "builtin", "skill:nope"); ok {
		t.Error("Find 在 id 不存在时不应命中")
	}
}

// fetcherFunc 让聚合逻辑可以脱离网络单测。
//
// 返回 `([]Item, error)` 而不是 `FetchResult`：绝大多数用例只关心"这个源给了什么
// / 为什么失败"，让它们去构造 FetchResult 只是噪音。降级（stale）用例使用
// fetchResultFunc（见下）。
type fetcherFunc func(ctx context.Context, src Source) ([]Item, error)

func (f fetcherFunc) Fetch(ctx context.Context, src Source) (FetchResult, error) {
	items, err := f(ctx, src)
	return FetchResult{Items: items}, err
}

// fetchResultFunc 用于需要表达 stale / warnings 的用例。
type fetchResultFunc func(ctx context.Context, src Source) (FetchResult, error)

func (f fetchResultFunc) Fetch(ctx context.Context, src Source) (FetchResult, error) {
	return f(ctx, src)
}

// 单源失败**不能**让整个目录失败：其它源照常返回，失败源进 warnings。
func TestLoadDegradesPerSource(t *testing.T) {
	svc := NewService(fetcherFunc(func(_ context.Context, src Source) ([]Item, error) {
		if src.ID == "bad" {
			return nil, NewSourceError("source_unreachable", "dial tcp: refused")
		}
		return []Item{{Kind: KindSkill, ID: "skill:remote", Name: "remote", Source: src.ID}}, nil
	}))
	svc.SetExternals([]Source{
		{ID: "good", Name: "good", Type: SourceTypeOperator, Enabled: true},
		{ID: "bad", Name: "bad", Type: SourceTypeOperator, Enabled: true},
	})

	snap := svc.Load(context.Background())

	if len(snap.Sources) != 3 { // builtin + good + bad
		t.Fatalf("sources = %d, want 3", len(snap.Sources))
	}
	if len(snap.Warnings) != 1 || snap.Warnings[0].Source != "bad" {
		t.Fatalf("warnings = %+v, want one for bad", snap.Warnings)
	}
	if snap.Warnings[0].Reason != "source_unreachable" {
		t.Errorf("warning reason = %q, want source_unreachable", snap.Warnings[0].Reason)
	}
	// 内置条目必须在
	hasBuiltin := false
	for _, it := range snap.Items {
		if it.Source == SourceBuiltin {
			hasBuiltin = true
		}
		if it.Source == "bad" {
			t.Error("失败源不应有条目进入结果")
		}
	}
	if !hasBuiltin {
		t.Error("内置条目缺失：外部源失败不应影响内置目录")
	}
	// 坏源在源列表里保留（Enabled=true, Count=0），页面才能解释"这个 Tab 为什么空"
	for _, s := range snap.Sources {
		if s.ID == "bad" {
			if !s.Enabled {
				t.Error("失败源应保持 Enabled=true（它是配置事实，不是健康状态）")
			}
			if s.LastError != "source_unreachable" {
				t.Errorf("失败源 LastError = %q, want source_unreachable", s.LastError)
			}
		}
	}
}

// 关闭的源不拉取、不出告警，但仍在源列表里（让用户看到"它存在，只是关着"）。
func TestLoadSkipsDisabledSources(t *testing.T) {
	called := false
	svc := NewService(fetcherFunc(func(context.Context, Source) ([]Item, error) {
		called = true
		return nil, errors.New("should not be called")
	}))
	svc.SetExternals([]Source{{ID: "off", Type: SourceTypeOperator, Enabled: false}})

	snap := svc.Load(context.Background())

	if called {
		t.Error("关闭的源不应触发抓取")
	}
	if len(snap.Warnings) != 0 {
		t.Errorf("关闭的源不应产生告警：%+v", snap.Warnings)
	}
	if len(snap.Sources) != 2 {
		t.Fatalf("sources = %d, want 2 (builtin + off)", len(snap.Sources))
	}
}

// 配了外部源但抓取层未启用（fetcher == nil）时，必须如实告知 remote_disabled，
// 而不是静默当成"没有外部源" —— 否则运维会以为配置没生效。
func TestLoadReportsMissingFetcher(t *testing.T) {
	svc := NewService(nil)
	svc.SetExternals([]Source{{ID: "team", Type: SourceTypeOperator, Enabled: true}})

	snap := svc.Load(context.Background())

	if len(snap.Warnings) != 1 || snap.Warnings[0].Reason != "remote_disabled" {
		t.Fatalf("warnings = %+v, want remote_disabled", snap.Warnings)
	}
	if len(snap.Items) == 0 {
		t.Error("内置条目应仍然可用")
	}
}

// 内置源恒为第一个：外部源反而排在后面，保证页面默认落在内置 Tab。
func TestLoadPutsBuiltinFirst(t *testing.T) {
	svc := NewService(fetcherFunc(func(_ context.Context, src Source) ([]Item, error) {
		return nil, nil
	}))
	svc.SetExternals([]Source{{ID: "team", Type: SourceTypeOperator, Enabled: true}})

	snap := svc.Load(context.Background())
	if len(snap.Sources) == 0 || snap.Sources[0].ID != SourceBuiltin {
		t.Fatalf("首个源 = %+v, want builtin", snap.Sources)
	}
}

// 结果必须稳定排序（kind 顺序 → name → id），否则分页会在两次请求间抖动。
func TestLoadSortsDeterministically(t *testing.T) {
	svc := NewService(fetcherFunc(func(_ context.Context, src Source) ([]Item, error) {
		return []Item{
			{Kind: KindMCP, ID: "mcp:z", Name: "z", Source: src.ID},
			{Kind: KindAgent, ID: "agent:z", Name: "z", Source: src.ID},
		}, nil
	}))
	svc.SetExternals([]Source{{ID: "team", Type: SourceTypeOperator, Enabled: true}})

	snap := svc.Load(context.Background())
	rank := map[Kind]int{KindAgent: 0, KindSkill: 1, KindKnowledge: 2, KindMCP: 3}
	for i := 1; i < len(snap.Items); i++ {
		prev, cur := snap.Items[i-1], snap.Items[i]
		if rank[prev.Kind] > rank[cur.Kind] {
			t.Fatalf("kind 顺序错误：%s 出现在 %s 之前", prev.Kind, cur.Kind)
		}
	}
}

func TestRequiresDeclaresFsNetShell(t *testing.T) {
	const raw = `{
	  "kind":"mcp","id":"x","name":"X",
	  "requires":{"env":["TOKEN"],"fs":["src/**"],"net":["api.example.com"],"shell":true}
	}`
	var def itemDef
	if err := json.Unmarshal([]byte(raw), &def); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(def.Requires.FS) != 1 || def.Requires.FS[0] != "src/**" {
		t.Fatalf("fs 解析错: %+v", def.Requires)
	}
	if len(def.Requires.Net) != 1 || def.Requires.Net[0] != "api.example.com" {
		t.Fatalf("net 解析错: %+v", def.Requires)
	}
	if !def.Requires.Shell {
		t.Fatal("shell 未解析")
	}
}
