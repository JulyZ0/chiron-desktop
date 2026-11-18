package market

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sort"
	"strings"
	"sync"
)

// SourceType 是源的类别。三类的信任级别不同，前端据此外显。
type SourceType string

const (
	// SourceTypeBuiltin 随二进制发布，永远可用、不需要联网。
	SourceTypeBuiltin SourceType = "builtin"
	// SourceTypeOperator 由运维通过环境变量配置（部署方白名单）。
	SourceTypeOperator SourceType = "operator"
	// SourceTypeUser 由用户在设置页自行添加。
	//
	// 这类源把 SSRF 面变成**自助入口**，因此默认不开放（见 MARKET_ALLOW_USER_SOURCES）：
	// 即使有解析级校验，也不值得让"任意 URL 可被本机抓取"成为默认能力。
	SourceTypeUser SourceType = "user"
)

// Source 是市场页上的一个来源分组。
//
// 市场页按源分 Tab（而不是把多源条目混排）：同名条目在不同源是**不同东西**，
// 混排后用户无法判断自己装的是哪一个。
type Source struct {
	ID   string     `json:"id"`
	Name string     `json:"name"`
	URL  string     `json:"url,omitempty"`
	Type SourceType `json:"type"`
	// Enabled 为 false 表示该源存在但当前不参与拉取（外部源默认关闭）。
	Enabled bool `json:"enabled"`
	// LastOKAt / LastError 是**可观测性**字段：源坏掉时页面要能说清是哪一个、
	// 上次成功是什么时候，而不是只剩一个空 Tab。
	LastOKAt  string `json:"last_ok_at,omitempty"`
	LastError string `json:"last_error,omitempty"`
	// Stale 表示当前条目来自上次成功快照（本次抓取失败）。
	Stale bool `json:"stale,omitempty"`
	// Count 是该源参与展示的条目数（内置源恒为目录大小）。
	Count int `json:"count"`
}

// Warning 是可以和结果一起返回的**非致命**问题。
//
// 关键约束：单个源失败不能让整个目录失败。它进 warnings，页面照常渲染其它源 ——
// 只有当用户**安装**某条来自坏源的条目时才返回错误。
type Warning struct {
	Source string `json:"source"`
	// Reason 是稳定标识（source_unreachable / source_invalid / source_timeout …），
	// 前端据此本地化；不要把英文异常串直接塞给用户。
	Reason string `json:"reason"`
	// Detail 是给运维看的原始信息（可能含 URL / 状态码）。
	Detail string `json:"detail,omitempty"`
}

// FetchResult 是一次外部源抓取的结果。
//
// 为什么要把它做成结构体而不是 `([]Item, error)`：抓取失败时**内容可能仍然可用**
// ——上次成功快照标 Stale 后照常返回。用 error 表达"降级成功"会让调用方只能二选一。
type FetchResult struct {
	Items []Item
	// Stale 表示 Items 来自上次成功快照（本次抓取失败）。
	Stale bool
	// StaleReason 是导致降级的稳定原因码（Stale 为 true 时才有值）。
	StaleReason string
	// Warnings 是本源的可观测性问题（部分条目被丢弃、未签名、条目被截断…）。
	Warnings []Warning

	// err 只在**完全不可用**（无快照且抓取失败）时非 nil。
	err error
}

// Fetcher 抓取一个外部源的目录。由网关侧实现（Go 编排 Python 的 internal 抓取端点），
// 抽成接口是为了让聚合逻辑可以脱离网络单测。
type Fetcher interface {
	// Fetch 返回该源当前的条目。实现必须已经做过传输/体积/结构校验。
	// 降级（返回旧快照）通过 FetchResult.Stale 表达，而不是返回 error。
	Fetch(ctx context.Context, src Source) (FetchResult, error)
}

