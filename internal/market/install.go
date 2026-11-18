package market

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"time"
)

// 安装编排（与 HTTP 无关）。
//
// 分层理由：`market_handler.go` 只做目录读取，本文件只做「装/卸/查已装」的状态机。
// 真正的落点（写 agents 表、写 plugins.json、代理 Python 装技能/建知识库）由 api 层
// 实现 `Installer` 注入 —— 这里不认识 pgx、不认识 http、也不认识 Python。
//
// **为什么安装需要一张表（market_installations）而不是"按名字找"**：
// 装完之后名字是**用户可改**的（agent 重命名、插件改名、知识库重名），任何"按名字
// 判断装没装"的实现都会在用户改过一次名之后失效；而市场页的「已安装」徽标、以及
// 「重复安装返回已安装而不是报错」的幂等语义，都必须有一个稳定的锚点。
// `(tenant_id, user_id, source, kind, market_id)` 就是那个锚点 —— 它同时表达
// 「谁装的」与「装的是哪个源里的哪一条」，与目录条目的主键 `(source, id)` 对齐。

// InstalledRecord 是一条「本机已安装」记录。
//
// Ref 是**落点引用**（agent_id / plugin name / skill name / kb_id 之类），
// 由 Installer 决定形状：编排层只负责原样存取，卸载时再交还给 Installer。
// 这样换一种落点实现不需要改表结构。
type InstalledRecord struct {
	Source   string         `json:"source"`
	Kind     Kind           `json:"kind"`
	MarketID string         `json:"id"`
	Ref      map[string]any `json:"ref"`
	// Enabled 是**装完之后的启用状态**：内置源条目装完即用（true），
	// 外部源条目默认禁用（false，需用户显式启用/授权）。
	Enabled   bool      `json:"enabled"`
	CreatedAt time.Time `json:"installed_at"`
}

// InstallStore 是已安装记录的持久化端口。
//
// 只有四个方法，且**都在 tenant/user 维度内**：安装记录是用户级事实，
// 同租户的另一个用户装了同一条目与他无关（各自有自己的一份落点）。
type InstallStore interface {
	List(ctx context.Context, tenantID, userID string, kind Kind) ([]InstalledRecord, error)
	// Get 用 rec 的 (Source, Kind, MarketID) 作键；未命中返回 (零值, false, nil)。
	Get(ctx context.Context, tenantID, userID string, rec InstalledRecord) (InstalledRecord, bool, error)
	// Upsert 幂等写入：唯一键是 (tenant, user, source, kind, market_id)。
	Upsert(ctx context.Context, tenantID, userID string, rec InstalledRecord) error
	// Delete 删除一条记录；不存在**不是**错误（幂等卸载的落点）。
	Delete(ctx context.Context, tenantID, userID string, source string, kind Kind, marketID string) error
}

// Installer 是 per-kind 的落点实现（写 agents 表 / 写 plugins.json / 代理 Python）。
//
// 拆成接口而不是在编排层写 switch：四类的落点差异极大（本地 DB、本地文件、远程
// Python），把它们塞进一个函数会让每一类的错误语义互相污染；分开之后
// 「某一类上游能力缺失」就能精确地报 501，而不是伪造成功。
type Installer interface {
	// Install 落地一个条目并返回落点引用（卸载时要还给它）。
	Install(ctx context.Context, item Item, env map[string]string) (ref map[string]any, err error)
	// Uninstall 撤销一个落点。item 在目录里仍能找到时是**真实条目**，
	// 找不到时只有 (Kind, ID, Source) 三个字段可信 —— 实现必须容忍这一点
	// （卸载只能依赖 ref，不能依赖 payload）。
	Uninstall(ctx context.Context, item Item, ref map[string]any) error
}

// InstallOptions 是安装策略开关（来自环境变量，见 config.Config 的 MARKET_* ）。
type InstallOptions struct {
	// AllowRemoteMCP 决定**外部源**的 mcp 条目能否安装（MARKET_ALLOW_REMOTE_MCP）。
	// 默认 false：外部 MCP 意味着让一个来路不明的命令在本机执行，这必须是显式决定。
	AllowRemoteMCP bool
}

