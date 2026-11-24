package market

// H75 编排层回归：**装完不等于可用**，启用才可用。
//
// 三条必须成立的语义：
//  1. 外部内容安装后 `Enabled=false`（inert），启用是显式动作；
//  2. 启用前重新做 H5 完整性复验与 H24 授权检查 —— 任一失败都不得调用落点；
//  3. 停用**立即生效**（不需要复验），落点与记录同时翻回不可用。

import (
	"context"
	"errors"
	"testing"
)

// fakeEnablerInstaller 是"支持启停"的落点替身。
type fakeEnablerInstaller struct {
	*fakeInstaller
	calls    int
	lastFlag bool
	lastRef  map[string]any
	failWith error
}

func (f *fakeEnablerInstaller) SetEnabled(_ context.Context, _ Item, ref map[string]any, enabled bool) error {
	if f.failWith != nil {
		return f.failWith
	}
	f.calls++
	f.lastFlag = enabled
	f.lastRef = ref
	return nil
}

// newEnableService 构造带验证器/授权钩子的编排服务，并预置一条已装记录。
func newEnableService(t *testing.T, inst *fakeEnablerInstaller) (*InstallService, *fakeStore) {
	t.Helper()
	store := newFakeStore()
	store.rows[key("team", KindSkill, "skill:ext")] = InstalledRecord{
		Source: "team", Kind: KindSkill, MarketID: "skill:ext",
		Ref:     map[string]any{"name": "ext", "manifest_sha256": digestHex([]byte(`{"name":"ext"}`))},
		Enabled: false,
	}
	svc := NewInstallService(store, map[Kind]Installer{KindSkill: inst}, InstallOptions{
		Verifier: &stubVerifier{ver: Verification{Verified: true, ManifestSHA256: digestHex([]byte(`{"name":"ext"}`))}},
		EnableCheck: func(_ context.Context, _ Item, _ map[string]any) error {
			return nil
		},
	})
	return svc, store
}

// externalRef 是外部 skill 条目的"目录形态"（启用时要用它复验 manifest）。
func externalRefItem() Item {
	raw := []byte(`{"name":"ext"}`)
	item := externalManifestItem(raw)
	item.ID, item.Name = "skill:ext", "ext"
	return item
}

// 未安装 → ErrNotInstalled（404），不触碰落点。
func TestSetEnabled_NotInstalled(t *testing.T) {
	inst := &fakeEnablerInstaller{fakeInstaller: &fakeInstaller{}}
	svc, _ := newEnableService(t, inst)
	item := externalRefItem()
	item.ID = "skill:nope"

	if _, err := svc.SetEnabled(context.Background(), "1", "u1", item, true); !errors.Is(err, ErrNotInstalled) {
		t.Fatalf("err = %v，want ErrNotInstalled", err)
	}
	if inst.calls != 0 {
		t.Error("未安装时不得调用落点")
	}
}

// 启用外部内容：复验通过 → 落点被启用 + 记录翻成 enabled + digest 合并进 ref。
func TestSetEnabled_ExternalEnableVerifiesAndFlips(t *testing.T) {
	inst := &fakeEnablerInstaller{fakeInstaller: &fakeInstaller{}}
	svc, store := newEnableService(t, inst)

	res, err := svc.SetEnabled(context.Background(), "1", "u1", externalRefItem(), true)
	if err != nil {
		t.Fatalf("启用失败: %v", err)
	}
	if !res.Enabled {
		t.Error("启用后结果必须 enabled=true")
	}
	if inst.calls != 1 || !inst.lastFlag {
		t.Errorf("落点 SetEnabled 调用 %d 次 flag=%v，want 1/true", inst.calls, inst.lastFlag)
	}
	rec := store.rows[key("team", KindSkill, "skill:ext")]
	if !rec.Enabled {
		t.Error("安装记录必须翻成 enabled")
	}
	if rec.Ref["manifest_verified"] != true {
		t.Errorf("复验结果应合并进 ref: %v", rec.Ref)
	}
}

