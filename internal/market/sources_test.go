package market

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ── ValidateSourceURL ──
//
// 这里只覆盖**形状**校验。IP/端口/重定向的拦截在出网口（Python fetch_url_safe），
// 因此"内网地址"这类输入在本层是**合法**的 —— 测试里显式断言这一点，
// 防止有人"顺手"在此处再写一套 SSRF 规则（那会变成第二个宽松绕过点）。
func TestValidateSourceURL(t *testing.T) {
	long := "https://e.com/" + strings.Repeat("a", maxSourceURLBytes)

	cases := []struct {
		name    string
		in      string
		want    string
		wantErr string // 期望的 SourceError.Code；空表示应成功
	}{
		{name: "正常", in: "https://m.example.com/catalog.json", want: "https://m.example.com/catalog.json"},
		{name: "带前导空白", in: "  https://m.example.com/a.json ", want: "https://m.example.com/a.json"},
		{name: "去掉 fragment", in: "https://m.example.com/c.json#sec", want: "https://m.example.com/c.json"},
		{name: "大写 scheme 视作 https", in: "HTTPS://m.example.com/c.json", want: "https://m.example.com/c.json"},
		{name: "空", in: "   ", wantErr: "invalid_source_url"},
		{name: "非 https", in: "http://m.example.com/c.json", wantErr: "source_url_not_https"},
		{name: "file scheme", in: "file:///etc/passwd", wantErr: "source_url_not_https"},
		{name: "带凭据", in: "https://user:pass@m.example.com/c.json", wantErr: "invalid_source_url"},
		{name: "无 host", in: "https:///c.json", wantErr: "invalid_source_url"},
		{name: "超长", in: long, wantErr: "invalid_source_url"},
		{name: "无法解析", in: "https://m.example.com/%zz", wantErr: "invalid_source_url"},
		// SSRF 不在本层：内网地址形状合法，放行。
		{name: "内网地址形状合法", in: "https://127.0.0.1:8443/c.json", want: "https://127.0.0.1:8443/c.json"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ValidateSourceURL(tc.in)
			if tc.wantErr == "" {
				if err != nil {
					t.Fatalf("ValidateSourceURL(%q) 不应报错: %v", tc.in, err)
				}
				if got != tc.want {
					t.Errorf("got %q, want %q", got, tc.want)
				}
				return
			}
			if err == nil {
				t.Fatalf("ValidateSourceURL(%q) 应报错，却得到 %q", tc.in, got)
			}
			var srcErr *SourceError
			if !errors.As(err, &srcErr) {
				t.Fatalf("错误应为 *SourceError，得到 %T", err)
			}
			if srcErr.Code != tc.wantErr {
				t.Errorf("code = %q, want %q", srcErr.Code, tc.wantErr)
			}
		})
	}
}

// ── Normalized / 20 上限 ──

func TestConfigNormalizedCapsMaxSources(t *testing.T) {
	cases := []struct {
		name string
		in   int
		want int
	}{
		{"零值回落默认", 0, DefaultMaxSources},
		{"负值回落默认", -3, DefaultMaxSources},
		{"小于上限原样", 7, 7},
		{"等于上限", 20, 20},
		// 关键：**大于 20 必须被截到 20**，否则"硬上限"只是句空话。
		{"超过上限截断", 21, DefaultMaxSources},
		{"远超上限截断", 1000, DefaultMaxSources},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cfg := Config{MaxSources: tc.in}.Normalized()
			if cfg.MaxSources != tc.want {
				t.Errorf("MaxSources = %d, want %d", cfg.MaxSources, tc.want)
			}
		})
	}
}

