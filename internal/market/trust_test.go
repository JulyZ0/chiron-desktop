package market

// H5：外部内容信任链的**攻击探针**回归。
//
// 这一组测试守的是同一条底线：**digest 或签名不匹配的内容不可能被交回给调用方**。
// 因此每个失败用例都断言两件事 —— 返回了对应错误、且没有返回"可用于落盘的 item"。
// 文件名带 trust_ 前缀，与既有 external_test.go（目录形状校验）分开：
// 那边回答"文档长得对不对"，这边回答"字节能不能信"。

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"strings"
	"testing"
)

// stubDocuments 是 DocumentFetcher 的测试替身：记录被调用的 URL/上限并返回固定字节。
type stubDocuments struct {
	body    []byte
	err     error
	calls   int
	lastURL string
	lastMax int
}

func (s *stubDocuments) FetchDocument(_ context.Context, url string, maxBytes int) ([]byte, error) {
	s.calls++
	s.lastURL, s.lastMax = url, maxBytes
	if s.err != nil {
		return nil, s.err
	}
	return s.body, nil
}

// signManifest 用给定私钥对 manifest 的 sha256 签名（与 external.go 的 Signature 口径一致）。
func signManifest(t *testing.T, raw []byte, priv ed25519.PrivateKey) Signature {
	t.Helper()
	digest := sha256.Sum256(raw)
	return Signature{
		KeyID: "test-key",
		Value: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, digest[:])),
	}
}

func digestHex(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// newKeypair 生成一对测试密钥。
func newKeypair(t *testing.T) (ed25519.PublicKey, ed25519.PrivateKey) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("generate key: %v", err)
	}
	return pub, priv
}

// externalManifestItem 构造一条"外部源 + manifest"条目（不含签名，由用例补）。
func externalManifestItem(raw []byte) Item {
	return Item{
		Kind:        KindSkill,
		ID:          "skill:ext",
		Name:        "ext",
		Source:      "team",
		ManifestURL: "https://market.test/skill.json",
		SHA256:      digestHex(raw),
		// 目录里的 payload 只是展示信息：**不可信**，不能流到落点。
		Payload: []byte(`{"name":"tampered"}`),
	}
}

// ── 纯函数：digest ──

// 缺 sha256 → 直接拒绝（不做"没哈希就当通过"的降级）。
func TestVerifyManifestBytes_RejectsMissingDigest(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	item := externalManifestItem(raw)
	item.SHA256 = ""
	if _, err := VerifyManifestBytes(raw, item, TrustPolicy{}); !errors.Is(err, ErrManifestDigestMissing) {
		t.Fatalf("err = %v，want ErrManifestDigestMissing", err)
	}
}

// sha256 不是 64 位 hex → 同样是"缺摘要"（形状不对等于没给）。
func TestVerifyManifestBytes_RejectsMalformedDigest(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	item := externalManifestItem(raw)
	item.SHA256 = "not-a-hex-digest"
	if _, err := VerifyManifestBytes(raw, item, TrustPolicy{}); !errors.Is(err, ErrManifestDigestMissing) {
		t.Fatalf("err = %v，want ErrManifestDigestMissing", err)
	}
}

// digest 不匹配（内容被换过）→ 拒绝。
func TestVerifyManifestBytes_RejectsDigestMismatch(t *testing.T) {
	declared := []byte(`{"name":"original"}`)
	tampered := []byte(`{"name":"evil"}`)
	item := externalManifestItem(declared)
	if _, err := VerifyManifestBytes(tampered, item, TrustPolicy{}); !errors.Is(err, ErrManifestDigestMismatch) {
		t.Fatalf("err = %v，want ErrManifestDigestMismatch", err)
	}
}

// ── 纯函数：签名 ──

// 要求签名但条目未签名 → 拒绝。
func TestVerifyManifestBytes_RejectsUnsigned(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	_, priv := newKeypair(t)
	pub := priv.Public().(ed25519.PublicKey)
	item := externalManifestItem(raw)
	policy := TrustPolicy{
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	}
	if _, err := VerifyManifestBytes(raw, item, policy); !errors.Is(err, ErrManifestUnsigned) {
		t.Fatalf("err = %v，want ErrManifestUnsigned", err)
	}
}

// 有签名但**没有信任根** → 拒绝（"无法验签"不允许被当成"验过了"）。
func TestVerifyManifestBytes_RejectsWithoutTrustRoot(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	_, priv := newKeypair(t)
	item := externalManifestItem(raw)
	sig := signManifest(t, raw, priv)
	item.Signature = &sig
	policy := TrustPolicy{RequireSignature: true}
	if _, err := VerifyManifestBytes(raw, item, policy); !errors.Is(err, ErrTrustRootMissing) {
		t.Fatalf("err = %v，want ErrTrustRootMissing", err)
	}
}

