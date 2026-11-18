package market

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// 本文件回答三个产品问题，与 service.go 的「抓取编排」分开：
//
//  1. 源从哪来：内置（随包）/ 运维（后台配置）/ 用户（自助添加）；
//  2. 能加几个：硬上限 20，**不可调大**；
//  3. 怎么落盘：用户源按 user 维度存 JSON 文件，损坏时不能拖垮整个市场页。
//
// 之所以把这些做成显式的 Config 结构而不是散落的 env 读取：外部源必须能在
// **后台**配置（初装默认关闭），而 env 只在没有 DB 覆盖时兜底。

const (
	// DefaultMaxSources 是源总数的默认值，同时是**硬上限**。
	//
	// 为什么硬：每个外部源都是一条可以拖慢市场页的抓取链路（4 个并发位、
	// TTL 缓存、每源限流）。上限可以往小配，但绝不接受往大配 —— 否则一个
	// 误配的 max_sources=1000 就能把页面变成"永远在加载"。
	DefaultMaxSources = 20

	// maxSourceURLBytes 限制单个源 URL 的长度。
	//
	// 目的是形状校验而不是安全边界：正常的一个市场目录 URL 不会超过几百字节，
	// 超长输入只可能是误粘贴或探测。真正的 SSRF 拦截在出网口（Python 的
	// fetch_url_safe），这里不做 IP/端口/重定向判断 —— 两套实现必然漂移。
	maxSourceURLBytes = 2048

	// maxSourceNameRunes 限制源显示名的字符数（Tab 上要放得下）。
	maxSourceNameRunes = 64

	// sourceFileName 是用户源在用户目录下的文件名。
	sourceFileName = "market_sources.json"
)

// Config 是源管理的运行期配置。
//
// 与 `config.Config`（env）的区别：这一份可以被后台热更新（`Configure`），
// env 只在 DB 无覆盖时提供初值。
type Config struct {
	// RemoteEnabled 是外部源总开关。**默认 false** —— 初装只吃内置目录。
	//
	// 打开它意味着本机开始访问第三方 URL，这是部署方/用户必须显式做出的决定，
	// 不能是安装完就默认生效的能力。
	RemoteEnabled bool
	// OperatorSources 是运维配置的源 URL（`catalog_sources`，后台可改）。
	OperatorSources []string
	// AllowUserSources 控制用户能否自助加源。
	//
	// 默认 false：用户级源会把 SSRF 面变成**自助入口**（任何登录用户都能让
	// 本机去抓任意 https 地址）。即使出网口有校验，也不该默认开放。
	AllowUserSources bool
	// AllowRemoteMCP 控制外部源里的 mcp 类条目能否安装（默认 false）。
	AllowRemoteMCP bool
	// MaxSources 是源总数上限，>DefaultMaxSources 一律截断。
	MaxSources int
	// TrustedKeys 是 key_id → base64 Ed25519 公钥。为空表示不验签
	// （条目一律标"未签名"并提醒，但**仍可安装**）。
	TrustedKeys map[string]string
}

// MaxSourcesNormalized 返回规整后的上限（<=0 回落默认，>硬上限截到硬上限）。
func (c Config) MaxSourcesNormalized() int {
	switch {
	case c.MaxSources <= 0:
		return DefaultMaxSources
	case c.MaxSources > DefaultMaxSources:
		return DefaultMaxSources
	}
	return c.MaxSources
}

// Normalized 返回一份规整后的配置副本（调用方持有原值不受影响）。
//
// 只有一处"不按输入的意愿办事"：MaxSources > 20 会被截到 20。这是刻意的 ——
// 上限写死 20 才有意义，否则后台填多大就是多大，等于没有上限。
func (c Config) Normalized() Config {
	out := c
	out.MaxSources = c.MaxSourcesNormalized()
	// 切片/map 深拷贝：配置会跨 goroutine 共享（热更新 + 每次请求读取），
	// 共享底层数组会让"改配置"与"读配置"在同一个 map 上并发。
	out.OperatorSources = append([]string(nil), c.OperatorSources...)
	if c.TrustedKeys != nil {
		out.TrustedKeys = make(map[string]string, len(c.TrustedKeys))
		for k, v := range c.TrustedKeys {
			out.TrustedKeys[k] = v
		}
	}
	return out
}

