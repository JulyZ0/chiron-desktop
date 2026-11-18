package market

import (
	"context"
	"errors"
	"testing"
	"time"
)

// ── 假实现：让编排逻辑脱离 DB / 文件 / 网络 ──

// fakeStore 是进程内 InstallStore。按 (source, kind, market_id) 存，
// 与真实实现的唯一键口径一致。
type fakeStore struct {
	rows    map[string]InstalledRecord
	upserts int
	deletes int
	// failGet 让 Get 返回错误，用于验证错误向上冒泡而不是被吞。
	failGet bool
}

func newFakeStore() *fakeStore { return &fakeStore{rows: map[string]InstalledRecord{}} }

func key(source string, kind Kind, id string) string {
	return source + "|" + string(kind) + "|" + id
}

func (s *fakeStore) List(_ context.Context, _, _ string, kind Kind) ([]InstalledRecord, error) {
	out := make([]InstalledRecord, 0, len(s.rows))
	for _, r := range s.rows {
		if kind != "" && r.Kind != kind {
			continue
		}
		out = append(out, r)
	}
	return out, nil
}

func (s *fakeStore) Get(_ context.Context, _, _ string, rec InstalledRecord) (InstalledRecord, bool, error) {
	if s.failGet {
		return InstalledRecord{}, false, errors.New("store down")
	}
	r, ok := s.rows[key(rec.Source, rec.Kind, rec.MarketID)]
	return r, ok, nil
}

func (s *fakeStore) Upsert(_ context.Context, _, _ string, rec InstalledRecord) error {
	s.upserts++
	s.rows[key(rec.Source, rec.Kind, rec.MarketID)] = rec
	return nil
}

func (s *fakeStore) Delete(_ context.Context, _, _ string, source string, kind Kind, id string) error {
	s.deletes++
	delete(s.rows, key(source, kind, id))
	return nil
}

// fakeInstaller 记录被调用的次数，并可注入失败。
type fakeInstaller struct {
	installs   int
	uninstalls int
	gotEnv     map[string]string
	failWith   error
	unfailWith error
}

func (f *fakeInstaller) Install(_ context.Context, item Item, env map[string]string) (map[string]any, error) {
	if f.failWith != nil {
		return nil, f.failWith
	}
	f.installs++
	f.gotEnv = env
	return map[string]any{"id": "local-" + item.ID, "name": item.Name}, nil
}

func (f *fakeInstaller) Uninstall(_ context.Context, item Item, ref map[string]any) error {
	if f.unfailWith != nil {
		return f.unfailWith
	}
	f.uninstalls++
	return nil
}

// newTestInstallService 构造一个四类都有落点、外部 MCP 默认禁止的服务。
func newTestInstallService(allowRemoteMCP bool) (*InstallService, *fakeStore, map[Kind]*fakeInstaller) {
	store := newFakeStore()
	insts := map[Kind]*fakeInstaller{}
	m := map[Kind]Installer{}
	for _, k := range Kinds {
		f := &fakeInstaller{}
		insts[k] = f
		m[k] = f
	}
	return NewInstallService(store, m, InstallOptions{AllowRemoteMCP: allowRemoteMCP}), store, insts
}

func builtinAgentItem() Item {
	return Item{
		Kind: KindAgent, ID: "agent:code-reviewer", Name: "code-reviewer",
		Source: SourceBuiltin, Payload: []byte(`{"system_prompt":"x"}`),
	}
}

func externalItem(kind Kind, id string) Item {
	return Item{Kind: kind, ID: id, Name: id, Source: "team", Payload: []byte(`{}`)}
}

// ── 幂等 ──

// 重复安装同一条目：第二次必须返回 AlreadyInstalled 且**不重复落地**。
// 否则用户点两次"安装"就会得到两个同名 agent。
func TestInstall_IdempotentRepeat(t *testing.T) {
	svc, store, insts := newTestInstallService(false)
	ctx := context.Background()

	first, err := svc.Install(ctx, "1", "u1", builtinAgentItem(), nil)
	if err != nil {
		t.Fatalf("首次安装失败: %v", err)
	}
	if first.AlreadyInstalled {
		t.Error("首次安装不应标记 already_installed")
	}

	second, err := svc.Install(ctx, "1", "u1", builtinAgentItem(), nil)
	if err != nil {
		t.Fatalf("重复安装应成功（幂等）: %v", err)
	}
	if !second.AlreadyInstalled {
		t.Error("重复安装必须标记 already_installed")
	}
	if insts[KindAgent].installs != 1 {
		t.Errorf("落点被调用 %d 次，want 1（重复安装不应再落地）", insts[KindAgent].installs)
	}
	if store.upserts != 1 {
		t.Errorf("upsert %d 次，want 1", store.upserts)
	}
	if second.Ref["id"] != first.Ref["id"] {
		t.Errorf("幂等分支应返回**已有**落点引用，got %v want %v", second.Ref, first.Ref)
	}
}

// ── 内外源默认启用状态 ──