// key_id 不在信任表里 → 拒绝（不能因为"签名看起来合法"就放行）。
func TestVerifyManifestBytes_RejectsUnknownKeyID(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	pub, priv := newKeypair(t)
	item := externalManifestItem(raw)
	sig := signManifest(t, raw, priv)
	sig.KeyID = "some-other-key"
	item.Signature = &sig
	policy := TrustPolicy{
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	}
	if _, err := VerifyManifestBytes(raw, item, policy); !errors.Is(err, ErrManifestSignatureInvalid) {
		t.Fatalf("err = %v，want ErrManifestSignatureInvalid", err)
	}
}

// 用**另一把**私钥签的（攻击者自签）→ 拒绝。
func TestVerifyManifestBytes_RejectsForgedSignature(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	pub, _ := newKeypair(t)
	_, attacker := newKeypair(t)
	item := externalManifestItem(raw)
	sig := signManifest(t, raw, attacker)
	item.Signature = &sig
	policy := TrustPolicy{
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	}
	if _, err := VerifyManifestBytes(raw, item, policy); !errors.Is(err, ErrManifestSignatureInvalid) {
		t.Fatalf("err = %v，want ErrManifestSignatureInvalid", err)
	}
}

// 签名非法 base64 → 拒绝。
func TestVerifyManifestBytes_RejectsUndecodableSignature(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	pub, _ := newKeypair(t)
	item := externalManifestItem(raw)
	item.Signature = &Signature{KeyID: "test-key", Value: "!!!not-base64!!!"}
	policy := TrustPolicy{
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	}
	if _, err := VerifyManifestBytes(raw, item, policy); !errors.Is(err, ErrManifestSignatureInvalid) {
		t.Fatalf("err = %v，want ErrManifestSignatureInvalid", err)
	}
}

// 正常路径：digest 与签名都对 → Verified + 记录 key_id。
func TestVerifyManifestBytes_AcceptsSignedManifest(t *testing.T) {
	raw := []byte(`{"name":"good","exec":{"type":"prompt","source":"x"}}`)
	pub, priv := newKeypair(t)
	item := externalManifestItem(raw)
	sig := signManifest(t, raw, priv)
	item.Signature = &sig
	policy := TrustPolicy{
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	}

	ver, err := VerifyManifestBytes(raw, item, policy)
	if err != nil {
		t.Fatalf("验签应通过: %v", err)
	}
	if !ver.Verified || !ver.Signed || ver.KeyID != "test-key" {
		t.Errorf("verification = %+v，want verified+signed+key_id", ver)
	}
	if ver.ManifestSHA256 != digestHex(raw) {
		t.Errorf("manifest_sha256 = %q，want %q", ver.ManifestSHA256, digestHex(raw))
	}
}

// 不要求签名时，未签名条目可通过（当前产品策略是要求签名，但纯函数保留该分支）。
func TestVerifyManifestBytes_AllowsUnsignedWhenPolicyPermits(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	item := externalManifestItem(raw)
	ver, err := VerifyManifestBytes(raw, item, TrustPolicy{RequireSignature: false})
	if err != nil {
		t.Fatalf("未要求签名时应通过: %v", err)
	}
	if !ver.Verified || ver.Signed {
		t.Errorf("verification = %+v，want verified 且未签名", ver)
	}
}

// ── Verifier：网络/策略分支 ──

// 外部源默认关闭：连抓都不抓。
func TestManifestVerifier_ExternalDisabledDoesNotFetch(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	docs := &stubDocuments{body: raw}
	v := NewTrustVerifier(docs, TrustPolicy{ExternalEnabled: false, RequireSignature: true})
	if _, _, err := v.Verify(context.Background(), externalManifestItem(raw)); !errors.Is(err, ErrExternalSourceDisabled) {
		t.Fatalf("err = %v，want ErrExternalSourceDisabled", err)
	}
	if docs.calls != 0 {
		t.Errorf("外部源关闭时仍发起了 %d 次抓取", docs.calls)
	}
}

// 缺 manifest_url → 拒绝且不抓取。
func TestManifestVerifier_RejectsMissingManifestURL(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	docs := &stubDocuments{body: raw}
	item := externalManifestItem(raw)
	item.ManifestURL = ""
	v := NewTrustVerifier(docs, TrustPolicy{ExternalEnabled: true, RequireSignature: true})
	if _, _, err := v.Verify(context.Background(), item); !errors.Is(err, ErrManifestUnavailable) {
		t.Fatalf("err = %v，want ErrManifestUnavailable", err)
	}
	if docs.calls != 0 {
		t.Errorf("缺 manifest_url 时不该发起抓取，calls=%d", docs.calls)
	}
}

// 缺 sha256 → 拒绝且**不抓取**（没有可对照的摘要，抓了也无法校验）。
func TestManifestVerifier_RejectsMissingDigestBeforeFetch(t *testing.T) {
	docs := &stubDocuments{body: []byte(`{"name":"x"}`)}
	item := externalManifestItem([]byte(`{"name":"x"}`))
	item.SHA256 = ""
	v := NewTrustVerifier(docs, TrustPolicy{ExternalEnabled: true, RequireSignature: true})
	if _, _, err := v.Verify(context.Background(), item); !errors.Is(err, ErrManifestDigestMissing) {
		t.Fatalf("err = %v，want ErrManifestDigestMissing", err)
	}
	if docs.calls != 0 {
		t.Errorf("缺摘要时不该抓取 manifest，calls=%d", docs.calls)
	}
}