// OperatorSourceList 把 OperatorSources 转成规整的源列表。
//
// 三个必须的处理（缺一个都会让"配置"与"页面"对不上）：
//   - **形状校验并规整**：非法 URL 直接丢弃并记日志。静默接收会让市场页出现
//     一个永远抓不到东西的 Tab，而运维以为配置成功了；
//   - **去重**：同一 URL 配两次会产生两个 Tab 显示同一批内容；
//   - **截断到上限**：MaxSources 是硬约束，超出的部分丢弃并告警。
func (c Config) OperatorSourceList() []Source {
	cfg := c.Normalized()
	out := make([]Source, 0, len(cfg.OperatorSources))
	seen := make(map[string]bool, len(cfg.OperatorSources))
	for _, raw := range cfg.OperatorSources {
		norm, err := ValidateSourceURL(raw)
		if err != nil {
			slog.Warn("market: skip invalid operator source", "url", raw, "reason", err.Error())
			continue
		}
		if seen[norm] {
			continue
		}
		if len(out) >= cfg.MaxSources {
			slog.Warn("market: operator source limit reached, ignoring the rest",
				"max", cfg.MaxSources, "ignored", norm)
			break
		}
		seen[norm] = true
		out = append(out, Source{
			ID:   SourceIDFromURL(norm),
			Name: SourceNameFromURL(norm),
			URL:  norm,
			Type: SourceTypeOperator,
			// Enabled 表达"该源是否参与拉取"。总开关是 RemoteEnabled，由
			// Service.LoadFor 统一裁决；这里如实标 true 表示"配置上是启用的"。
			Enabled: true,
		})
	}
	return out
}

// ValidateSourceURL 只做**形状**校验，返回规整后的 URL。
//
// 明确**不做**的事：IP / 端口 / 重定向 / DNS 校验。那些属于 SSRF 策略，
// 只在出网口（Python 的 `fetch_url_safe`）实现一处 —— 在 Go 里再写一份，
// 绕过只需挑更宽松的那一边。
//
// 错误码：`invalid_source_url`（形状不对）/ `source_url_not_https`（scheme 不对）。
func ValidateSourceURL(raw string) (string, error) {
	trimmed := strings.TrimSpace(raw)
	if trimmed == "" {
		return "", NewSourceError("invalid_source_url", "url is required")
	}
	if len(trimmed) > maxSourceURLBytes {
		return "", NewSourceError("invalid_source_url",
			fmt.Sprintf("url exceeds %d bytes", maxSourceURLBytes))
	}
	u, err := url.Parse(trimmed)
	if err != nil {
		return "", NewSourceError("invalid_source_url", err.Error())
	}
	// 强制 https：明文 HTTP 下 manifest 的 sha256/签名都可被中间人同时改写，
	// "完整性校验"会变成一句空话。
	if !strings.EqualFold(u.Scheme, "https") {
		return "", NewSourceError("source_url_not_https",
			fmt.Sprintf("scheme must be https, got %q", u.Scheme))
	}
	if u.Host == "" {
		return "", NewSourceError("invalid_source_url", "url has no host")
	}
	// 凭据（user:pass@）不允许出现在源 URL 里：它会随日志/错误/快照泄露，
	// 而且这些源本身是公开目录，不需要认证。
	if u.User != nil {
		return "", NewSourceError("invalid_source_url", "url must not embed credentials")
	}
	// Fragment 对抓取无意义，且会影响 id 指纹（同一 URL 带不同锚点会变成两个源）。
	u.Fragment = ""
	return u.String(), nil
}

