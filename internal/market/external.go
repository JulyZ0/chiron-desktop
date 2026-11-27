package market

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"strings"
	"sync"
	"time"
)

// DocumentFetcher 抓取一个 URL 的原始字节。
//
// 抽成接口而不是直接调引擎：**出网口只有一处**（Python 的
// `/v1/internal/market/fetch`，走 `fetch_url_safe` 做解析级校验）。Go 侧不自己
// 拼 HTTP 请求 —— 否则 SSRF 策略会有两套实现，绕过只需挑更宽松的那一边。
type DocumentFetcher interface {
	// FetchDocument 返回文档原始字节。实现必须已做过 SSRF 校验与体积上限。
	FetchDocument(ctx context.Context, url string, maxBytes int) ([]byte, error)
}

// 外部源的硬性约束（集中在此处便于审阅）。
const (
	// MaxDocumentBytes 单份文档（目录或 manifest）上限 1 MiB。
	MaxDocumentBytes = 1 << 20
	// MaxItemsPerSource 单源条目上限：一个坏源不能把内存/页面撑爆。
	MaxItemsPerSource = 200
	// maxConcurrentFetches 全局并发抓取上限，避免多源同时拖慢整个网关。
	maxConcurrentFetches = 4
	// sourceRateWindow / sourceRateLimit 每源限流：60 秒内最多 30 次抓取。
	sourceRateWindow = time.Minute
	sourceRateLimit  = 30
	// maxCacheBytes 进程内缓存上限，按 LRU 淘汰。
	maxCacheBytes = 8 << 20
)

// Signature 是外部条目的发布方签名（Ed25519 over sha256(manifest 原始字节)）。
//
// **签名可选**：不提供时条目仍可安装（依赖 HTTPS + sha256 完整性校验），
// 但市场页标「未签名」并给出提醒 —— 用户有权知道自己装的东西有没有来源背书。
type Signature struct {
	// KeyID 指向 `MARKET_TRUSTED_KEYS` 里的公钥。
	KeyID string `json:"key_id"`
	// Value 是 base64 编码的 Ed25519 签名。
	Value string `json:"value"`
}

// cacheEntry 是一个源上次成功抓取的快照。
type cacheEntry struct {
	items     []Item
	fetchedAt time.Time
	bytes     int
}

// RemoteFetcher 实现 Fetcher：抓取 / 解析 / 缓存 / 降级。
//
// 设计要点：
//   - **单飞**：同一源并发只发一次抓取，其余等结果；
//   - **TTL 缓存**：TTL 内直接返回，页面刷新不会打爆对方；
//   - **失败降级**：保留上次成功快照并标 Stale，不阻塞页面；
//   - **源隔离**：任一源失败只影响它自己（内置目录永远可用）。
type RemoteFetcher struct {
	docs    DocumentFetcher
	ttl     time.Duration
	timeout time.Duration
	// trustedKeys 为空表示不校验签名（条目一律标未签名）。
	trustedKeys map[string]ed25519.PublicKey

	sem chan struct{}

	mu      sync.Mutex
	cache   map[string]*cacheEntry // key = source URL
	order   []string               // 粗略 LRU（最近使用在尾部）
	bytes   int
	flight  map[string]*flightCall
	recent  map[string][]time.Time // 每源抓取时间窗
	nowFunc func() time.Time
}

type flightCall struct {
	done  chan struct{}
	items []Item
	stale bool
	err   error
}

// RemoteOptions 是 RemoteFetcher 的构造参数。
type RemoteOptions struct {
	TTL     time.Duration
	Timeout time.Duration
	// TrustedKeys 是 key_id → base64 公钥。为空表示不校验（全体标未签名）。
	TrustedKeys map[string]string
}

const (
	defaultCatalogTTL     = 5 * time.Minute
	defaultFetchTimeout   = 8 * time.Second
	maxCatalogTTL         = time.Hour
	maxFetchTimeout       = time.Minute
)

