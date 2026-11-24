package market

// H5 安装流水线回归：**校验在落点之前**。
//
// 断言方式刻意做成"副作用计数"：被拒绝的安装不仅要有错误，还必须是
//     installer.installs == 0 且 store.upserts == 0
// —— 也就是"一个字节都没落盘"。只看错误码的测试无法发现"先写了再回滚"的实现。

import (
	"context"
	"crypto/ed25519"
	"errors"
	"testing"
)

// itemRecordingInstaller 记录落点**收到的条目**（既有 fakeInstaller 只记 env/次数）。
//
// 为什么要它：H5 的关键事实之一是"落点拿到的是校验过的字节，而不是目录里那份
// 展示用 payload"，只看 installs 计数无法断言这一点。
type itemRecordingInstaller struct {
	fakeInstaller
	lastItem Item
}

func (r *itemRecordingInstaller) Install(ctx context.Context, item Item, env map[string]string) (map[string]any, error) {
	res, err := r.fakeInstaller.Install(ctx, item, env)
	if err == nil {
		r.lastItem = item
	}
	return res, err
}

// stubVerifier 是 Verifier 的测试替身：可注入错误，并可断言收到的条目。
type stubVerifier struct {
	ver   Verification
	err   error
	calls int
	got   Item
	// out 是校验通过时交回调用方的条目（需模拟"payload 被替换"时用）。
	out *Item
}

func (s *stubVerifier) Verify(_ context.Context, item Item) (Item, Verification, error) {
	s.calls++
	s.got = item
	if s.err != nil {
		return item, Verification{}, s.err
	}
	if s.out != nil {
		return *s.out, s.ver, nil
	}
	return item, s.ver, nil
}

// newVerifiedService 构造带验证器的编排服务。
func newVerifiedService(t *testing.T, v Verifier) (*InstallService, *fakeStore, map[Kind]*fakeInstaller) {
	t.Helper()
	store := newFakeStore()
	insts := map[Kind]*fakeInstaller{}
	m := map[Kind]Installer{}
	for _, k := range Kinds {
		f := &fakeInstaller{}
		insts[k] = f
		m[k] = f
	}
	return NewInstallService(store, m, InstallOptions{
		AllowRemoteMCP: true,
		Verifier:       v,
	}), store, insts
}

// 外部 source 默认关闭：验证器（生产实现）用默认策略时，外部条目**不落地**。
// 这是 H5 的第一条验收：外部源关着就不该有任何外部内容被装进来。
func TestInstall_ExternalSourceDisabledWritesNothing(t *testing.T) {
	raw := []byte(`{"name":"ext"}`)
	verifier := NewTrustVerifier(&stubDocuments{body: raw}, TrustPolicy{
		ExternalEnabled:  false, // 默认形态
		RequireSignature: true,
	})
	svc, store, insts := newVerifiedService(t, verifier)

	_, err := svc.Install(context.Background(), "1", "u1", externalManifestItem(raw), nil)
	if !errors.Is(err, ErrExternalSourceDisabled) {
		t.Fatalf("err = %v，want ErrExternalSourceDisabled", err)
	}
	if insts[KindSkill].installs != 0 {
		t.Error("外部源关闭时不得调用落点（一个字节都不该落盘）")
	}
	if store.upserts != 0 {
		t.Errorf("外部源关闭时仍写了 %d 条安装记录", store.upserts)
	}
}

// 缺 sha256 → 拒绝且不落盘（攻击探针）。
func TestInstall_MissingDigestWritesNothing(t *testing.T) {
	raw := []byte(`{"name":"ext"}`)
	item := externalManifestItem(raw)
	item.SHA256 = ""
	pub, _ := newKeypair(t)
	verifier := NewTrustVerifier(&stubDocuments{body: raw}, TrustPolicy{
		ExternalEnabled:  true,
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	})
	svc, store, insts := newVerifiedService(t, verifier)

	_, err := svc.Install(context.Background(), "1", "u1", item, nil)
	if !errors.Is(err, ErrManifestDigestMissing) {
		t.Fatalf("err = %v，want ErrManifestDigestMissing", err)
	}
	if insts[KindSkill].installs != 0 || store.upserts != 0 {
		t.Errorf("缺摘要时仍落地了（installs=%d upserts=%d）", insts[KindSkill].installs, store.upserts)
	}
}