// 安装链路上的稳定错误。HTTP 层按 errors.Is 映射状态码。
var (
	// ErrRemoteMCPForbidden 表示外部源的 mcp 条目被策略禁止安装。
	// 与「装失败」区分开：它不是故障，是**默认禁止**的产品决策。
	ErrRemoteMCPForbidden = errors.New(
		"external mcp install is disabled: set MARKET_ALLOW_REMOTE_MCP=true to allow it")
	// ErrInstallerUnavailable 表示该 kind 没有注入落点实现（网关侧缺能力）。
	ErrInstallerUnavailable = errors.New("no installer registered for kind")
	// ErrBadRequest 是一类「调用方参数问题」的包装，便于 HTTP 层统一映射 400。
	ErrBadRequest = errors.New("invalid install request")
)

// Result 是一次安装的结果。字段与前端契约一一对应（见 market_install.go 的响应体）。
type Result struct {
	Source           string         `json:"source"`
	Kind             Kind           `json:"kind"`
	ID               string         `json:"id"`
	Ref              map[string]any `json:"ref"`
	Enabled          bool           `json:"enabled"`
	RequiresApproval bool           `json:"requires_approval"`
	// AlreadyInstalled 表示这次**没有**重新落地（幂等命中）。前端据此显示
	// 「已安装」而不是再弹一次成功提示。
	AlreadyInstalled bool `json:"already_installed,omitempty"`
}

// InstallService 编排安装/卸载/列表。
type InstallService struct {
	store      InstallStore
	installers map[Kind]Installer
	opts       InstallOptions
}

// NewInstallService 构造编排服务。
//
// installers 缺某个 kind 时不报错：那是部署形态（例如未接引擎时没有 skill 落点），
// 真正安装该 kind 时返回 501 语义错误，让前端能说清"这个类型当前装不了"。
func NewInstallService(store InstallStore, installers map[Kind]Installer, opts InstallOptions) *InstallService {
	m := make(map[Kind]Installer, len(installers))
	for k, v := range installers {
		if v != nil {
			m[k] = v
		}
	}
	return &InstallService{store: store, installers: m, opts: opts}
}

// requiresApproval 判定「装完之后是否仍需用户授权才能真正执行」。
//
// 两条来源：
//   - 条目自己声明的 `requires.approval`（如内置 mcp 条目：装完要在插件页授权）；
//   - **外部 mcp 强制为 true** —— 这是产品决策，不是条目可以自己关闭的：
//     外部源的命令执行必须经过用户逐条确认。
func requiresApproval(item Item, external bool) bool {
	if item.Kind == KindMCP && external {
		return true
	}
	return item.Requires.Approval
}

// installKeyError 把条目自身字段的校验失败统一包成 ErrBadRequest（→ 400）。
func installKeyError(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrBadRequest, fmt.Sprintf(format, args...))
}

// Install 安装一个条目（幂等：已装则返回已有状态，不重复落地）。
func (s *InstallService) Install(ctx context.Context, tenantID, userID string, item Item, env map[string]string) (Result, error) {
	kind, err := ParseKind(string(item.Kind))
	if err != nil {
		return Result{}, installKeyError("%v", err)
	}
	if item.Source == "" {
		return Result{}, installKeyError("source is required")
	}
	if item.ID == "" {
		return Result{}, installKeyError("id is required")
	}

	external := item.Source != SourceBuiltin
	// 策略闸在**任何落点动作之前**：外部 mcp 默认禁止，不能先写了再回滚。
	if kind == KindMCP && external && !s.opts.AllowRemoteMCP {
		return Result{}, ErrRemoteMCPForbidden
	}
	inst, ok := s.installers[kind]
	if !ok {
		return Result{}, fmt.Errorf("%w: %s", ErrInstallerUnavailable, kind)
	}
	if s.store == nil {
		return Result{}, errors.New("install store unavailable")
	}

	probe := InstalledRecord{Source: item.Source, Kind: kind, MarketID: item.ID}
	existing, found, err := s.store.Get(ctx, tenantID, userID, probe)
	if err != nil {
		return Result{}, err
	}
	if found {
		// 幂等命中：不重新落地（重复安装同一条目不该产生第二份 agent/插件/知识库）。
		return Result{
			Source:           existing.Source,
			Kind:             existing.Kind,
			ID:               existing.MarketID,
			Ref:              nonNilRef(existing.Ref),
			Enabled:          existing.Enabled,
			RequiresApproval: requiresApproval(item, external),
			AlreadyInstalled: true,
		}, nil
	}

	ref, err := inst.Install(ctx, item, env)
	if err != nil {
		return Result{}, err
	}

	rec := InstalledRecord{
		Source:   item.Source,
		Kind:     kind,
		MarketID: item.ID,
		Ref:      nonNilRef(ref),
		// **内置源装完即用，外部源装完默认禁用**：外部内容没经过二进制同源发布，
		// 装好不等于获得启用；用户必须显式启用（mcp 还要逐条授权）。
		Enabled:   !external,
		CreatedAt: time.Now().UTC(),
	}
	if err := s.store.Upsert(ctx, tenantID, userID, rec); err != nil {
		// 记录写不进去时**不回滚落点**：落点已经生效，回滚反而可能删掉用户既有数据。
		// 如实报错，让调用方看到"装了但没记住"——重试会走 Install 的幂等分支。
		return Result{}, err
	}

	return Result{
		Source:           rec.Source,
		Kind:             rec.Kind,
		ID:               rec.MarketID,
		Ref:              rec.Ref,
		Enabled:          rec.Enabled,
		RequiresApproval: requiresApproval(item, external),
	}, nil
}