// Normalized 必须深拷贝：配置会跨 goroutine 共享（热更新 + 每次请求读取），
// 共享底层数组会让"改配置"与"读配置"在同一个 map 上并发。
func TestConfigNormalizedDeepCopies(t *testing.T) {
	orig := Config{
		OperatorSources: []string{"https://a.com/c.json"},
		TrustedKeys:     map[string]string{"k1": "pub"},
	}
	cp := orig.Normalized()
	cp.OperatorSources[0] = "https://changed.com/c.json"
	cp.TrustedKeys["k1"] = "changed"

	if orig.OperatorSources[0] != "https://a.com/c.json" {
		t.Error("OperatorSources 被共享：修改副本污染了原配置")
	}
	if orig.TrustedKeys["k1"] != "pub" {
		t.Error("TrustedKeys 被共享：修改副本污染了原配置")
	}
}

// ── OperatorSourceList ──

func TestOperatorSourceListDedupAndTruncate(t *testing.T) {
	// 3 条不同的 + 1 条重复 + 1 条非法（非 https）+ 1 条空
	cfg := Config{
		MaxSources: 2, // 故意调到 2，验证截断
		OperatorSources: []string{
			"https://b.example.com/cat.json",
			"https://a.example.com/cat.json",
			"https://b.example.com/cat.json", // 重复
			"http://plain.example.com/cat.json",
			"",
			"https://c.example.com/cat.json", // 超出上限，应被丢
		},
	}

	srcs := cfg.OperatorSourceList()
	if len(srcs) != 2 {
		t.Fatalf("源数 = %d, want 2（去重后按 max_sources 截断）: %+v", len(srcs), srcs)
	}
	// 截断保留的是**配置顺序靠前**的两条，不是字典序。
	if srcs[0].URL != "https://b.example.com/cat.json" {
		t.Errorf("srcs[0].URL = %q", srcs[0].URL)
	}
	if srcs[1].URL != "https://a.example.com/cat.json" {
		t.Errorf("srcs[1].URL = %q", srcs[1].URL)
	}
	for i, s := range srcs {
		if s.Type != SourceTypeOperator {
			t.Errorf("srcs[%d].Type = %q, want operator", i, s.Type)
		}
		if !s.Enabled {
			t.Errorf("srcs[%d].Enabled = false，运维源配置后应为启用", i)
		}
		if s.ID != SourceIDFromURL(s.URL) {
			t.Errorf("srcs[%d].ID = %q, want %q", i, s.ID, SourceIDFromURL(s.URL))
		}
		if s.Name != SourceNameFromURL(s.URL) {
			t.Errorf("srcs[%d].Name = %q, want %q", i, s.Name, SourceNameFromURL(s.URL))
		}
	}
}

// 上限即便配成 1000，这一层也只能给出 20 个 —— 双保险（Normalized + 这里）。
func TestOperatorSourceListHardCapsAt20(t *testing.T) {
	var urls []string
	for i := 0; i < 40; i++ {
		urls = append(urls, fmt.Sprintf("https://s%02d.example.com/cat.json", i))
	}
	srcs := Config{MaxSources: 1000, OperatorSources: urls}.OperatorSourceList()
	if len(srcs) != DefaultMaxSources {
		t.Fatalf("源数 = %d, want %d（硬上限）", len(srcs), DefaultMaxSources)
	}
}

// ── FileSourceStore ──

func newTestStore(t *testing.T) (*FileSourceStore, string) {
	t.Helper()
	dir := t.TempDir()
	return NewFileSourceStore(filepath.Join(dir, "market")), dir
}

func TestFileSourceStorePutOverwritesSameID(t *testing.T) {
	store, _ := newTestStore(t)
	ctx := context.Background()

	if err := store.Put(ctx, "u1", Source{ID: "src_1", Name: "旧名", URL: "https://a.com/c.json"}); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := store.Put(ctx, "u1", Source{ID: "src_1", Name: "新名", URL: "https://a.com/c.json"}); err != nil {
		t.Fatalf("Put(2): %v", err)
	}

	srcs, err := store.List(ctx, "u1")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(srcs) != 1 {
		t.Fatalf("源数 = %d, want 1（同 id 覆盖不产生重复）: %+v", len(srcs), srcs)
	}
	if srcs[0].Name != "新名" {
		t.Errorf("Name = %q, want 新名", srcs[0].Name)
	}
	if srcs[0].Type != SourceTypeUser {
		t.Errorf("Type = %q, want user", srcs[0].Type)
	}
}