// digest 不匹配 → 拒绝且不落盘（攻击探针）。
func TestInstall_DigestMismatchWritesNothing(t *testing.T) {
	declared := []byte(`{"name":"original"}`)
	tampered := []byte(`{"name":"evil"}`)
	svc, store, insts := newVerifiedService(t, &stubVerifier{err: ErrManifestDigestMismatch})

	_, err := svc.Install(context.Background(), "1", "u1", externalManifestItem(declared), nil)
	if !errors.Is(err, ErrManifestDigestMismatch) {
		t.Fatalf("err = %v，want ErrManifestDigestMismatch", err)
	}
	if insts[KindSkill].installs != 0 || store.upserts != 0 {
		t.Errorf("digest 不匹配时仍落地了（installs=%d upserts=%d）", insts[KindSkill].installs, store.upserts)
	}
	// 真实抓取路径同样拦住：抓到的字节与声明的摘要不符。
	verifier := NewTrustVerifier(&stubDocuments{body: tampered}, TrustPolicy{
		ExternalEnabled:  true,
		RequireSignature: false,
	})
	svc2, store2, insts2 := newVerifiedService(t, verifier)
	_, err = svc2.Install(context.Background(), "1", "u1", externalManifestItem(declared), nil)
	if !errors.Is(err, ErrManifestDigestMismatch) {
		t.Fatalf("真实抓取路径 err = %v，want ErrManifestDigestMismatch", err)
	}
	if insts2[KindSkill].installs != 0 || store2.upserts != 0 {
		t.Errorf("真实抓取路径仍落地了（installs=%d upserts=%d）", insts2[KindSkill].installs, store2.upserts)
	}
}

// 未签名 → 拒绝且不落盘（攻击探针）。
func TestInstall_UnsignedWritesNothing(t *testing.T) {
	raw := []byte(`{"name":"ext"}`)
	pub, _ := newKeypair(t)
	verifier := NewTrustVerifier(&stubDocuments{body: raw}, TrustPolicy{
		ExternalEnabled:  true,
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	})
	svc, store, insts := newVerifiedService(t, verifier)

	item := externalManifestItem(raw) // 无 signature
	_, err := svc.Install(context.Background(), "1", "u1", item, nil)
	if !errors.Is(err, ErrManifestUnsigned) {
		t.Fatalf("err = %v，want ErrManifestUnsigned", err)
	}
	if insts[KindSkill].installs != 0 || store.upserts != 0 {
		t.Errorf("未签名时仍落地了（installs=%d upserts=%d）", insts[KindSkill].installs, store.upserts)
	}
}

// 签名错误（自签）→ 拒绝且不落盘（攻击探针）。
func TestInstall_BadSignatureWritesNothing(t *testing.T) {
	raw := []byte(`{"name":"ext"}`)
	pub, _ := newKeypair(t)
	_, attacker := newKeypair(t)
	item := externalManifestItem(raw)
	sig := signManifest(t, raw, attacker)
	item.Signature = &sig

	verifier := NewTrustVerifier(&stubDocuments{body: raw}, TrustPolicy{
		ExternalEnabled:  true,
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	})
	svc, store, insts := newVerifiedService(t, verifier)

	_, err := svc.Install(context.Background(), "1", "u1", item, nil)
	if !errors.Is(err, ErrManifestSignatureInvalid) {
		t.Fatalf("err = %v，want ErrManifestSignatureInvalid", err)
	}
	if insts[KindSkill].installs != 0 || store.upserts != 0 {
		t.Errorf("签名错误时仍落地了（installs=%d upserts=%d）", insts[KindSkill].installs, store.upserts)
	}
}