// 抓取失败 → ErrManifestFetchFailed（可重试的上游故障，而不是"内容不可信"）。
func TestManifestVerifier_FetchFailureIsUpstream(t *testing.T) {
	item := externalManifestItem([]byte(`{"name":"x"}`))
	docs := &stubDocuments{err: NewSourceError("source_unreachable", "connection refused")}
	// 这里不要求签名：本用例只验证抓取阶段的错误归类，签名的前置拒绝另有专门用例。
	v := NewTrustVerifier(docs, TrustPolicy{ExternalEnabled: true, RequireSignature: false})
	_, _, err := v.Verify(context.Background(), item)
	if !errors.Is(err, ErrManifestFetchFailed) {
		t.Fatalf("err = %v，want ErrManifestFetchFailed", err)
	}
	if !strings.Contains(err.Error(), "source_unreachable") {
		t.Errorf("错误里应保留下层原因码: %v", err)
	}
}

// 校验通过时 **Payload 被换成抓到的原始字节**：目录里那份展示用 payload 不会流到落点。
func TestManifestVerifier_ReturnsVerifiedBytesAsPayload(t *testing.T) {
	raw := []byte(`{"name":"good"}`)
	pub, priv := newKeypair(t)
	item := externalManifestItem(raw)
	sig := signManifest(t, raw, priv)
	item.Signature = &sig
	docs := &stubDocuments{body: raw}
	v := NewTrustVerifier(docs, TrustPolicy{
		ExternalEnabled:  true,
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	})

	verified, ver, err := v.Verify(context.Background(), item)
	if err != nil {
		t.Fatalf("校验应通过: %v", err)
	}
	if !ver.Verified {
		t.Fatalf("verification 未标记 verified: %+v", ver)
	}
	if string(verified.Payload) != string(raw) {
		t.Errorf("落盘 payload = %q，want 抓到的 manifest 字节 %q", verified.Payload, raw)
	}
	if docs.lastMax != MaxDocumentBytes {
		t.Errorf("maxBytes = %d，want %d", docs.lastMax, MaxDocumentBytes)
	}
}

// 抓到的字节被换过（digest 不符）→ 拒绝，且**不返回可用于落盘的 item**。
func TestManifestVerifier_RejectsTamperedBody(t *testing.T) {
	declared := []byte(`{"name":"original"}`)
	tampered := []byte(`{"name":"evil","exec":{"type":"shell"}}`)
	item := externalManifestItem(declared)
	pub, priv := newKeypair(t)
	sig := signManifest(t, declared, priv) // 用原始内容签的
	item.Signature = &sig
	docs := &stubDocuments{body: tampered}
	v := NewTrustVerifier(docs, TrustPolicy{
		ExternalEnabled:  true,
		RequireSignature: true,
		TrustedKeys:      map[string]ed25519.PublicKey{"test-key": pub},
	})

	verified, ver, err := v.Verify(context.Background(), item)
	if !errors.Is(err, ErrManifestDigestMismatch) {
		t.Fatalf("err = %v，want ErrManifestDigestMismatch", err)
	}
	if ver.Verified {
		t.Error("失败路径不得标记 verified")
	}
	if string(verified.Payload) == string(tampered) {
		t.Error("失败路径不得把被篡改的字节交回给调用方")
	}
}

// 内置条目：随包发布，不联网、不校验。
func TestManifestVerifier_BuiltinSkipsNetwork(t *testing.T) {
	docs := &stubDocuments{body: []byte(`{}`)}
	v := NewTrustVerifier(docs, TrustPolicy{ExternalEnabled: false, RequireSignature: true})
	item := Item{Kind: KindAgent, ID: "agent:x", Source: SourceBuiltin, Payload: []byte(`{"system_prompt":"x"}`)}
	verified, ver, err := v.Verify(context.Background(), item)
	if err != nil {
		t.Fatalf("内置条目不该被校验拦下: %v", err)
	}
	if ver.Verified {
		t.Error("内置条目没有 manifest，不该标记 verified")
	}
	if string(verified.Payload) != `{"system_prompt":"x"}` {
		t.Errorf("内置条目 payload 必须原样返回: %q", verified.Payload)
	}
	if docs.calls != 0 {
		t.Errorf("内置条目不该发起抓取，calls=%d", docs.calls)
	}
}

// 没有出网口（docs == nil）时外部条目一律拒绝：不能校验 ≠ 可以安装。
func TestManifestVerifier_NoFetcherIsFailClosed(t *testing.T) {
	raw := []byte(`{"name":"x"}`)
	item := externalManifestItem(raw)
	v := NewTrustVerifier(nil, TrustPolicy{ExternalEnabled: true, RequireSignature: false})
	if _, _, err := v.Verify(context.Background(), item); !errors.Is(err, ErrManifestFetchFailed) {
		t.Fatalf("err = %v，want ErrManifestFetchFailed", err)
	}
}