// 用户源必须**按用户隔离**：一个用户看得见另一个用户的源，等于泄露了他的
// 内网拓扑线索。
func TestFileSourceStoreIsolatesUsers(t *testing.T) {
	store, _ := newTestStore(t)
	ctx := context.Background()

	if err := store.Put(ctx, "alice", Source{ID: "a", Name: "a", URL: "https://a.com/c.json"}); err != nil {
		t.Fatalf("Put alice: %v", err)
	}
	if err := store.Put(ctx, "bob", Source{ID: "b", Name: "b", URL: "https://b.com/c.json"}); err != nil {
		t.Fatalf("Put bob: %v", err)
	}

	alice, _ := store.List(ctx, "alice")
	bob, _ := store.List(ctx, "bob")
	if len(alice) != 1 || alice[0].ID != "a" {
		t.Errorf("alice 的源 = %+v", alice)
	}
	if len(bob) != 1 || bob[0].ID != "b" {
		t.Errorf("bob 的源 = %+v", bob)
	}
}

func TestFileSourceStoreDeleteIsIdempotent(t *testing.T) {
	store, _ := newTestStore(t)
	ctx := context.Background()

	if err := store.Put(ctx, "u1", Source{ID: "src_1", Name: "n", URL: "https://a.com/c.json"}); err != nil {
		t.Fatalf("Put: %v", err)
	}
	if err := store.Delete(ctx, "u1", "src_1"); err != nil {
		t.Fatalf("Delete: %v", err)
	}
	// 再删一次必须成功（前端重复点击/并发刷新不该报错）。
	if err := store.Delete(ctx, "u1", "src_1"); err != nil {
		t.Fatalf("Delete 幂等失败: %v", err)
	}
	// 删一个从存在过的 id 也一样。
	if err := store.Delete(ctx, "u1", "never-existed"); err != nil {
		t.Fatalf("Delete(unknown) 应成功: %v", err)
	}

	srcs, err := store.List(ctx, "u1")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(srcs) != 0 {
		t.Errorf("删除后仍有 %d 条源", len(srcs))
	}
}

// 空用户（从未存过）应返回空列表而不是错误 —— 市场页要能正常渲染"还没有源"。
func TestFileSourceStoreListMissingFile(t *testing.T) {
	store, _ := newTestStore(t)
	srcs, err := store.List(context.Background(), "nobody")
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(srcs) != 0 {
		t.Errorf("源数 = %d, want 0", len(srcs))
	}
}

// 文件损坏时返回空列表而不是错误：一个坏 JSON 不能让市场页整体失败
// （内置目录永远该可用），也不能让用户无法通过"再存一条"把文件覆盖修好。
func TestFileSourceStoreSurvivesCorruptFile(t *testing.T) {
	store, _ := newTestStore(t)
	ctx := context.Background()

	// 先写一条，再把它手工改坏。
	if err := store.Put(ctx, "u1", Source{ID: "a", Name: "a", URL: "https://a.com/c.json"}); err != nil {
		t.Fatalf("Put: %v", err)
	}
	path := store.path("u1")
	if err := os.WriteFile(path, []byte("{not json at all"), 0o600); err != nil {
		t.Fatalf("写坏文件: %v", err)
	}

	srcs, err := store.List(ctx, "u1")
	if err != nil {
		t.Fatalf("损坏文件不该返回错误: %v", err)
	}
	if len(srcs) != 0 {
		t.Errorf("损坏文件应视为无源，得到 %d 条", len(srcs))
	}

	// 关键：损坏后仍然可以写入，把文件修好。
	if err := store.Put(ctx, "u1", Source{ID: "b", Name: "b", URL: "https://b.com/c.json"}); err != nil {
		t.Fatalf("损坏后 Put: %v", err)
	}
	srcs, _ = store.List(ctx, "u1")
	if len(srcs) != 1 || srcs[0].ID != "b" {
		t.Errorf("修复后源 = %+v, want 只有 b", srcs)
	}
}