// NewRemoteFetcher 构造外部源抓取器。docs 不可为 nil。
func NewRemoteFetcher(docs DocumentFetcher, opts RemoteOptions) *RemoteFetcher {
	ttl := opts.TTL
	if ttl <= 0 {
		ttl = defaultCatalogTTL
	}
	if ttl > maxCatalogTTL {
		ttl = maxCatalogTTL
	}
	timeout := opts.Timeout
	if timeout <= 0 {
		timeout = defaultFetchTimeout
	}
	if timeout > maxFetchTimeout {
		timeout = maxFetchTimeout
	}
	return &RemoteFetcher{
		docs:        docs,
		ttl:         ttl,
		timeout:     timeout,
		trustedKeys: parseTrustedKeys(opts.TrustedKeys),
		sem:         make(chan struct{}, maxConcurrentFetches),
		cache:       make(map[string]*cacheEntry),
		flight:      make(map[string]*flightCall),
		recent:      make(map[string][]time.Time),
		nowFunc:     time.Now,
	}
}

// parseTrustedKeys 解析公钥表。**坏公钥只跳过自己**：一个写错的 key 不该让
// 整张表失效（那会把所有已签名的源判成 bad_signature）。
func parseTrustedKeys(in map[string]string) map[string]ed25519.PublicKey {
	if len(in) == 0 {
		return nil
	}
	out := make(map[string]ed25519.PublicKey, len(in))
	for id, b64 := range in {
		id = strings.TrimSpace(id)
		raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
		if err != nil || len(raw) != ed25519.PublicKeySize {
			slog.Warn("market: skip invalid trusted key", "key_id", id, "error", err)
			continue
		}
		out[id] = ed25519.PublicKey(raw)
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// Fetch 实现 Fetcher。它**永远不因单源故障而返回错误让整个目录失败**之外的
// 副作用：错误只描述这一个源。
func (f *RemoteFetcher) Fetch(ctx context.Context, src Source) (FetchResult, error) {
	if strings.TrimSpace(src.URL) == "" {
		return FetchResult{}, NewSourceError("unknown_source", "source has no url")
	}

	if items, ok := f.fromCache(src.URL); ok {
		return FetchResult{Items: items}, nil
	}

	res := f.singleflight(ctx, src)
	return res, res.err
}

func (f *RemoteFetcher) singleflight(ctx context.Context, src Source) FetchResult {
	f.mu.Lock()
	if call, ok := f.flight[src.URL]; ok {
		f.mu.Unlock()
		select {
		case <-call.done:
			return FetchResult{Items: call.items, Stale: call.stale, err: call.err}
		case <-ctx.Done():
			return FetchResult{err: NewSourceError("source_timeout", "request cancelled")}
		}
	}
	call := &flightCall{done: make(chan struct{})}
	f.flight[src.URL] = call
	f.mu.Unlock()

	res := f.fetchAndParse(ctx, src)

	f.mu.Lock()
	call.items, call.stale, call.err = res.Items, res.Stale, res.err
	delete(f.flight, src.URL)
	f.mu.Unlock()
	close(call.done)
	return res
}

func (f *RemoteFetcher) fetchAndParse(ctx context.Context, src Source) FetchResult {
	if !f.allow(src.URL) {
		// 限流算"这个源现在不可用"，不是目录失败 —— 降级到快照或空。
		return f.degrade(src, NewSourceError("source_rate_limited", "too many fetches for this source"))
	}

	select {
	case f.sem <- struct{}{}:
		defer func() { <-f.sem }()
	case <-ctx.Done():
		return FetchResult{err: NewSourceError("source_timeout", "request cancelled")}
	}

	fetchCtx, cancel := context.WithTimeout(ctx, f.timeout)
	defer cancel()

	raw, err := f.docs.FetchDocument(fetchCtx, src.URL, MaxDocumentBytes)
	if err != nil {
		return f.degrade(src, classifyFetchError(err))
	}

	items, warns, perr := parseRemoteCatalog(raw, src.ID, src.URL)
	if perr != nil {
		return f.degrade(src, perr)
	}

	// 来源标注：这里**只标注、不验签**。
	//
	// 理由：条目的 signature 覆盖的是 **manifest 字节**，而列表阶段只拿到 catalog，
	// manifest 还没下载。在列表里拿目录摘要去验 manifest 签名，只会把所有已签名
	// 条目误判成 bad_signature。真正的密码学校验发生在安装流水线（拿到 manifest
	// 之后）与条目详情接口（它按需抓一次 manifest）。
	unsigned := 0
	for i := range items {
		items[i].Signed = items[i].Signature != nil
		if !items[i].Signed {
			unsigned++
		}
	}
	if unsigned > 0 {
		// 不强制签名，但必须**提醒**：用户有权知道自己装的东西没有来源背书。
		warns = append(warns, Warning{
			Source: src.ID,
			Reason: "unsigned_items",
			Detail: fmt.Sprintf("%d", unsigned),
		})
	}

	f.store(src.URL, items)
	return FetchResult{Items: items, Warnings: warns}
}

// degrade 在抓取失败时回退到上次成功快照。
//
// 关键语义：**快照可用时不算失败** —— 返回条目 + Stale 标记，页面照常渲染，
// 只是提醒"这些内容是旧快照"。没有快照才返回错误（该源空 Tab）。
func (f *RemoteFetcher) degrade(src Source, err error) FetchResult {
	f.mu.Lock()
	entry, ok := f.cache[src.URL]
	f.mu.Unlock()
	if ok && len(entry.items) > 0 {
		stale := make([]Item, len(entry.items))
		copy(stale, entry.items)
		for i := range stale {
			stale[i].Stale = true
		}
		return FetchResult{
			Items: stale,
			Stale: true,
			StaleReason: errReason(err),
			err:         nil,
			Warnings: []Warning{{
				Source: src.ID,
				Reason: "stale_snapshot",
				Detail: errReason(err),
			}},
		}
	}
	return FetchResult{err: err}
}

func (f *RemoteFetcher) verify(sig *Signature, digest [32]byte) error {
	if f.trustedKeys == nil {
		return fmt.Errorf("no trusted keys configured")
	}
	pub, ok := f.trustedKeys[sig.KeyID]
	if !ok {
		return fmt.Errorf("unknown key_id %q", sig.KeyID)
	}
	raw, err := base64.StdEncoding.DecodeString(sig.Value)
	if err != nil {
		return fmt.Errorf("signature is not valid base64")
	}
	if !ed25519.Verify(pub, digest[:], raw) {
		return fmt.Errorf("ed25519 verify failed")
	}
	return nil
}

// allow 做每源限流（滑动窗口）。
func (f *RemoteFetcher) allow(key string) bool {
	now := f.nowFunc()
	f.mu.Lock()
	defer f.mu.Unlock()
	window := f.recent[key]
	cut := now.Add(-sourceRateWindow)
	kept := window[:0]
	for _, t := range window {
		if t.After(cut) {
			kept = append(kept, t)
		}
	}
	if len(kept) >= sourceRateLimit {
		f.recent[key] = kept
		return false
	}
	f.recent[key] = append(kept, now)
	return true
}

func (f *RemoteFetcher) fromCache(key string) ([]Item, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	entry, ok := f.cache[key]
	if !ok {
		return nil, false
	}
	if f.nowFunc().Sub(entry.fetchedAt) > f.ttl {
		return nil, false
	}
	out := make([]Item, len(entry.items))
	copy(out, entry.items)
	f.touchLocked(key)
	return out, true
}

func (f *RemoteFetcher) store(key string, items []Item) {
	size := estimateSize(items)
	f.mu.Lock()
	defer f.mu.Unlock()
	if old, ok := f.cache[key]; ok {
		f.bytes -= old.bytes
	}
	f.cache[key] = &cacheEntry{items: items, fetchedAt: f.nowFunc(), bytes: size}
	f.bytes += size
	f.touchLocked(key)
	f.evictLocked()
}

func (f *RemoteFetcher) touchLocked(key string) {
	for i, k := range f.order {
		if k == key {
			f.order = append(f.order[:i], f.order[i+1:]...)
			break
		}
	}
	f.order = append(f.order, key)
}

// evictLocked 按 LRU 淘汰，保证缓存不会无界增长。
func (f *RemoteFetcher) evictLocked() {
	for f.bytes > maxCacheBytes && len(f.order) > 0 {
		victim := f.order[0]
		f.order = f.order[1:]
		if entry, ok := f.cache[victim]; ok {
			f.bytes -= entry.bytes
			delete(f.cache, victim)
		}
	}
}

func estimateSize(items []Item) int {
	total := 0
	for _, it := range items {
		total += len(it.ID) + len(it.Name) + len(it.Description) + len(it.Payload) + 128
	}
	return total
}

// classifyFetchError 把下层错误映射成稳定原因码。
//
// 下层（Python 端点）已经给出原因码；这里再兜一层是为了不把任意错误串
// 直接当 code 透给前端。
func classifyFetchError(err error) error {
	var se *SourceError
	if errors.As(err, &se) {
		return se
	}
	msg := strings.ToLower(err.Error())
	switch {
	case strings.Contains(msg, "timeout") || strings.Contains(msg, "deadline exceeded"):
		return NewSourceError("source_timeout", err.Error())
	case strings.Contains(msg, "payload_too_large") || strings.Contains(msg, "too large"):
		return NewSourceError("payload_too_large", err.Error())
	}
	return NewSourceError("source_unreachable", err.Error())
}

// remoteCatalog 是外部源目录的线上形态。
//
// 只允许 `version` 与 `items` 两个顶层字段；条目字段见 remoteItemKeys。
// **多做一层 map 检查**而不是直接 struct 解析：白名单的意义是"出现未知字段就
// 丢弃该条目"，而 `DisallowUnknownFields` 会让整份文档失败 —— 一条坏条目
// 不该干掉一个源。
type remoteCatalog struct {
	Version int               `json:"version"`
	Items   []json.RawMessage `json:"items"`
}

var remoteCatalogKeys = map[string]bool{
	"version":   true,
	"items":     true,
	"signature": true,
}

// remoteItemKeys 是外部条目的字段白名单。
var remoteItemKeys = map[string]bool{
	"kind":         true,
	"id":           true,
	"name":         true,
	"description":  true,
	"version":      true,
	"author":       true,
	"tags":         true,
	"homepage":     true,
	"payload":      true,
	"requires":     true,
	"manifest_url": true,
	"sha256":       true,
	"signature":    true,
	"min_engine":   true,
}

// parseRemoteCatalog 解析并校验外部目录。
//
// 失败语义：整份文档不合法 → error（该源整体不可用）；单条不合法 → 跳过并记
// warning。这个区分很重要：前者说明"对方给的不是市场文档"，后者只是它内部有一条坏的。
func parseRemoteCatalog(raw []byte, sourceID, sourceURL string) ([]Item, []Warning, error) {
	var top map[string]json.RawMessage
	if err := json.Unmarshal(raw, &top); err != nil {
		return nil, nil, NewSourceError("source_invalid", "catalog is not a JSON object")
	}
	for k := range top {
		if !remoteCatalogKeys[k] {
			return nil, nil, NewSourceError("source_invalid", "unexpected top-level field: "+k)
		}
	}

	var cat remoteCatalog
	if err := json.Unmarshal(raw, &cat); err != nil {
		return nil, nil, NewSourceError("source_invalid", "catalog shape is invalid: "+err.Error())
	}
	if cat.Version != 1 {
		return nil, nil, NewSourceError("source_invalid", "unsupported catalog version")
	}

	items := make([]Item, 0, len(cat.Items))
	warnings := make([]Warning, 0, 4)
	for _, rawItem := range cat.Items {
		if len(items) >= MaxItemsPerSource {
			warnings = append(warnings, Warning{
				Source: sourceID,
				Reason: "items_truncated",
				Detail: fmt.Sprintf("%d", MaxItemsPerSource),
			})
			break
		}
		item, reason := parseRemoteItem(rawItem, sourceID)
		if reason != "" {
			warnings = append(warnings, Warning{Source: sourceID, Reason: reason})
			continue
		}
		items = append(items, item)
	}
	return items, warnings, nil
}

func parseRemoteItem(raw json.RawMessage, sourceID string) (Item, string) {
	var probe map[string]json.RawMessage
	if err := json.Unmarshal(raw, &probe); err != nil {
		return Item{}, "invalid_item"
	}
	for k := range probe {
		if !remoteItemKeys[k] {
			return Item{}, "unknown_item_field"
		}
	}

	var def itemDef
	if err := json.Unmarshal(raw, &def); err != nil {
		return Item{}, "invalid_item"
	}
	if _, err := ParseKind(string(def.Kind)); err != nil {
		// 类型不在市场四类里：跳过而不是猜。
		return Item{}, "unsupported_kind"
	}
	if msg := validateRemoteDef(def); msg != "" {
		return Item{}, "invalid_item"
	}

	item := Item{
		Kind:        def.Kind,
		ID:          def.ID,
		Name:        def.Name,
		Description: def.Description,
		Version:     def.Version,
		Author:      def.Author,
		Tags:        def.Tags,
		Homepage:    def.Homepage,
		Payload:     def.Payload,
		Requires:    def.Requires,
		Source:      sourceID,
		ManifestURL: def.ManifestURL,
		SHA256:      strings.ToLower(strings.TrimSpace(def.SHA256)),
		Signature:   def.Signature,
		MinEngine:   def.MinEngine,
	}
	if item.ManifestURL == "" {
		// 没有 manifest 就无法安装：外部条目的 payload 只是展示信息，
		// 真正落库的内容必须来自经过校验的 manifest。
		return Item{}, "missing_manifest_url"
	}
	return item, ""
}

// validateRemoteDef 校验一条外部条目，返回空串表示通过。
//
// 与内置条目的差异：**payload 不再是必需的**。外部条目的 payload 只是展示信息，
// 真正落库的内容来自 manifest；反过来 manifest_url 是必需的 —— 没有它就无法安装。
func validateRemoteDef(d itemDef) string {
	if strings.TrimSpace(d.ID) == "" {
		return "id is required"
	}
	if !strings.HasPrefix(d.ID, string(d.Kind)+":") {
		return fmt.Sprintf("id %q must be prefixed with %q", d.ID, string(d.Kind)+":")
	}
	if strings.TrimSpace(d.Name) == "" {
		return "name is required"
	}
	manifest := strings.TrimSpace(d.ManifestURL)
	if manifest == "" {
		return "manifest_url is required"
	}
	// manifest 也只能走 https：安装时网关会去抓它，明文 HTTP 让完整性校验失去意义
	// （中间人可以直接改写内容并同时改写 sha256）。
	if !strings.HasPrefix(strings.ToLower(manifest), "https://") {
		return "manifest_url must use https"
	}
	if len(d.Payload) > 0 && string(d.Payload) != "null" {
		var probe map[string]any
		if err := json.Unmarshal(d.Payload, &probe); err != nil {
			return "payload must be a JSON object"
		}
	}
	if h := strings.TrimSpace(d.SHA256); h != "" {
		if len(h) != 64 {
			return "sha256 must be 64 hex characters"
		}
		if _, err := hex.DecodeString(h); err != nil {
			return "sha256 must be hex"
		}
	}
	if sig := d.Signature; sig != nil {
		if strings.TrimSpace(sig.KeyID) == "" || strings.TrimSpace(sig.Value) == "" {
			return "signature requires key_id and value"
		}
	}
	return ""
}

// SourceIDFromURL 由源 URL 生成稳定 id（前缀 + 指纹）。
//
// 不能用 host 当 id：同一域下可以挂多个市场（`/a` 与 `/b` 是不同源）。
// 也不能用完整 URL：id 会出现在路由与安装记录里，过长且带特殊字符。
func SourceIDFromURL(url string) string {
	sum := sha256.Sum256([]byte(strings.TrimSpace(url)))
	return "src_" + hex.EncodeToString(sum[:8])
}

// SourceNameFromURL 生成源的默认显示名（host + 路径尾部）。
//
// 只取路径最后一段而不是完整路径：Tab 上要能一眼分辨，`https://x.com/a/b/catalog.json`
// 显示成 `x.com/catalog.json` 比整串更可读。运维配置里的自定义名字优先于它。
func SourceNameFromURL(rawURL string) string {
	trimmed := strings.TrimSpace(rawURL)
	u, err := url.Parse(trimmed)
	if err != nil || u.Host == "" {
		return trimmed
	}
	path := strings.TrimSuffix(u.Path, "/")
	name := u.Host
	if idx := strings.LastIndex(path, "/"); idx >= 0 && idx+1 < len(path) {
		last := path[idx+1:]
		last = strings.TrimSuffix(last, ".json")
		if last != "" {
			name = u.Host + "/" + last
		}
	}
	return name
}