// 启用前复验失败（digest 不匹配）→ 拒绝，落点不动，记录仍 disabled。
func TestSetEnabled_VerificationFailureKeepsDisabled(t *testing.T) {
	inst := &fakeEnablerInstaller{fakeInstaller: &fakeInstaller{}}
	store := newFakeStore()
	store.rows[key("team", KindSkill, "skill:ext")] = InstalledRecord{
		Source: "team", Kind: KindSkill, MarketID: "skill:ext",
		Ref: map[string]any{"name": "ext"}, Enabled: false,
	}
	svc := NewInstallService(store, map[Kind]Installer{KindSkill: inst}, InstallOptions{
		Verifier: &stubVerifier{err: ErrManifestDigestMismatch},
	})

	_, err := svc.SetEnabled(context.Background(), "1", "u1", externalRefItem(), true)
	if !errors.Is(err, ErrManifestDigestMismatch) {
		t.Fatalf("err = %v，want ErrManifestDigestMismatch", err)
	}
	if inst.calls != 0 {
		t.Error("复验失败时不得启用落点")
	}
	if store.rows[key("team", KindSkill, "skill:ext")].Enabled {
		t.Error("复验失败时记录必须保持 disabled")
	}
}

// 授权检查未通过（H24）→ 拒绝，落点不动。
func TestSetEnabled_EnableCheckDenied(t *testing.T) {
	inst := &fakeEnablerInstaller{fakeInstaller: &fakeInstaller{}}
	store := newFakeStore()
	store.rows[key("team", KindSkill, "skill:ext")] = InstalledRecord{
		Source: "team", Kind: KindSkill, MarketID: "skill:ext",
		Ref: map[string]any{"name": "ext"}, Enabled: false,
	}
	svc := NewInstallService(store, map[Kind]Installer{KindSkill: inst}, InstallOptions{
		EnableCheck: func(_ context.Context, _ Item, _ map[string]any) error { return ErrEnableDenied },
	})

	if _, err := svc.SetEnabled(context.Background(), "1", "u1", externalRefItem(), true); !errors.Is(err, ErrEnableDenied) {
		t.Fatalf("err = %v，want ErrEnableDenied", err)
	}
	if inst.calls != 0 {
		t.Error("授权检查未通过时不得启用落点")
	}
}

// 停用：**不复验**也必须能成功（撤销不能因为 manifest 抓不到而被卡住）。
func TestSetEnabled_DisableSkipsVerification(t *testing.T) {
	inst := &fakeEnablerInstaller{fakeInstaller: &fakeInstaller{}}
	store := newFakeStore()
	store.rows[key("team", KindSkill, "skill:ext")] = InstalledRecord{
		Source: "team", Kind: KindSkill, MarketID: "skill:ext",
		Ref: map[string]any{"name": "ext"}, Enabled: true,
	}
	svc := NewInstallService(store, map[Kind]Installer{KindSkill: inst}, InstallOptions{
		Verifier:    &stubVerifier{err: ErrManifestDigestMismatch},
		EnableCheck: func(_ context.Context, _ Item, _ map[string]any) error { return ErrEnableDenied },
	})

	res, err := svc.SetEnabled(context.Background(), "1", "u1", externalRefItem(), false)
	if err != nil {
		t.Fatalf("停用必须能成功: %v", err)
	}
	if res.Enabled {
		t.Error("停用后结果必须 enabled=false")
	}
	if inst.calls != 1 || inst.lastFlag {
		t.Errorf("落点 SetEnabled 调用 %d 次 flag=%v，want 1/false", inst.calls, inst.lastFlag)
	}
	if store.rows[key("team", KindSkill, "skill:ext")].Enabled {
		t.Error("记录必须翻成 disabled")
	}
}

// 重复启用是幂等的：不重复触发落点动作。
func TestSetEnabled_RepeatedEnableIsIdempotent(t *testing.T) {
	inst := &fakeEnablerInstaller{fakeInstaller: &fakeInstaller{}}
	svc, _ := newEnableService(t, inst)
	ctx := context.Background()

	if _, err := svc.SetEnabled(ctx, "1", "u1", externalRefItem(), true); err != nil {
		t.Fatalf("首次启用失败: %v", err)
	}
	if _, err := svc.SetEnabled(ctx, "1", "u1", externalRefItem(), true); err != nil {
		t.Fatalf("重复启用失败: %v", err)
	}
	if inst.calls != 1 {
		t.Errorf("落点被调用 %d 次，want 1（重复启用不该重复动作）", inst.calls)
	}
}
