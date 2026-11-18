package market

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// docFetcherFunc 让抓取层脱离网络单测。
type docFetcherFunc func(ctx context.Context, url string, maxBytes int) ([]byte, error)

func (f docFetcherFunc) FetchDocument(ctx context.Context, url string, maxBytes int) ([]byte, error) {
	return f(ctx, url, maxBytes)
}

// catalogJSON 造一份合法目录。
func catalogJSON(t *testing.T, items ...map[string]any) []byte {
	t.Helper()
	raw, err := json.Marshal(map[string]any{"version": 1, "items": items})
	if err != nil {
		t.Fatalf("marshal catalog: %v", err)
	}
	return raw
}

func goodItem(id string) map[string]any {
	return map[string]any{
		"kind":         "skill",
		"id":           id,
		"name":         "Name of " + id,
		"manifest_url": "https://example.com/" + strings.ReplaceAll(id, ":", "_") + ".json",
		"sha256":       strings.Repeat("a", 64),
	}
}

func remoteSrc(url string) Source {
	return Source{ID: SourceIDFromURL(url), Name: "test", URL: url, Type: SourceTypeOperator, Enabled: true}
}

// 外部源成功抓取：条目被解析、Source 由加载层填写（不信任条目自称的来源）。
func TestRemoteFetchParsesCatalog(t *testing.T) {
	url := "https://example.com/catalog.json"
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		return catalogJSON(t, goodItem("skill:a"), goodItem("skill:b")), nil
	}), RemoteOptions{})

	res, err := f.Fetch(context.Background(), remoteSrc(url))
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if len(res.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(res.Items))
	}
	for _, it := range res.Items {
		if it.Source != SourceIDFromURL(url) {
			t.Errorf("item %q source = %q, want 源的 id", it.ID, it.Source)
		}
		if it.ManifestURL == "" {
			t.Errorf("item %q 缺 manifest_url", it.ID)
		}
	}
	// 未签名条目必须产生提醒（签名不强制，但要告知）。
	if !hasWarning(res.Warnings, "unsigned_items") {
		t.Errorf("warnings = %+v, want unsigned_items", res.Warnings)
	}
}

// 单条坏数据只跳过自己，不能让整个源失效。
func TestRemoteFetchSkipsBadItems(t *testing.T) {
	bad := []map[string]any{
		{"kind": "skill", "id": "skill:no-manifest", "name": "x"},                          // 缺 manifest_url
		{"kind": "skill", "id": "skill:http", "name": "x", "manifest_url": "http://a/b.json"}, // 非 https
		{"kind": "skill", "id": "wrongprefix", "name": "x", "manifest_url": "https://a/b"},   // id 前缀不符
		{"kind": "bogus", "id": "bogus:x", "name": "x", "manifest_url": "https://a/b"},       // 类型不支持
		{"kind": "skill", "id": "skill:unknown-field", "name": "x", "manifest_url": "https://a/b", "evil": 1},
	}
	items := append([]map[string]any{goodItem("skill:good")}, bad...)

	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		return catalogJSON(t, items...), nil
	}), RemoteOptions{})

	res, err := f.Fetch(context.Background(), remoteSrc("https://example.com/c.json"))
	if err != nil {
		t.Fatalf("单条坏数据不应让整源失败: %v", err)
	}
	if len(res.Items) != 1 || res.Items[0].ID != "skill:good" {
		t.Fatalf("items = %+v, want 只保留 skill:good", res.Items)
	}
	// 每条被丢弃的条目都应留下痕迹，否则"市场里少了一条"无从排查。
	var skipped int
	for _, w := range res.Warnings {
		switch w.Reason {
		case "invalid_item", "unknown_item_field", "unsupported_kind", "missing_manifest_url":
			skipped++
		}
	}
	if skipped < len(bad) {
		t.Errorf("丢弃条目 %d，warnings 只记了 %d 条", len(bad), skipped)
	}
}