// 内置源装完即可用（Enabled=true）。
func TestInstall_BuiltinIsEnabledByDefault(t *testing.T) {
	svc, _, _ := newTestInstallService(false)
	res, err := svc.Install(context.Background(), "1", "u1", builtinAgentItem(), nil)
	if err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	if !res.Enabled {
		t.Error("内置源条目装完应为 enabled")
	}
	if res.RequiresApproval {
		t.Error("内置 agent 条目不应要求额外授权")
	}
}

// 外部源装完默认**禁用**：外部内容没经过同源发布，装好不等于获得启用。
func TestInstall_ExternalIsDisabledByDefault(t *testing.T) {
	svc, _, _ := newTestInstallService(true)
	res, err := svc.Install(context.Background(), "1", "u1", externalItem(KindSkill, "skill:ext"), nil)
	if err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	if res.Enabled {
		t.Error("外部源条目装完必须为 disabled（默认禁止，需用户显式启用）")
	}
}

// 外部 MCP：即使 MARKET_ALLOW_REMOTE_MCP=true 允许安装，装完也**强制**要求授权。
func TestInstall_ExternalMCPForcesApproval(t *testing.T) {
	svc, _, _ := newTestInstallService(true)
	res, err := svc.Install(context.Background(), "1", "u1", externalItem(KindMCP, "mcp:ext"), nil)
	if err != nil {
		t.Fatalf("AllowRemoteMCP=true 时应允许安装: %v", err)
	}
	if !res.RequiresApproval {
		t.Error("外部 mcp 必须 requires.approval=true")
	}
	if res.Enabled {
		t.Error("外部 mcp 装完应默认禁用")
	}
}

// MARKET_ALLOW_REMOTE_MCP=false（默认）时外部 mcp 一律拒绝，且**不落地**。
func TestInstall_ExternalMCPForbiddenByDefault(t *testing.T) {
	svc, _, insts := newTestInstallService(false)
	_, err := svc.Install(context.Background(), "1", "u1", externalItem(KindMCP, "mcp:ext"), nil)
	if !errors.Is(err, ErrRemoteMCPForbidden) {
		t.Fatalf("err = %v，want ErrRemoteMCPForbidden", err)
	}
	if insts[KindMCP].installs != 0 {
		t.Error("被策略拒绝时不应调用落点（外部 MCP 默认禁止安装）")
	}
}

// 内置 mcp 不受 AllowRemoteMCP 影响：它是同源发布的目录条目。
// 但条目自己声明了 requires.approval，装完仍需用户授权。
func TestInstall_BuiltinMCPNeedsApprovalButAllowed(t *testing.T) {
	svc, _, _ := newTestInstallService(false)
	it := Item{
		Kind: KindMCP, ID: "mcp:filesystem", Name: "filesystem",
		Source: SourceBuiltin, Payload: []byte(`{"command":"npx"}`),
		Requires: Requires{Approval: true},
	}
	res, err := svc.Install(context.Background(), "1", "u1", it, nil)
	if err != nil {
		t.Fatalf("内置 mcp 应可安装: %v", err)
	}
	if !res.RequiresApproval {
		t.Error("内置 mcp 声明了 requires.approval，结果必须如实返回")
	}
}

// ── 参数校验 ──

func TestInstall_RejectsUnknownKind(t *testing.T) {
	svc, _, _ := newTestInstallService(false)
	it := builtinAgentItem()
	it.Kind = Kind("bogus")
	if _, err := svc.Install(context.Background(), "1", "u1", it, nil); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("err = %v，want ErrBadRequest（kind 非法 → 400）", err)
	}
}

// 没有为该 kind 注入落点实现时必须明确失败（HTTP 层映射 501），不能伪造成功。
func TestInstall_MissingInstallerFails(t *testing.T) {
	store := newFakeStore()
	svc := NewInstallService(store, nil, InstallOptions{})
	if _, err := svc.Install(context.Background(), "1", "u1", builtinAgentItem(), nil); !errors.Is(err, ErrInstallerUnavailable) {
		t.Fatalf("err = %v，want ErrInstallerUnavailable", err)
	}
}

// 落点失败必须原样冒泡，且**不写记录**（否则用户会看到"已安装"却什么都没有）。
func TestInstall_InstallerErrorDoesNotRecord(t *testing.T) {
	svc, store, insts := newTestInstallService(false)
	insts[KindAgent].failWith = errors.New("db down")
	if _, err := svc.Install(context.Background(), "1", "u1", builtinAgentItem(), nil); err == nil {
		t.Fatal("落点失败时应返回错误")
	}
	if store.upserts != 0 {
		t.Errorf("落点失败仍写了 %d 条记录", store.upserts)
	}
}