// Service 聚合内置目录与外部源。
//
// 内置目录永远可用（随包发布），因此 Service 失败的上限是"少几个外部源"，
// 绝不会让市场页变空。
type Service struct {
	// fetcher 为空表示抓取层未注入（外部源即使配置了也不参与拉取）。
	fetcher Fetcher

	mu sync.RWMutex
	// externals 是**已解析好的**外部源列表（由 SetExternals 直接灌入）。
	//
	// 它保留给"调用方已经自己算好源列表"的场景（含单测）。正常路径走
	// Configure + ResolveSources：配置是 URL 字符串，解析与去重在那里做。
	externals []Source

	cfgMu sync.RWMutex
	// config 是源管理配置（后台可热更新，见 Configure）。零值即"初装形态"：
	// 外部源关闭、不允许用户加源、无运维源。
	config Config
	// configured 表示配置已被显式设置（Configure 调用过）。
	//
	// 未设置时总开关按"抓取层是否注入"推断：fetcher != nil 视为启用 ——
	// 这是 Service 的**历史语义**（注入 fetcher 本身就代表调用方要抓外部源），
	// 保留它让既有构造路径与单测的行为不变。生产路径总会 Configure，
	// 因此"初装默认关闭"依然由配置（而非这里）保证。
	configured bool

	// userStore 是用户自建源的持久化层，可为 nil（没有落盘位置）。
	userStore SourceStore
}

// NewService 构造市场聚合服务。fetcher 可为 nil（只吃内置目录）。
func NewService(fetcher Fetcher) *Service {
	return &Service{fetcher: fetcher, config: Config{}.Normalized()}
}

// SetExternals 热更新外部源列表（后台配置改动后调用）。
func (s *Service) SetExternals(srcs []Source) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.externals = append([]Source(nil), srcs...)
}

// Externals 返回当前外部源配置的副本。
func (s *Service) Externals() []Source {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return append([]Source(nil), s.externals...)
}

// Configure 热更新源管理配置（后台保存 market 分组后调用）。
//
// 只影响"源从哪来、能不能加、加几个"，不影响 fetcher：TrustedKeys 之类属于
// 抓取层的参数由装配处（internal/api/market_wiring.go）在构造 fetcher 时读取。
func (s *Service) Configure(cfg Config) {
	s.cfgMu.Lock()
	defer s.cfgMu.Unlock()
	s.config = cfg.Normalized()
	s.configured = true
}

// Config 返回当前配置的副本（已规整、已深拷贝）。
func (s *Service) Config() Config {
	s.cfgMu.RLock()
	defer s.cfgMu.RUnlock()
	if !s.configured {
		// 未显式配置：总开关随抓取层走（见 configured 的注释）。
		return Config{RemoteEnabled: s.fetcher != nil}.Normalized()
	}
	return s.config.Normalized()
}

// SetUserStore 装配用户源存储。传 nil 表示该部署没有用户级源落盘位置
// （此时 add/remove 返回 ErrSourceStoreUnavailable，而不是静默丢数据）。
func (s *Service) SetUserStore(store SourceStore) {
	s.cfgMu.Lock()
	defer s.cfgMu.Unlock()
	s.userStore = store
}

// UserSources 返回该用户自建的源（读失败时返回空并记日志 —— 单个用户的源文件
// 坏掉不能让他的市场页整体失败）。
func (s *Service) UserSources(userID string) []Source {
	store := s.store()
	if store == nil || strings.TrimSpace(userID) == "" {
		return nil
	}
	srcs, err := store.List(context.Background(), userID)
	if err != nil {
		slog.Warn("market: list user sources failed", "user", userID, "error", err)
		return nil
	}
	return srcs
}

func (s *Service) store() SourceStore {
	s.cfgMu.RLock()
	defer s.cfgMu.RUnlock()
	return s.userStore
}