// 整份文档不合法 ⇒ 该源整体失败（说明对方给的不是市场文档）。
func TestRemoteFetchRejectsInvalidCatalog(t *testing.T) {
	cases := map[string][]byte{
		"not json":        []byte("{"),
		"unknown field":   []byte(`{"version":1,"items":[],"extra":1}`),
		"bad version":     []byte(`{"version":2,"items":[]}`),
		"array top-level": []byte(`[]`),
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
				return body, nil
			}), RemoteOptions{})
			if _, err := f.Fetch(context.Background(), remoteSrc("https://e.com/c.json")); err == nil {
				t.Fatal("应返回 source_invalid")
			} else if errReason(err) != "source_invalid" {
				t.Fatalf("err = %v, want source_invalid", err)
			}
		})
	}
}

// 单源条目数必须被截断：一个坏源不能把内存与页面撑爆。
func TestRemoteFetchTruncatesItems(t *testing.T) {
	items := make([]map[string]any, MaxItemsPerSource+5)
	for i := range items {
		items[i] = goodItem("skill:" + strings.Repeat("x", 1) + string(rune('a'+i%26)) + string(rune('A'+i/26)))
	}
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		return catalogJSON(t, items...), nil
	}), RemoteOptions{})

	res, err := f.Fetch(context.Background(), remoteSrc("https://e.com/c.json"))
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if len(res.Items) > MaxItemsPerSource {
		t.Fatalf("items = %d, want <= %d", len(res.Items), MaxItemsPerSource)
	}
	if !hasWarning(res.Warnings, "items_truncated") {
		t.Errorf("warnings = %+v, want items_truncated", res.Warnings)
	}
}

// 失败降级：保留上次成功快照并标 stale，**不返回错误**。
func TestRemoteFetchDegradesToStaleSnapshot(t *testing.T) {
	var fail atomic.Bool
	url := "https://e.com/c.json"
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		if fail.Load() {
			return nil, NewSourceError("source_unreachable", "connection refused")
		}
		return catalogJSON(t, goodItem("skill:a")), nil
	}), RemoteOptions{TTL: time.Millisecond})

	if _, err := f.Fetch(context.Background(), remoteSrc(url)); err != nil {
		t.Fatalf("首次抓取应成功: %v", err)
	}
	time.Sleep(2 * time.Millisecond) // 让缓存过期
	fail.Store(true)

	res, err := f.Fetch(context.Background(), remoteSrc(url))
	if err != nil {
		t.Fatalf("有快照可降级时不应返回错误: %v", err)
	}
	if !res.Stale {
		t.Error("降级结果必须标 Stale=true")
	}
	if res.StaleReason != "source_unreachable" {
		t.Errorf("StaleReason = %q, want source_unreachable", res.StaleReason)
	}
	if len(res.Items) != 1 {
		t.Fatalf("降级应返回快照内容，got %d 条", len(res.Items))
	}
	if !res.Items[0].Stale {
		t.Error("快照条目本身也应标 Stale")
	}
	if !hasWarning(res.Warnings, "stale_snapshot") {
		t.Errorf("warnings = %+v, want stale_snapshot", res.Warnings)
	}
}

// 没有快照可降级时必须返回错误（该源空 Tab，而不是静默返回空列表）。
func TestRemoteFetchErrorsWithoutSnapshot(t *testing.T) {
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		return nil, NewSourceError("source_unreachable", "refused")
	}), RemoteOptions{})

	_, err := f.Fetch(context.Background(), remoteSrc("https://e.com/c.json"))
	if err == nil {
		t.Fatal("无快照时应返回错误")
	}
	if errReason(err) != "source_unreachable" {
		t.Errorf("err = %v, want source_unreachable", err)
	}
}

// TTL 内的重复读取不能再打对方（页面刷新不该变成抓取风暴）。
func TestRemoteFetchCachesWithinTTL(t *testing.T) {
	var calls atomic.Int32
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		calls.Add(1)
		return catalogJSON(t, goodItem("skill:a")), nil
	}), RemoteOptions{TTL: time.Minute})
	src := remoteSrc("https://e.com/c.json")

	for i := 0; i < 5; i++ {
		if _, err := f.Fetch(context.Background(), src); err != nil {
			t.Fatalf("Fetch 失败: %v", err)
		}
	}
	if got := calls.Load(); got != 1 {
		t.Errorf("TTL 内抓取 %d 次，want 1", got)
	}
}