// 校验通过：落点收到的是**校验过的字节**，且 ref 里留下 digest/key_id。
func TestInstall_VerifiedContentIsPersistedWithDigest(t *testing.T) {
	raw := []byte(`{"name":"good","exec":{"type":"prompt","source":"x"}}`)
	item := externalManifestItem(raw)
	verified := item
	verified.Payload = raw // 模拟"抓取并校验后 payload 被替换"
	rec := &itemRecordingInstaller{fakeInstaller: fakeInstaller{}}
	store := newFakeStore()
	svc := NewInstallService(store, map[Kind]Installer{KindSkill: rec}, InstallOptions{
		Verifier: &stubVerifier{
			out: &verified,
			ver: Verification{Verified: true, ManifestSHA256: digestHex(raw), KeyID: "test-key", Signed: true},
		},
	})

	if _, err := svc.Install(context.Background(), "1", "u1", item, nil); err != nil {
		t.Fatalf("校验通过时应可安装: %v", err)
	}
	if string(rec.lastItem.Payload) != string(raw) {
		t.Errorf("落点收到的 payload = %q，want 校验过的 manifest 字节", rec.lastItem.Payload)
	}
	record := store.rows[key("team", KindSkill, "skill:ext")]
	if record.Ref["manifest_sha256"] != digestHex(raw) {
		t.Errorf("ref.manifest_sha256 = %v，want %s", record.Ref["manifest_sha256"], digestHex(raw))
	}
	if record.Ref["signature_key_id"] != "test-key" {
		t.Errorf("ref.signature_key_id = %v，want test-key", record.Ref["signature_key_id"])
	}
	if record.Ref["manifest_verified"] != true {
		t.Errorf("ref.manifest_verified = %v，want true", record.Ref["manifest_verified"])
	}
	// 外部内容装完仍默认禁用（H75 的前置语义，这里一并守住）。
	if record.Enabled {
		t.Error("外部内容装完必须 disabled")
	}
}

// 相同摘要重复安装：走幂等，不重新落地。
func TestInstall_SameDigestStaysIdempotent(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	v := &stubVerifier{ver: Verification{Verified: true, ManifestSHA256: digestHex(raw)}}
	svc, store, insts := newVerifiedService(t, v)
	ctx := context.Background()

	if _, err := svc.Install(ctx, "1", "u1", externalManifestItem(raw), nil); err != nil {
		t.Fatalf("首次安装失败: %v", err)
	}
	res, err := svc.Install(ctx, "1", "u1", externalManifestItem(raw), nil)
	if err != nil {
		t.Fatalf("重复安装失败: %v", err)
	}
	if !res.AlreadyInstalled {
		t.Error("摘要未变时必须标记 already_installed（不得原地重装）")
	}
	if insts[KindSkill].installs != 1 {
		t.Errorf("落点被调用 %d 次，want 1", insts[KindSkill].installs)
	}
	if store.upserts != 1 {
		t.Errorf("upsert %d 次，want 1", store.upserts)
	}
}

// fakeUpdaterInstaller 是"支持原地更新"的落点替身。
type fakeUpdaterInstaller struct {
	*fakeInstaller
	updates  int
	lastItem Item
	lastRef  map[string]any
}

func (f *fakeUpdaterInstaller) Update(_ context.Context, item Item, ref map[string]any) (map[string]any, error) {
	f.updates++
	f.lastItem = item
	f.lastRef = ref
	out := map[string]any{"name": item.Name}
	for k, v := range ref {
		if k != "manifest_sha256" {
			out[k] = v
		}
	}
	return out, nil
}

