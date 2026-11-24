package market

// 外部内容的信任链（H5）。
//
// 目录阶段只做**形状**校验（见 external.go：字段白名单、manifest_url 必须是 https、
// sha256 是 64 位 hex）。但"形状对"不等于"内容可信"：目录里的 payload 只是展示信息，
// 真正落库的内容必须来自 manifest，而 manifest 只有经过
//
//	1) 按目录里声明的 sha256 做**强校验**，
//	2) 按 `MARKET_TRUSTED_KEYS` 做 **Ed25519 验签**（签名覆盖 sha256(manifest 字节)），
//
// 之后才可以交给落点。任何一步失败都**不产生 VerifiedItem**，因此调用方（安装编排）
// 拿不到可写入的 item —— digest 或签名不匹配不可能落盘。
//
// 默认语义是 fail-closed，三个"不"：
//   - 外部源总开关关着时，外部条目一律不校验通过（连抓都不抓）；
//   - 外部条目缺 sha256 → 拒绝（不做"没哈希就当通过"的降级）；
//   - 要求签名（默认）时未签名 → 拒绝；配了签名但没有信任根 → 拒绝，
//     绝不因为"没有公钥所以跳过验签"而放行。
//
// 唯一不需要校验的是**内置源**：它随二进制发布，内容与代码同源（//go:embed），
// 已经有构建期的完整性；给它再套一层市场签名只会让离线安装凭空依赖网络。

import (
	"context"
	"crypto/ed25519"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"strings"
)

// 信任链上的稳定错误。HTTP 层按 errors.Is 映射状态码（见 api/market_install.go 的
// writeInstallError），因此这些值必须保持稳定，不能被包裹成不可识别的字符串。
var (
	// ErrExternalSourceDisabled 表示外部源总开关关闭（默认状态）。
	ErrExternalSourceDisabled = errors.New(
		"external market sources are disabled: set MARKET_REMOTE_ENABLED=true to allow them")
	// ErrManifestUnavailable 表示条目没有 manifest_url（外部条目无法安装）。
	ErrManifestUnavailable = errors.New("market item has no manifest_url")
	// ErrManifestDigestMissing 表示条目没有声明 sha256（或缺格式不正确）。
	ErrManifestDigestMissing = errors.New("manifest sha256 is required")
	// ErrManifestDigestMismatch 表示抓到的 manifest 与声明的 sha256 不一致。
	ErrManifestDigestMismatch = errors.New("manifest sha256 mismatch")
	// ErrManifestUnsigned 表示条目未签名而策略要求签名。
	ErrManifestUnsigned = errors.New("manifest signature is required")
	// ErrTrustRootMissing 表示没有配置任何可信公钥（无法验签）。
	ErrTrustRootMissing = errors.New("no trusted signing keys configured (MARKET_TRUSTED_KEYS)")
	// ErrManifestSignatureInvalid 表示 key_id 未知 / base64 非法 / 验签失败。
	ErrManifestSignatureInvalid = errors.New("manifest signature is invalid")
	// ErrManifestFetchFailed 表示 manifest 抓取失败（不可达/超时/被 SSRF 策略拦）。
	ErrManifestFetchFailed = errors.New("manifest fetch failed")
)

// Verification 是一次通过的完整性/信任链校验结果。
//
// **只有 Verified=true 才代表内容可以落盘**；它的字段会被写进安装记录的 ref，
// 使"这条内容当时验的是哪份字节、由谁签的"可追溯（升级/排查都要用到）。
type Verification struct {
	Verified bool `json:"verified"`
	// ManifestSHA256 是**实际抓到**的 manifest 字节的 sha256（hex）。
	ManifestSHA256 string `json:"manifest_sha256,omitempty"`
	// KeyID 是验签通过的 key_id（未签名时为空）。
	KeyID string `json:"signature_key_id,omitempty"`
	// Signed 表示这次校验是否包含验签环节。
	Signed bool `json:"signed,omitempty"`
}

// TrustPolicy 是信任链策略（来自配置，构造期固定）。
type TrustPolicy struct {
	// ExternalEnabled 是外部源总开关（MARKET_REMOTE_ENABLED，默认 false）。
	ExternalEnabled bool
	// RequireSignature 要求外部条目必须带签名（默认 true —— 未签名一律拒绝）。
	RequireSignature bool
	// TrustedKeys 是 key_id → 公钥。为空表示没有信任根：带签名的条目会被拒绝，
	// 因为"无法验签"和"验签失败"在安全上是一回事。
	TrustedKeys map[string]ed25519.PublicKey
}

// Verifier 是安装前置校验的端口。
//
// 抽成接口的用途：安装编排（internal/market/install.go）不依赖网络，测试可以注入
// 一个"永远拒绝"的验证器来断言**看起来能装的条目一个字节都不会落盘**。
type Verifier interface {
	// Verify 返回**已校验的**条目（Payload 被替换成 manifest 原始字节）与校验结果。
	// 内置源条目原样返回，不作网络访问。
	Verify(ctx context.Context, item Item) (Item, Verification, error)
}