// SourceStore 是用户自建源的持久化接口。
//
// 抽成接口的理由：HTTP 层与聚合层都不该知道"源存在 JSON 文件里"。换存储
// （DB / 远端）时只替换实现。
type SourceStore interface {
	// List 返回该用户的全部源。无数据时返回 (nil, nil)。
	List(ctx context.Context, userID string) ([]Source, error)
	// Put 新增或覆盖（按 Source.ID）一条源。
	Put(ctx context.Context, userID string, src Source) error
	// Delete 删除一条源，**幂等**（不存在不算错误）。
	Delete(ctx context.Context, userID string, id string) error
}

// ErrUserSourcesDisabled 表示当前配置不允许用户自助加源（HTTP 403）。
var ErrUserSourcesDisabled = errors.New("user sources are disabled")

// ErrTooManySources 表示源总数已达上限（HTTP 409）。
var ErrTooManySources = errors.New("market source limit reached")

// ErrSourceStoreUnavailable 表示没有配置用户源存储（HTTP 500）。
var ErrSourceStoreUnavailable = errors.New("market source store unavailable")

// ErrSourceNotRemovable 表示该源不是用户源（内置/运维源不可删除，HTTP 403）。
var ErrSourceNotRemovable = errors.New("only user sources can be removed")

// FileSourceStore 把用户源存到 `{dir}/{user_id}/market_sources.json`。
//
// 为什么按用户分文件而不是一张表：桌面版可能没有 DB（本地模式），源列表必须
// 在无 DB 时也可用，且它与插件配置同级同构（`{PluginDataDir}/{user_id}/plugins.json`）。
type FileSourceStore struct {
	dir string
	// mu 串行化「读-改-写」。Put/Delete 都是先读整份文件再写回，
	// 同一用户的并发写会丢更新；锁粒度取整个 store 足够（写入极低频）。
	mu sync.Mutex
}

// NewFileSourceStore 构造文件存储。dir 为空回落 `data/market`（与其它默认目录一致）。
func NewFileSourceStore(dir string) *FileSourceStore {
	if strings.TrimSpace(dir) == "" {
		dir = filepath.Join("data", "market")
	}
	return &FileSourceStore{dir: dir}
}

// sourceRecord 是磁盘上的一条记录（不含 Enabled/Count 等运行期字段 ——
// 那些由配置与抓取结果决定，落盘只会读到过期值）。
type sourceRecord struct {
	ID      string `json:"id"`
	Name    string `json:"name"`
	URL     string `json:"url"`
	AddedAt string `json:"added_at,omitempty"`
}

// sourceFile 是 market_sources.json 的结构。
type sourceFile struct {
	Sources []sourceRecord `json:"sources"`
}

// safeUserID 清理 userID 防止路径穿越（`../tenant/evil`）。
//
// 与 `internal/api/plugin_handler.go` 的 userPluginPath 同策略：只取 Base，
// 且空/`.`/`..` 一律归为 unknown —— 绝不让 userID 决定目录上溯。
func safeUserID(userID string) string {
	safe := filepath.Base(filepath.Clean(userID))
	if safe == "." || safe == "" || safe == ".." || safe == string(filepath.Separator) {
		return "unknown"
	}
	return safe
}

// path 返回该用户源文件路径。
func (s *FileSourceStore) path(userID string) string {
	return filepath.Join(s.dir, safeUserID(userID), sourceFileName)
}

// readFile 读取并解析用户源文件。
//
// 文件不存在 → (空, nil)；**文件损坏 → (空, nil) + 告警**。
// 后者是关键取舍：一个坏 JSON 不能让市场页整体 500（内置目录永远该可用），
// 也不能让用户无法通过"再加一条"把文件覆盖修好。
func (s *FileSourceStore) readFile(userID string) sourceFile {
	var sf sourceFile
	data, err := os.ReadFile(s.path(userID))
	if err != nil {
		if !errors.Is(err, os.ErrNotExist) {
			slog.Warn("market: read user sources failed", "user", safeUserID(userID), "error", err)
		}
		return sf
	}
	if err := json.Unmarshal(data, &sf); err != nil {
		slog.Warn("market: user sources file is corrupt, ignoring it",
			"user", safeUserID(userID), "error", err)
		return sourceFile{}
	}
	return sf
}