// ResolveSources 返回**全部**源（内置 + 运维 + 用户）用于展示与合并抓取。
//
// 与 LoadFor 的关键区别（两条语义，别混）：
//   - ResolveSources 回答"这个部署/用户有哪些源"——即使外部源总开关是关的，
//     也照样列出来，只是 Enabled=false。管理页要能显示"你配了这些，但没启用"，
//     否则用户永远看不到自己刚加的源。
//   - LoadFor 回答"这一轮真正能拿到哪些内容"——总开关关着时，外部源一条都不抓。
//
// 排序经 sortedSources：内置恒在首位（页面默认落在内置 Tab），同类型内按名字稳定
// 排序，避免两次请求间 Tab 顺序抖动。
func (s *Service) ResolveSources(userID string) []Source {
	cfg := s.Config()
	max := cfg.MaxSourcesNormalized()

	// 内置源不计入 max：它随包发布、不可配置、永远存在。
	out := []Source{{
		ID:      SourceBuiltin,
		Name:    "builtin",
		Type:    SourceTypeBuiltin,
		Enabled: true,
	}}

	externals := make([]Source, 0, len(cfg.OperatorSources)+len(s.Externals())+2)
	seen := make(map[string]bool, len(cfg.OperatorSources)+2)
	add := func(src Source, origin string) {
		key := strings.TrimSpace(src.ID)
		if key == "" {
			key = strings.TrimSpace(src.URL)
		}
		if key == "" {
			return
		}
		if seen[key] {
			return
		}
		if u := strings.TrimSpace(src.URL); u != "" {
			// 同一 URL 经不同路径（运维/用户各自加了一遍）只能有一个 Tab：
			// 两批同名条目分不清谁是谁。
			if seen[u] {
				return
			}
			seen[u] = true
		}
		if len(externals) >= max {
			slog.Warn("market: source limit reached, ignoring the rest",
				"max", max, "ignored", key, "origin", origin)
			return
		}
		seen[key] = true
		externals = append(externals, src)
	}

	// 顺序即优先级：运维源在前（部署方白名单），其后是 SetExternals 直接注入的源，
	// 最后才是用户自建源 —— 截断发生时被丢掉的应该是最"自发"的那一批。
	for _, src := range cfg.OperatorSourceList() {
		add(src, "operator")
	}
	for _, src := range s.Externals() {
		add(src, "externals")
	}
	for _, src := range s.UserSources(userID) {
		add(src, "user")
	}

	if !cfg.RemoteEnabled {
		// 总开关关着：源仍列出（管理页要看到），但标 Enabled=false 表示
		// "存在但不参与拉取"。
		for i := range externals {
			externals[i].Enabled = false
		}
	}
	return sortedSources(append(out, externals...))
}

// AddUserSource 为用户新增一个自建源，返回规整后的源。
//
// 返回的错误是可区分的（HTTP 层据此映射状态码）：
//   - ErrUserSourcesDisabled → 403（用户级源把 SSRF 面变成自助入口，默认不开放）；
//   - ErrTooManySources      → 409（源总数硬上限）；
//   - *SourceError           → 400（URL 形状不合法）。
func (s *Service) AddUserSource(ctx context.Context, userID, rawURL, name string) (Source, error) {
	cfg := s.Config()
	if !cfg.AllowUserSources {
		return Source{}, ErrUserSourcesDisabled
	}
	store := s.store()
	if store == nil {
		return Source{}, ErrSourceStoreUnavailable
	}
	if strings.TrimSpace(userID) == "" {
		// 无身份就没有"用户的源"这个概念：写进 unknown 目录会让所有匿名请求
		// 共享同一份源列表。
		return Source{}, ErrUserSourcesDisabled
	}

	norm, err := ValidateSourceURL(rawURL)
	if err != nil {
		return Source{}, err
	}
	id := SourceIDFromURL(norm)

	// 已有源（运维或用户）：重复添加是幂等操作，不占额度、不产生第二个 Tab。
	// 用 URL 比较而不是只比 id：id 是 URL 的指纹，但外部注入的源可能带尾斜杠差异。
	count := 0
	max := cfg.MaxSourcesNormalized()
	for _, src := range s.ResolveSources(userID) {
		if src.Type == SourceTypeBuiltin {
			continue
		}
		count++
		if src.ID == id || (src.URL != "" && strings.EqualFold(strings.TrimSpace(src.URL), norm)) {
			return src, nil
		}
	}
	if count >= max {
		return Source{}, fmt.Errorf("%w: max %d sources (hard cap %d)",
			ErrTooManySources, max, DefaultMaxSources)
	}

	display := truncateRunes(strings.TrimSpace(name), maxSourceNameRunes)
	if display == "" {
		display = SourceNameFromURL(norm)
	}
	src := Source{
		ID:   id,
		Name: display,
		URL:  norm,
		Type: SourceTypeUser,
		// Enabled 如实反映"这一轮会不会真的抓它"：总开关关着时刚加的源在页面上
		// 也应是"未启用"状态，否则用户会以为它立刻在拉内容。
		Enabled: cfg.RemoteEnabled,
	}
	if err := store.Put(ctx, userID, src); err != nil {
		return Source{}, err
	}
	return src, nil
}