// 摘要变化 + 落点支持更新 → 原地更新，记录保持一行，且**重新进入禁用状态**。
func TestInstall_ChangedDigestUpdatesInPlace(t *testing.T) {
	old := []byte(`{"name":"v1"}`)
	newer := []byte(`{"name":"v2"}`)
	store := newFakeStore()
	inst := &fakeUpdaterInstaller{fakeInstaller: &fakeInstaller{}}
	svc := NewInstallService(store, map[Kind]Installer{KindSkill: inst}, InstallOptions{
		Verifier: &stubVerifier{ver: Verification{Verified: true, ManifestSHA256: digestHex(newer)}},
	})
	store.rows[key("team", KindSkill, "skill:ext")] = InstalledRecord{
		Source: "team", Kind: KindSkill, MarketID: "skill:ext",
		Ref:     map[string]any{"name": "ext", "manifest_sha256": digestHex(old)},
		Enabled: true, // 旧版本曾被用户启用
	}

	item := externalManifestItem(newer)
	res, err := svc.Install(context.Background(), "1", "u1", item, nil)
	if err != nil {
		t.Fatalf("摘要变化时应走原地更新: %v", err)
	}
	if !res.Updated || res.AlreadyInstalled {
		t.Errorf("res.Updated=%v already=%v，want updated=true 且非幂等命中", res.Updated, res.AlreadyInstalled)
	}
	if inst.updates != 1 {
		t.Errorf("Update 调用 %d 次，want 1", inst.updates)
	}
	if inst.fakeInstaller.installs != 0 {
		t.Error("更新路径不得走 Install（那会产生第二份落点）")
	}
	if store.upserts != 1 || len(store.rows) != 1 {
		t.Errorf("upserts=%d rows=%d，want 1/1（原地更新不新增记录）", store.upserts, len(store.rows))
	}
	rec := store.rows[key("team", KindSkill, "skill:ext")]
	if rec.Enabled {
		t.Error("内容换过之后必须重新进入禁用状态（需用户重新信任）")
	}
	if rec.Ref["manifest_sha256"] != digestHex(newer) {
		t.Errorf("ref.manifest_sha256 未更新: %v", rec.Ref["manifest_sha256"])
	}
}

// 摘要变化但落点不支持更新 → 明确 409 语义错误，不产生第二份落点。
func TestInstall_ChangedDigestWithoutUpdaterFails(t *testing.T) {
	old := []byte(`{"name":"v1"}`)
	newer := []byte(`{"name":"v2"}`)
	svc, store, insts := newVerifiedService(t, &stubVerifier{
		ver: Verification{Verified: true, ManifestSHA256: digestHex(newer)},
	})
	store.rows[key("team", KindAgent, "agent:ext")] = InstalledRecord{
		Source: "team", Kind: KindAgent, MarketID: "agent:ext",
		Ref: map[string]any{"agent_id": "a1", "manifest_sha256": digestHex(old)},
	}

	item := Item{
		Kind: KindAgent, ID: "agent:ext", Name: "ext", Source: "team",
		ManifestURL: "https://market.test/a.json", SHA256: digestHex(newer),
	}
	_, err := svc.Install(context.Background(), "1", "u1", item, nil)
	if !errors.Is(err, ErrUpdateUnavailable) {
		t.Fatalf("err = %v，want ErrUpdateUnavailable", err)
	}
	if insts[KindAgent].installs != 0 {
		t.Error("不支持更新时不得退化成再装一份")
	}
	if store.upserts != 0 {
		t.Error("失败路径不得写记录")
	}
}

// 没有注入验证器的部署（只有内置目录）：内置条目照常安装。
func TestInstall_WithoutVerifierStillInstallsBuiltin(t *testing.T) {
	svc, _, _ := newTestInstallService(false)
	if _, err := svc.Install(context.Background(), "1", "u1", builtinAgentItem(), nil); err != nil {
		t.Fatalf("内置条目安装失败: %v", err)
	}
}