// writeFile 原子写用户源文件（临时文件 + Rename，权限 0600）。
//
// 为什么必须原子：页面在保存后立刻刷新，半截 JSON 会让列表直接变空 ——
// 用户看到的是"我刚加的两个源都不见了"。
func (s *FileSourceStore) writeFile(userID string, sf sourceFile) error {
	if sf.Sources == nil {
		sf.Sources = []sourceRecord{}
	}
	data, err := json.MarshalIndent(sf, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal market sources: %w", err)
	}
	path := s.path(userID)
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return fmt.Errorf("mkdir market sources dir: %w", err)
	}
	tmp, err := os.CreateTemp(dir, sourceFileName+".tmp*")
	if err != nil {
		return fmt.Errorf("create temp market sources: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		// Rename 成功后这个 Remove 是幂等的 no-op；失败路径上它负责清理残片。
		_ = os.Remove(tmpName)
	}()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("write temp market sources: %w", err)
	}
	if err := tmp.Chmod(0o600); err != nil {
		// Windows 上 Chmod 语义有限，失败不致命（目录权限已收紧到 0700）。
		slog.Debug("market: chmod user sources failed", "error", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp market sources: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("commit market sources: %w", err)
	}
	return nil
}

// List 实现 SourceStore。
func (s *FileSourceStore) List(_ context.Context, userID string) ([]Source, error) {
	s.mu.Lock()
	defer s.mu.Unlock()

	sf := s.readFile(userID)
	out := make([]Source, 0, len(sf.Sources))
	for _, rec := range sf.Sources {
		if strings.TrimSpace(rec.URL) == "" || strings.TrimSpace(rec.ID) == "" {
			continue
		}
		name := rec.Name
		if strings.TrimSpace(name) == "" {
			name = SourceNameFromURL(rec.URL)
		}
		out = append(out, Source{
			ID:      rec.ID,
			Name:    name,
			URL:     rec.URL,
			Type:    SourceTypeUser,
			Enabled: true,
		})
	}
	return out, nil
}

// Put 实现 SourceStore：按 id 覆盖，不产生重复。
func (s *FileSourceStore) Put(_ context.Context, userID string, src Source) error {
	if strings.TrimSpace(src.ID) == "" || strings.TrimSpace(src.URL) == "" {
		return fmt.Errorf("market source requires id and url")
	}
	s.mu.Lock()
	defer s.mu.Unlock()

	sf := s.readFile(userID)
	rec := sourceRecord{
		ID:      src.ID,
		Name:    src.Name,
		URL:     src.URL,
		AddedAt: time.Now().UTC().Format(time.RFC3339),
	}
	replaced := false
	for i := range sf.Sources {
		if sf.Sources[i].ID == src.ID {
			// 覆盖时保留首次添加时间：它回答的是"这个源我什么时候开始用的"，
			// 而不是"最后一次改名字的时间"。
			if sf.Sources[i].AddedAt != "" {
				rec.AddedAt = sf.Sources[i].AddedAt
			}
			sf.Sources[i] = rec
			replaced = true
			break
		}
	}
	if !replaced {
		sf.Sources = append(sf.Sources, rec)
	}
	return s.writeFile(userID, sf)
}

// Delete 实现 SourceStore：幂等（删不存在的 id 也成功）。
func (s *FileSourceStore) Delete(_ context.Context, userID string, id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	sf := s.readFile(userID)
	kept := make([]sourceRecord, 0, len(sf.Sources))
	removed := false
	for _, rec := range sf.Sources {
		if rec.ID == id {
			removed = true
			continue
		}
		kept = append(kept, rec)
	}
	if !removed {
		// 幂等：没有该 id 就不用写盘（省一次 IO，也避免把损坏文件"顺手修好"
		// 掩盖了损坏这个事实）。
		return nil
	}
	sf.Sources = kept
	return s.writeFile(userID, sf)
}

// truncateRunes 按字符（不是字节）截断，避免把多字节名字切坏。
func truncateRunes(s string, max int) string {
	if max <= 0 || utf8.RuneCountInString(s) <= max {
		return s
	}
	runes := []rune(s)
	return string(runes[:max])
}