// RemoveUserSource 删除一个用户自建源。
//
// 只有 user 类型可删：内置源是随包发布的目录，运维源是部署方白名单 —— 让页面
// 上的删除按钮能删掉它们，等于给了普通用户改运维配置的能力。
// 幂等：删不存在的 id 也返回 nil（前端重复点击不该报错）。
func (s *Service) RemoveUserSource(ctx context.Context, userID, id string) error {
	store := s.store()
	if store == nil {
		return ErrSourceStoreUnavailable
	}
	if strings.TrimSpace(userID) == "" {
		return ErrSourceStoreUnavailable
	}
	for _, src := range s.UserSources(userID) {
		if src.ID == id {
			return store.Delete(ctx, userID, id)
		}
	}
	// 不是用户源：若它确实存在（内置/运维）则明确拒绝，让前端知道"按钮不该出现在这里"。
	for _, src := range s.ResolveSources(userID) {
		if src.ID == id {
			return fmt.Errorf("%w: %s source", ErrSourceNotRemovable, src.Type)
		}
	}
	return nil
}

// Snapshot 是一次目录读取的完整结果。
type Snapshot struct {
	Sources  []Source
	Items    []Item
	Warnings []Warning
}

// Load 读取全部来源的目录（不区分用户）。
//
// 签名**刻意保持不变**：并行分支仍在调用它，且"内置目录 + 运维源"这个语义
// 对不需要用户身份的调用点依然正确。用户自建源需要身份，走 LoadFor。
func (s *Service) Load(ctx context.Context) Snapshot {
	return s.LoadFor(ctx, "")
}

// LoadFor 读取该用户的完整目录。
//
// 顺序固定：内置在前、外部在后（页面默认落在内置 Tab）。外部源并发抓取但
// 结果按配置顺序拼回，保证分页稳定。
//
// 与 ResolveSources 的语义差别（**两种语义，别合并**）：
//   - 这里要的是"这一轮真的能给出什么内容"，所以 remote_enabled=false 时
//     **一条外部源都不抓**（Items 只有内置目录）；
//   - 但源列表**仍然列出**外部源（Enabled=false + remote_disabled 告警）：
//     静默隐藏会让用户以为"我配的源凭空消失了"，市场页的 Tab 也要能解释
//     为什么那几栏没有内容。
func (s *Service) LoadFor(ctx context.Context, userID string) Snapshot {
	builtin := BuiltinItems()
	out := Snapshot{Items: builtin}

	externals := make([]Source, 0, 4)
	for _, src := range s.ResolveSources(userID) {
		if src.Type == SourceTypeBuiltin {
			src.Count = len(builtin)
			out.Sources = append(out.Sources, src)
			continue
		}
		externals = append(externals, src)
	}
	if len(externals) == 0 {
		return out
	}
	// 抓取层未注入（fetcher == nil）或总开关关着：源如实列出，但一律不抓。
	if s.fetcher == nil || !s.Config().RemoteEnabled {
		for _, src := range externals {
			src.Count = 0
			src.Enabled = false
			out.Sources = append(out.Sources, src)
			out.Warnings = append(out.Warnings, Warning{
				Source: src.ID,
				Reason: "remote_disabled",
			})
		}
		return out
	}

	type result struct {
		idx  int
		src  Source
		res  FetchResult
		err  error
	}
	results := make([]result, len(externals))
	var wg sync.WaitGroup
	for i, src := range externals {
		results[i].idx = i
		results[i].src = src
		if !src.Enabled {
			continue
		}
		wg.Add(1)
		go func(i int, src Source) {
			defer wg.Done()
			res, err := s.fetcher.Fetch(ctx, src)
			results[i].res = res
			results[i].err = err
			if err == nil && res.Stale {
				// 降级返回的是成功结果（有内容），错误只留在 StaleReason 里。
				results[i].err = nil
			}
		}(i, src)
	}
	wg.Wait()

	for _, r := range results {
		src := r.src
		if !src.Enabled {
			src.Count = 0
			out.Sources = append(out.Sources, src)
			continue
		}
		if r.err != nil {
			// 失败降级：源仍在列表里（Enabled=true）但 Count=0 + LastError，
			// 页面能显示"这个源当前不可用"，而不是让 Tab 莫名消失。
			src.Count = 0
			src.LastError = errReason(r.err)
			out.Sources = append(out.Sources, src)
			out.Warnings = append(out.Warnings, Warning{
				Source: src.ID,
				Reason: r.err.Error(),
				Detail: errDetail(r.err),
			})
			continue
		}
		src.Count = len(r.res.Items)
		// Stale 是**源级**事实：本次抓取失败、内容来自旧快照。页面据此显示
		// "内容可能已过期"，而不是把它当成一次正常刷新。
		src.Stale = r.res.Stale
		if r.res.Stale {
			src.LastError = r.res.StaleReason
		}
		out.Sources = append(out.Sources, src)
		out.Items = append(out.Items, r.res.Items...)
		// 源级告警（坏条目被丢弃、未签名、被截断…）必须冒泡到列表响应，
		// 否则"这个源少了几条"就成了无从排查的现象。
		out.Warnings = append(out.Warnings, r.res.Warnings...)
	}

	sortItems(out.Items)
	return out
}