// 同一源的并发请求只发一次抓取（其余等结果）。
func TestRemoteFetchSingleflight(t *testing.T) {
	var calls atomic.Int32
	release := make(chan struct{})
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		calls.Add(1)
		<-release
		return catalogJSON(t, goodItem("skill:a")), nil
	}), RemoteOptions{})
	src := remoteSrc("https://e.com/c.json")

	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			_, _ = f.Fetch(context.Background(), src)
		}()
	}
	time.Sleep(50 * time.Millisecond)
	close(release)
	wg.Wait()

	if got := calls.Load(); got != 1 {
		t.Errorf("并发抓取 %d 次，want 1（单飞失效）", got)
	}
}

// 签名只做标注、不在列表阶段验签：验签需要 manifest 字节，列表里没有。
func TestRemoteFetchMarksSignedWithoutVerifying(t *testing.T) {
	item := goodItem("skill:signed")
	item["signature"] = map[string]any{"key_id": "k1", "value": base64.StdEncoding.EncodeToString(make([]byte, 64))}

	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		return catalogJSON(t, item), nil
	}), RemoteOptions{})

	res, err := f.Fetch(context.Background(), remoteSrc("https://e.com/c.json"))
	if err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if len(res.Items) != 1 {
		t.Fatalf("items = %d, want 1（有签名的条目不应被丢）", len(res.Items))
	}
	if !res.Items[0].Signed {
		t.Error("带 signature 的条目应标 Signed=true")
	}
	if hasWarning(res.Warnings, "unsigned_items") {
		t.Error("全部条目已签名时不应出现 unsigned_items 提醒")
	}
}

// verify 是安装阶段与详情接口使用的验签入口，这里直接验证它的正确性。
func TestRemoteFetcherVerify(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("生成密钥失败: %v", err)
	}
	f := NewRemoteFetcher(docFetcherFunc(nil), RemoteOptions{
		TrustedKeys: map[string]string{
			"good": base64.StdEncoding.EncodeToString(pub),
			"junk": "not-base64!!",
		},
	})

	digest := [32]byte{1, 2, 3}
	sig := &Signature{
		KeyID: "good",
		Value: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, digest[:])),
	}
	if err := f.verify(sig, digest); err != nil {
		t.Fatalf("合法签名应通过: %v", err)
	}
	// 内容被改（摘要不同）⇒ 必须失败，否则签名失去意义。
	if err := f.verify(sig, [32]byte{9}); err == nil {
		t.Error("摘要不匹配时验签必须失败")
	}
	if err := f.verify(&Signature{KeyID: "unknown", Value: sig.Value}, digest); err == nil {
		t.Error("未知 key_id 必须失败")
	}
	if err := f.verify(&Signature{KeyID: "junk", Value: sig.Value}, digest); err == nil {
		t.Error("坏公钥不应被当作可用（它应已被跳过）")
	}
}

// 坏公钥只跳过自己，不能让整张信任表失效。
func TestParseTrustedKeysSkipsBadEntries(t *testing.T) {
	pub, _, _ := ed25519.GenerateKey(rand.Reader)
	got := parseTrustedKeys(map[string]string{
		"ok":   base64.StdEncoding.EncodeToString(pub),
		"junk": "!!!",
		"shrt": base64.StdEncoding.EncodeToString([]byte("short")),
	})
	if len(got) != 1 {
		t.Fatalf("keys = %d, want 1", len(got))
	}
	if _, ok := got["ok"]; !ok {
		t.Error("合法公钥被丢掉")
	}
}