// env 必须原样传到落点（用户在安装表单里填的凭据）。
func TestInstall_PassesEnvToInstaller(t *testing.T) {
	svc, _, insts := newTestInstallService(false)
	env := map[string]string{"GITHUB_TOKEN": "t0ken"}
	if _, err := svc.Install(context.Background(), "1", "u1", builtinAgentItem(), env); err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	if insts[KindAgent].gotEnv["GITHUB_TOKEN"] != "t0ken" {
		t.Errorf("env 未透传，got %v", insts[KindAgent].gotEnv)
	}
}

// ── 卸载 ──

// 卸载不存在的条目返回成功（幂等），且不调用落点卸载以外的任何破坏性动作。
func TestUninstall_MissingIsSuccess(t *testing.T) {
	svc, store, insts := newTestInstallService(false)
	if err := svc.Uninstall(context.Background(), "1", "u1", SourceBuiltin, KindAgent, "agent:nope", nil); err != nil {
		t.Fatalf("卸载不存在的条目应成功: %v", err)
	}
	if insts[KindAgent].uninstalls != 0 {
		t.Error("没有记录时不应调用落点卸载")
	}
	if store.deletes != 1 {
		t.Errorf("delete %d 次，want 1（顺手清掉可能的半写状态）", store.deletes)
	}
}

// 已安装条目：先让落点卸载成功，再删记录。
func TestUninstall_InstalledRemovesBothSides(t *testing.T) {
	svc, store, insts := newTestInstallService(false)
	ctx := context.Background()
	if _, err := svc.Install(ctx, "1", "u1", builtinAgentItem(), nil); err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	if err := svc.Uninstall(ctx, "1", "u1", SourceBuiltin, KindAgent, "agent:code-reviewer", nil); err != nil {
		t.Fatalf("卸载失败: %v", err)
	}
	if insts[KindAgent].uninstalls != 1 {
		t.Errorf("落点卸载 %d 次，want 1", insts[KindAgent].uninstalls)
	}
	if _, ok := store.rows[key(SourceBuiltin, KindAgent, "agent:code-reviewer")]; ok {
		t.Error("记录未删除")
	}
}

// 落点卸载失败时**保留记录**：否则记录没了、东西还在，用户既看不到也无法再卸。
func TestUninstall_InstallerErrorKeepsRecord(t *testing.T) {
	svc, store, insts := newTestInstallService(false)
	ctx := context.Background()
	if _, err := svc.Install(ctx, "1", "u1", builtinAgentItem(), nil); err != nil {
		t.Fatalf("安装失败: %v", err)
	}
	insts[KindAgent].unfailWith = errors.New("upstream down")
	if err := svc.Uninstall(ctx, "1", "u1", SourceBuiltin, KindAgent, "agent:code-reviewer", nil); err == nil {
		t.Fatal("落点卸载失败时应返回错误")
	}
	if _, ok := store.rows[key(SourceBuiltin, KindAgent, "agent:code-reviewer")]; !ok {
		t.Error("落点卸载失败后记录必须保留（可重试）")
	}
}

// ── 列表 ──

func TestList_FiltersByKind(t *testing.T) {
	svc, _, _ := newTestInstallService(true)
	ctx := context.Background()
	if _, err := svc.Install(ctx, "1", "u1", builtinAgentItem(), nil); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Install(ctx, "1", "u1", externalItem(KindSkill, "skill:ext"), nil); err != nil {
		t.Fatal(err)
	}

	all, err := svc.List(ctx, "1", "u1", "")
	if err != nil {
		t.Fatalf("list 失败: %v", err)
	}
	if len(all) != 2 {
		t.Errorf("全部安装记录 = %d，want 2", len(all))
	}

	skills, err := svc.List(ctx, "1", "u1", KindSkill)
	if err != nil {
		t.Fatalf("list 失败: %v", err)
	}
	if len(skills) != 1 || skills[0].Kind != KindSkill {
		t.Errorf("kind 过滤失效: %+v", skills)
	}
}

func TestList_RejectsUnknownKind(t *testing.T) {
	svc, _, _ := newTestInstallService(false)
	if _, err := svc.List(context.Background(), "1", "u1", Kind("bogus")); !errors.Is(err, ErrBadRequest) {
		t.Fatalf("err = %v，want ErrBadRequest", err)
	}
}

// 记录里的 Ref 必须原样返回（卸载要靠它找到落点）。
func TestInstall_RefRoundTrip(t *testing.T) {
	svc, store, _ := newTestInstallService(false)
	ctx := context.Background()
	if _, err := svc.Install(ctx, "1", "u1", builtinAgentItem(), nil); err != nil {
		t.Fatal(err)
	}
	rec := store.rows[key(SourceBuiltin, KindAgent, "agent:code-reviewer")]
	if rec.Ref["id"] != "local-agent:code-reviewer" {
		t.Errorf("ref = %v，落点引用必须落库", rec.Ref)
	}
	if rec.CreatedAt.IsZero() || time.Since(rec.CreatedAt) > time.Minute {
		t.Errorf("created_at = %v，应为安装时刻", rec.CreatedAt)
	}
	if !rec.Enabled {
		t.Error("内置条目记录应为 enabled")
	}
}