// errReasoner 让抓取错误能区分"稳定原因码"与"细节"。
//
// 分开的理由：**原因码进 UI、细节进日志**。把 `dial tcp 10.0.0.5:443: connect:
// connection refused` 直接渲染给用户既没意义也漏内网拓扑。
type errReasoner interface {
	Reason() string
	Detail() string
}

func errReason(err error) string {
	var r errReasoner
	if errors.As(err, &r) {
		return r.Reason()
	}
	return "source_unreachable"
}

func errDetail(err error) string {
	var r errReasoner
	if errors.As(err, &r) {
		return r.Detail()
	}
	if err == nil {
		return ""
	}
	return err.Error()
}

// SourceError 是带稳定原因码的抓取错误。HTTP 层据此映射状态码，
// 列表场景只让它进 warnings。
type SourceError struct {
	Code       string
	DetailText string
}

func (e *SourceError) Error() string  { return e.Code }
func (e *SourceError) Reason() string { return e.Code }
func (e *SourceError) Detail() string { return e.DetailText }

// NewSourceError 构造一个抓取错误。
func NewSourceError(code, detail string) *SourceError {
	return &SourceError{Code: code, DetailText: detail}
}

// sourceTypeRank 决定源在页面上的分组顺序：内置 → 运维 → 用户。
//
// 依据是**信任级别递减**：内置随包发布、运维是部署方白名单、用户源由使用者
// 自己负责。把排序显式化，避免"用户源混到内置前面"这类误解。
func sourceTypeRank(t SourceType) int {
	switch t {
	case SourceTypeBuiltin:
		return 0
	case SourceTypeOperator:
		return 1
	case SourceTypeUser:
		return 2
	}
	return 3
}

// sortedSources 稳定排序源列表（type → name → id）。
//
// 必须稳定：Tab 顺序在两次请求间抖动会让用户点错源。同类型内按 name 而非 URL
// 排序，因为用户看得到 name、看不到指纹化的 id。
func sortedSources(srcs []Source) []Source {
	out := append([]Source(nil), srcs...)
	sort.SliceStable(out, func(i, j int) bool {
		ri, rj := sourceTypeRank(out[i].Type), sourceTypeRank(out[j].Type)
		if ri != rj {
			return ri < rj
		}
		if out[i].Name != out[j].Name {
			return out[i].Name < out[j].Name
		}
		return out[i].ID < out[j].ID
	})
	return out
}