// Uninstall 卸载一个条目（幂等：不存在的条目也返回成功）。
//
// item 可以为 nil：外部源下线后目录里已经没有该条目，但用户仍有权卸载它。
// 此时只有记录里的 Ref 可用 —— 这也是 Installer.Uninstall 只依赖 ref 的原因。
func (s *InstallService) Uninstall(ctx context.Context, tenantID, userID, source string, kind Kind, id string, item *Item) error {
	k, err := ParseKind(string(kind))
	if err != nil {
		return installKeyError("%v", err)
	}
	if source == "" || id == "" {
		return installKeyError("source and id are required")
	}
	if s.store == nil {
		return errors.New("install store unavailable")
	}

	probe := InstalledRecord{Source: source, Kind: k, MarketID: id}
	existing, found, err := s.store.Get(ctx, tenantID, userID, probe)
	if err != nil {
		return err
	}
	if !found {
		// 幂等：没有记录说明"未安装"这个目标已经达成。仍然尝试 Delete 一次，
		// 清掉可能存在的半写状态（例如上次记录写失败但落点已成功）。
		return s.store.Delete(ctx, tenantID, userID, source, k, id)
	}

	upstream := Item{Kind: k, ID: id, Source: source}
	if item != nil {
		upstream = *item
	} else {
		upstream.Name = refString(existing.Ref, "name")
	}
	if inst, ok := s.installers[k]; ok {
		if err := inst.Uninstall(ctx, upstream, nonNilRef(existing.Ref)); err != nil {
			// 上游卸载失败时**保留记录**：否则记录没了、落点还在，用户既看不到也无法再卸。
			return err
		}
	} else {
		// 没有落点实现（能力缺失）时只能清记录，并留一条可追溯的日志：
		// 静默成功会让"记录没了但东西还在"变成无从排查的现象。
		slog.Warn("market uninstall: no installer for kind, dropping record only",
			"kind", string(k), "source", source, "id", id)
	}
	return s.store.Delete(ctx, tenantID, userID, source, k, id)
}

// List 返回当前用户已安装的记录（kind 为空表示全部）。
func (s *InstallService) List(ctx context.Context, tenantID, userID string, kind Kind) ([]InstalledRecord, error) {
	if kind != "" {
		k, err := ParseKind(string(kind))
		if err != nil {
			return nil, installKeyError("%v", err)
		}
		kind = k
	}
	if s.store == nil {
		return nil, errors.New("install store unavailable")
	}
	return s.store.List(ctx, tenantID, userID, kind)
}

// nonNilRef 保证 ref 序列化成 `{}` 而不是 `null`：前端一律按对象读，null 会炸。
func nonNilRef(ref map[string]any) map[string]any {
	if ref == nil {
		return map[string]any{}
	}
	return ref
}

// refString 从落点引用里取一个字符串字段（卸载时目录里已找不到条目的兜底）。
func refString(ref map[string]any, key string) string {
	if ref == nil {
		return ""
	}
	if v, ok := ref[key].(string); ok {
		return v
	}
	return ""
}