// 每源限流：超过窗口上限后不再出网，但降级为"不可用"而不是让目录失败。
func TestRemoteFetchRateLimitsPerSource(t *testing.T) {
	var calls atomic.Int32
	now := time.Now()
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		calls.Add(1)
		return catalogJSON(t, goodItem("skill:a")), nil
	}), RemoteOptions{})
	// TTL=0 会退回默认 5m，直接用 nowFunc 冻结时间跳过缓存：每次换 URL 都不同，
	// 这里改为让缓存立刻过期。
	f.ttl = 0
	f.nowFunc = func() time.Time { return now }
	src := remoteSrc("https://e.com/c.json")

	for i := 0; i < sourceRateLimit+5; i++ {
		now = now.Add(time.Second) // 每次加 1s，仍在同一 60s 窗口内
		_, _ = f.Fetch(context.Background(), src)
	}
	if got := calls.Load(); got > sourceRateLimit+1 {
		t.Errorf("抓取 %d 次，超过每源上限 %d", got, sourceRateLimit)
	}
}

// 空 URL 的源直接报 unknown_source，不做无意义的抓取。
func TestRemoteFetchRejectsEmptyURL(t *testing.T) {
	f := NewRemoteFetcher(docFetcherFunc(func(context.Context, string, int) ([]byte, error) {
		t.Fatal("空 URL 不应触发抓取")
		return nil, nil
	}), RemoteOptions{})
	_, err := f.Fetch(context.Background(), Source{ID: "x", Enabled: true})
	if err == nil || errReason(err) != "unknown_source" {
		t.Fatalf("err = %v, want unknown_source", err)
	}
}

// 文档体积上限必须传给下层：上层不允许放大。
func TestRemoteFetchPassesDocumentLimit(t *testing.T) {
	var gotLimit int
	f := NewRemoteFetcher(docFetcherFunc(func(_ context.Context, _ string, maxBytes int) ([]byte, error) {
		gotLimit = maxBytes
		return catalogJSON(t, goodItem("skill:a")), nil
	}), RemoteOptions{})
	if _, err := f.Fetch(context.Background(), remoteSrc("https://e.com/c.json")); err != nil {
		t.Fatalf("Fetch 失败: %v", err)
	}
	if gotLimit != MaxDocumentBytes {
		t.Errorf("maxBytes = %d, want %d", gotLimit, MaxDocumentBytes)
	}
}

// 源 id / 名字的生成必须稳定且可区分（同一域下的不同市场是不同源）。
func TestSourceIDAndName(t *testing.T) {
	a := SourceIDFromURL("https://e.com/a/catalog.json")
	b := SourceIDFromURL("https://e.com/b/catalog.json")
	if a == b {
		t.Error("同域不同路径必须是不同源 id")
	}
	if a != SourceIDFromURL("  https://e.com/a/catalog.json  ") {
		t.Error("源 id 应对首尾空白稳定")
	}
	if got := SourceNameFromURL("https://e.com/a/catalog.json"); got != "e.com/catalog" {
		t.Errorf("SourceNameFromURL = %q, want e.com/catalog", got)
	}
	if got := SourceNameFromURL("https://e.com"); got != "e.com" {
		t.Errorf("SourceNameFromURL = %q, want e.com", got)
	}
}

// 分类错误必须落到稳定原因码上（前端据此本地化，不展示原始异常串）。
func TestClassifyFetchError(t *testing.T) {
	cases := []struct {
		err  error
		want string
	}{
		{NewSourceError("source_timeout", "x"), "source_timeout"},
		{errors.New("context deadline exceeded"), "source_timeout"},
		{errors.New("payload_too_large: 2MB"), "payload_too_large"},
		{errors.New("dial problem"), "source_unreachable"},
	}
	for _, c := range cases {
		if got := errReason(classifyFetchError(c.err)); got != c.want {
			t.Errorf("classifyFetchError(%v) = %q, want %q", c.err, got, c.want)
		}
	}
}

func hasWarning(warns []Warning, reason string) bool {
	for _, w := range warns {
		if w.Reason == reason {
			return true
		}
	}
	return false
}
