package market

import (
	"context"
	"errors"
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
	// fetcher 为空表示未启用外部源（MARKET_REMOTE_ENABLED=0，桌面版默认）。
	fetcher Fetcher

	mu sync.RWMutex
	// externals 是运维/用户配置的外部源（由 SetExternals 热更新）。
	externals []Source
}

// NewService 构造市场聚合服务。fetcher 可为 nil（只吃内置目录）。
func NewService(fetcher Fetcher) *Service {
	return &Service{fetcher: fetcher}
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

// Snapshot 是一次目录读取的完整结果。
type Snapshot struct {
	Sources  []Source
	Items    []Item
	Warnings []Warning
}

// Load 读取全部来源的目录。
//
// 顺序固定：内置在前、外部在后（页面默认落在内置 Tab）。外部源并发抓取但
// 结果按配置顺序拼回，保证分页稳定。
func (s *Service) Load(ctx context.Context) Snapshot {
	builtin := BuiltinItems()
	out := Snapshot{
		Sources: []Source{{
			ID:      SourceBuiltin,
			Name:    "builtin",
			Type:    SourceTypeBuiltin,
			Enabled: true,
			Count:   len(builtin),
		}},
		Items: builtin,
	}

	externals := s.Externals()
	if len(externals) == 0 {
		return out
	}
	if s.fetcher == nil {
		// 配了外部源但抓取层未启用：如实告知，而不是静默当成"没有外部源"。
		for _, src := range externals {
			src.Count = 0
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