// 路径穿越：userID 绝不能决定目录上溯（`../tenant/evil` 只能落在 store 根内）。
func TestFileSourceStoreRejectsPathTraversal(t *testing.T) {
	store, root := newTestStore(t)
	ctx := context.Background()

	evil := []string{"../tenant/evil", "..", ".", "", "../../etc", `..\..\windows`}
	for _, userID := range evil {
		if err := store.Put(ctx, userID, Source{ID: "x", Name: "x", URL: "https://a.com/c.json"}); err != nil {
			t.Fatalf("Put(%q): %v", userID, err)
		}
		path := store.path(userID)
		rel, err := filepath.Rel(store.dir, path)
		if err != nil {
			t.Fatalf("Rel(%q): %v", path, err)
		}
		if strings.HasPrefix(rel, "..") || filepath.IsAbs(rel) {
			t.Errorf("userID=%q 逃出了 store 根目录: %s", userID, path)
		}
	}

	// 磁盘上不应出现 store 根之外的文件。
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatalf("ReadDir: %v", err)
	}
	for _, e := range entries {
		if e.Name() != "market" {
			t.Errorf("worktree 根下出现意外条目: %s", e.Name())
		}
	}
}

// 落盘结构必须是契约里的 `{"sources":[{id,name,url,added_at}]}`。
func TestFileSourceStoreFileFormat(t *testing.T) {
	store, _ := newTestStore(t)
	ctx := context.Background()
	if err := store.Put(ctx, "u1", Source{ID: "src_1", Name: "团队", URL: "https://a.com/c.json"}); err != nil {
		t.Fatalf("Put: %v", err)
	}

	data, err := os.ReadFile(store.path("u1"))
	if err != nil {
		t.Fatalf("ReadFile: %v", err)
	}
	var raw struct {
		Sources []struct {
			ID      string `json:"id"`
			Name    string `json:"name"`
			URL     string `json:"url"`
			AddedAt string `json:"added_at"`
		} `json:"sources"`
	}
	if err := json.Unmarshal(data, &raw); err != nil {
		t.Fatalf("落盘结构不是合法 JSON: %v; %s", err, data)
	}
	if len(raw.Sources) != 1 {
		t.Fatalf("sources = %+v", raw.Sources)
	}
	got := raw.Sources[0]
	if got.ID != "src_1" || got.Name != "团队" || got.URL != "https://a.com/c.json" {
		t.Errorf("记录 = %+v", got)
	}
	if got.AddedAt == "" {
		t.Error("added_at 应被填上")
	}
}

// NewFileSourceStore 的 dir 为空必须回落 `data/market`（与其它默认目录一致），
// 而不是把文件写到当前工作目录下。
func TestNewFileSourceStoreDefaultDir(t *testing.T) {
	store := NewFileSourceStore("  ")
	want := filepath.Join("data", "market")
	if store.dir != want {
		t.Errorf("dir = %q, want %q", store.dir, want)
	}
}

// truncateRunes 按字符截断（不能把多字节名字切坏）。
func TestTruncateRunes(t *testing.T) {
	long := strings.Repeat("源", maxSourceNameRunes+10)
	got := truncateRunes(long, maxSourceNameRunes)
	if n := len([]rune(got)); n != maxSourceNameRunes {
		t.Errorf("字符数 = %d, want %d", n, maxSourceNameRunes)
	}
	if got == "" || strings.ContainsRune(got, '\uFFFD') {
		t.Errorf("截断产生了坏字符: %q", got)
	}
	// 短名原样返回。
	if s := truncateRunes("abc", 10); s != "abc" {
		t.Errorf("got %q, want abc", s)
	}
}