// ManifestVerifier 是 Verifier 的默认实现：抓取 manifest → 校验 digest → 验签。
type ManifestVerifier struct {
	docs   DocumentFetcher
	policy TrustPolicy
}

// NewTrustVerifier 构造信任链校验器。docs 为 nil 时外部条目一律拒绝（fail-closed）：
// 没有出网口就不能校验，而"不能校验"不等于"可以安装"。
func NewTrustVerifier(docs DocumentFetcher, policy TrustPolicy) *ManifestVerifier {
	return &ManifestVerifier{docs: docs, policy: policy}
}

// Verify 实现 Verifier。
func (v *ManifestVerifier) Verify(ctx context.Context, item Item) (Item, Verification, error) {
	if item.Source == SourceBuiltin {
		// 内置目录：随二进制发布，构建期已保证完整性。
		return item, Verification{}, nil
	}
	if !v.policy.ExternalEnabled {
		return item, Verification{}, ErrExternalSourceDisabled
	}
	if strings.TrimSpace(item.ManifestURL) == "" {
		return item, Verification{}, ErrManifestUnavailable
	}
	if strings.TrimSpace(item.SHA256) == "" {
		return item, Verification{}, ErrManifestDigestMissing
	}
	if item.Signature == nil && v.policy.RequireSignature {
		return item, Verification{}, ErrManifestUnsigned
	}
	if v.docs == nil {
		return item, Verification{}, fmt.Errorf("%w: document fetcher unavailable", ErrManifestFetchFailed)
	}

	raw, err := v.docs.FetchDocument(ctx, item.ManifestURL, MaxDocumentBytes)
	if err != nil {
		// 抓取失败的原因码（source_unreachable / source_timeout / source_invalid）
		// 保留在 detail 里：用户看到的应该是"这个源现在抓不下来"。
		return item, Verification{}, fmt.Errorf("%w: %v", ErrManifestFetchFailed, err)
	}

	ver, err := VerifyManifestBytes(raw, item, v.policy)
	if err != nil {
		return item, Verification{}, err
	}

	// **Payload 换成校验过的字节**：落点写的就是这份内容。
	// 目录里那份 payload 只是展示信息（不可信），绝不能流到落点。
	out := item
	out.Payload = raw
	return out, ver, nil
}

// VerifyManifestBytes 校验一份内存中的 manifest（**纯函数，无 I/O**）。
//
// 顺序刻意固定为 digest → 签名：
//   - digest 先跑，因为签名覆盖的就是 digest，digest 不符时验签没有意义；
//   - 两者都不匹配时返回 digest 错误（更早、更好排查），签名错误只在 digest 通过后出现。
func VerifyManifestBytes(raw []byte, item Item, policy TrustPolicy) (Verification, error) {
	want := strings.ToLower(strings.TrimSpace(item.SHA256))
	if want == "" {
		return Verification{}, ErrManifestDigestMissing
	}
	wantRaw, err := hex.DecodeString(want)
	if err != nil || len(wantRaw) != sha256.Size {
		return Verification{}, fmt.Errorf("%w: sha256 must be 64 hex characters", ErrManifestDigestMissing)
	}

	digest := sha256.Sum256(raw)
	if subtle.ConstantTimeCompare(digest[:], wantRaw) != 1 {
		return Verification{}, fmt.Errorf("%w: got %s", ErrManifestDigestMismatch, hex.EncodeToString(digest[:]))
	}

	ver := Verification{Verified: true, ManifestSHA256: hex.EncodeToString(digest[:])}
	sig := item.Signature
	if sig == nil {
		if policy.RequireSignature {
			return Verification{}, ErrManifestUnsigned
		}
		return ver, nil
	}
	if len(policy.TrustedKeys) == 0 {
		return Verification{}, ErrTrustRootMissing
	}
	pub, ok := policy.TrustedKeys[strings.TrimSpace(sig.KeyID)]
	if !ok {
		return Verification{}, fmt.Errorf("%w: unknown key_id %q", ErrManifestSignatureInvalid, sig.KeyID)
	}
	sigRaw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(sig.Value))
	if err != nil {
		return Verification{}, fmt.Errorf("%w: signature is not valid base64", ErrManifestSignatureInvalid)
	}
	if len(sigRaw) != ed25519.SignatureSize || !ed25519.Verify(pub, digest[:], sigRaw) {
		return Verification{}, ErrManifestSignatureInvalid
	}
	ver.Signed, ver.KeyID = true, strings.TrimSpace(sig.KeyID)
	return ver, nil
}

// ParseTrustedKeys 把配置里的 key_id → base64 公钥解析成公钥表。
//
// 与 RemoteOptions.TrustedKeys 共用同一套解析（坏公钥只跳过自己）：配置里有一个
// 写错的 key 不该把所有源判成签名无效。
func ParseTrustedKeys(in map[string]string) map[string]ed25519.PublicKey {
	return parseTrustedKeys(in)
}
