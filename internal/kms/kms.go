// Package kms 提供**信封加密**（envelope encryption）与可插拔密钥提供者。
//
// 为什么不是"直接用 APP_SECRET 加密每条值"（v1 的形态）：
//   - 每写入一条密文都直接吃主密钥，轮换主密钥必须把全部密文解出来再写回去 ——
//     一次失败就留下半套新密钥的库。
//   - 主密钥出现在每一次加解密调用里，能拿到进程内存的人随时可以解全库。
//
// 信封加密把两者分开：每条值用一次性数据密钥（DEK）加密，DEK 再用密钥加密密钥
// （KEK）包裹后与密文存在一起。轮换只重包 DEK（`Rewrap`），不需要碰数据密文，
// 也不需要在轮换窗口里让业务方重新提交明文。
//
// 密文格式（与 python-engine/app/knowledge/crypto.py 的 v2 解密逐字节对齐）：
//
//	v2:<keyID>:<base64(nonce || wrappedDEK)>:<base64(nonce || ciphertext)>
//
// 两段都是 AES-256-GCM，nonce 12 字节，无 AAD。keyID 只允许 [A-Za-z0-9._-]，
// 因为它是格式分隔符的一部分。
package kms

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"regexp"
	"strings"
)

const (
	// VersionV2 是信封密文的版本前缀。v1 仍由 internal/settings 兼容读取。
	VersionV2 = "v2:"
	// DataKeySize 是 DEK 长度（AES-256）。
	DataKeySize = 32
	nonceSize   = 12
	// AppSecretKeyID 是零配置部署的默认 KEK 标识：KEK 由 APP_SECRET 派生，
	// 行为与 v1 完全一致，只是多了一层"每条值一个 DEK"。
	AppSecretKeyID = "app-secret-v1"
)

var (
	ErrKeyUnavailable = errors.New("kms: key material unavailable")
	ErrUnknownKeyID   = errors.New("kms: unknown key id")
	ErrInvalidCipher  = errors.New("kms: invalid ciphertext")
)

var keyIDPattern = regexp.MustCompile(`^[A-Za-z0-9._-]{1,64}$`)

// Provider 是**可插拔**的密钥提供者：只有包裹/解开 DEK 需要它，数据面不依赖它。
//
// 外部 KMS（云 KMS / Vault Transit）只需实现这三个方法并通过 Register 注册；
// 本包不引入任何云 SDK，避免为一个未选定的供应商把依赖带进二进制。
type Provider interface {
	// Name 用于状态展示（如 "local"）。
	Name() string
	// ActiveKeyID 是当前**包裹**新 DEK 使用的 keyID；解旧值仍按密文里的 keyID 走。
	ActiveKeyID() string
	// WrapKey 用当前活跃 KEK 包裹 DEK，返回实际使用的 keyID。
	WrapKey(dek []byte) (keyID string, wrapped []byte, err error)
	// UnwrapKey 按密文里的 keyID 解开 DEK。未知 keyID 必须报错，不能猜。
	UnwrapKey(keyID string, wrapped []byte) ([]byte, error)
}

// Options 是提供者工厂的输入。
type Options struct {
	// AppSecret 是部署主密钥，仅 local 提供者用于派生兜底 KEK。
	AppSecret string
}

// Factory 由外部 KMS 实现注册；返回的 Provider 必须并发安全。
type Factory func(Options) (Provider, error)

var registry = map[string]Factory{}

// Register 注册一个提供者。重复注册同一名字会覆盖（便于测试与后续替代实现）。
func Register(name string, factory Factory) {
	registry[strings.ToLower(strings.TrimSpace(name))] = factory
}

// Open 按名字构造提供者。
func Open(name string, opts Options) (Provider, error) {
	f, ok := registry[strings.ToLower(strings.TrimSpace(name))]
	if !ok {
		return nil, fmt.Errorf("kms: unknown provider %q", name)
	}
	return f(opts)
}

// FromEnv 按 `KMS_PROVIDER`（默认 local）构造提供者。
func FromEnv(appSecret string) (Provider, error) {
	name := strings.TrimSpace(os.Getenv("KMS_PROVIDER"))
	if name == "" {
		name = "local"
	}
	return Open(name, Options{AppSecret: appSecret})
}

func init() {
	Register("local", func(opts Options) (Provider, error) { return NewLocal(opts.AppSecret) })
}

// localProvider 的 KEK 可来自 `KMS_KEK_KEYS`（或 `KMS_KEK_KEYS_FILE`）：
// JSON 对象 `{"v1":"<base64 32字节>"}`，配 `KMS_ACTIVE_KEY_ID` 指定新值用哪把。
// 未配置时退化为由 APP_SECRET 派生的 `app-secret-v1` —— 老部署零改动即可用信封格式。
type localProvider struct {
	active string
	keks   map[string][]byte
}

// NewLocal 构造 local 提供者。appSecret 为空且没有显式 KEK 时返回 ErrKeyUnavailable。
func NewLocal(appSecret string) (*localProvider, error) {
	keks, err := loadKEKs()
	if err != nil {
		return nil, err
	}
	active := strings.TrimSpace(os.Getenv("KMS_ACTIVE_KEY_ID"))
	if len(keks) == 0 {
		if appSecret == "" {
			return nil, ErrKeyUnavailable
		}
		// v1 的派生域必须保持 `chiron-settings:`：否则同一份 APP_SECRET 下
		// 老密文（v1）与新 KEK 会解不开，而报错只会在用户读配置时才暴露。
		sum := sha256.Sum256([]byte("chiron-settings:" + appSecret))
		keks = map[string][]byte{AppSecretKeyID: sum[:]}
		if active == "" {
			active = AppSecretKeyID
		}
	}
	if active == "" {
		// 只配了一把则用它；多把必须显式指定，避免"重启后新值换了密钥"的静默漂移。
		if len(keks) == 1 {
			for id := range keks {
				active = id
			}
		} else {
			return nil, errors.New("kms: KMS_ACTIVE_KEY_ID is required when multiple KEKs are configured")
		}
	}
	if _, ok := keks[active]; !ok {
		return nil, fmt.Errorf("kms: active key id %q not found in KMS_KEK_KEYS", active)
	}
	return &localProvider{active: active, keks: keks}, nil
}

// NewLocalWithKEKs 用给定的 KEK 表直接构造 local 提供者（测试、或配置来自 DB/Vault 时）。
func NewLocalWithKEKs(active string, keks map[string][]byte) (Provider, error) {
	if len(keks) == 0 {
		return nil, ErrKeyUnavailable
	}
	if _, ok := keks[active]; !ok {
		return nil, fmt.Errorf("kms: active key id %q not found", active)
	}
	for id, k := range keks {
		if !keyIDPattern.MatchString(id) {
			return nil, fmt.Errorf("kms: invalid key id %q", id)
		}
		if len(k) != DataKeySize {
			return nil, fmt.Errorf("kms: key %q must be %d bytes", id, DataKeySize)
		}
	}
	return &localProvider{active: active, keks: keks}, nil
}

func loadKEKs() (map[string][]byte, error) {
	raw := os.Getenv("KMS_KEK_KEYS")
	if raw == "" {
		if path := os.Getenv("KMS_KEK_KEYS_FILE"); path != "" {
			if b, err := os.ReadFile(path); err == nil {
				raw = string(b)
			}
		}
	}
	if strings.TrimSpace(raw) == "" {
		return nil, nil
	}
	return ParseKEKs(raw)
}

// ParseKEKs 解析 `{"v1":"<base64>"}`。拆出来是为了单测与未来的配置来源（DB/Vault）。
func ParseKEKs(raw string) (map[string][]byte, error) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return nil, nil
	}
	// 避免在 kms 里引 encoding/json 之外的结构体：键值都是字符串，map 足够。
	var decoded map[string]string
	if err := json.Unmarshal([]byte(raw), &decoded); err != nil {
		return nil, fmt.Errorf("kms: parse KMS_KEK_KEYS: %w", err)
	}
	out := make(map[string][]byte, len(decoded))
	for id, b64 := range decoded {
		if !keyIDPattern.MatchString(id) {
			return nil, fmt.Errorf("kms: invalid key id %q", id)
		}
		key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
		if err != nil {
			return nil, fmt.Errorf("kms: key %q is not valid base64: %w", id, err)
		}
		if len(key) != DataKeySize {
			return nil, fmt.Errorf("kms: key %q must decode to %d bytes, got %d", id, DataKeySize, len(key))
		}
		out[id] = key
	}
	return out, nil
}

func (p *localProvider) Name() string       { return "local" }
func (p *localProvider) ActiveKeyID() string { return p.active }

func (p *localProvider) WrapKey(dek []byte) (string, []byte, error) {
	kek := p.keks[p.active]
	aead, err := aeadFor(kek)
	if err != nil {
		return "", nil, err
	}
	nonce := make([]byte, nonceSize)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", nil, err
	}
	wrapped := aead.Seal(nil, nonce, dek, nil)
	return p.active, append(nonce, wrapped...), nil
}

func (p *localProvider) UnwrapKey(keyID string, wrapped []byte) ([]byte, error) {
	kek, ok := p.keks[keyID]
	if !ok {
		return nil, fmt.Errorf("%w: %s", ErrUnknownKeyID, keyID)
	}
	aead, err := aeadFor(kek)
	if err != nil {
		return nil, err
	}
	if len(wrapped) <= nonceSize {
		return nil, ErrInvalidCipher
	}
	nonce, sealed := wrapped[:nonceSize], wrapped[nonceSize:]
	dek, err := aead.Open(nil, nonce, sealed, nil)
	if err != nil {
		return nil, ErrInvalidCipher
	}
	if len(dek) != DataKeySize {
		return nil, ErrInvalidCipher
	}
	return dek, nil
}

// Envelope 是数据面：Seal/Open 只与 Provider 打交道，不关心 KEK 存在哪里。
type Envelope struct {
	provider Provider
}

// NewEnvelope 包装一个 Provider。
func NewEnvelope(p Provider) *Envelope { return &Envelope{provider: p} }

// ActiveKeyID 返回当前包裹新 DEK 使用的 keyID。
func (e *Envelope) ActiveKeyID() string {
	if e == nil || e.provider == nil {
		return ""
	}
	return e.provider.ActiveKeyID()
}

// ProviderName 返回提供者名（状态展示用）。
func (e *Envelope) ProviderName() string {
	if e == nil || e.provider == nil {
		return ""
	}
	return e.provider.Name()
}

// Seal 加密明文。每次调用都生成**新 DEK**：相同明文两次写入的密文不同，
// 也不会因为一条泄漏而牵连其它记录。
func (e *Envelope) Seal(plain []byte) (string, error) {
	if e == nil || e.provider == nil {
		return "", ErrKeyUnavailable
	}
	dek := make([]byte, DataKeySize)
	if _, err := io.ReadFull(rand.Reader, dek); err != nil {
		return "", err
	}
	defer wipe(dek)

	keyID, wrappedDEK, err := e.provider.WrapKey(dek)
	if err != nil {
		return "", err
	}
	if !keyIDPattern.MatchString(keyID) {
		return "", fmt.Errorf("kms: invalid key id %q", keyID)
	}
	aead, err := aeadFor(dek)
	if err != nil {
		return "", err
	}
	nonce := make([]byte, nonceSize)
	if _, err := io.ReadFull(rand.Reader, nonce); err != nil {
		return "", err
	}
	sealed := aead.Seal(nil, nonce, plain, nil)

	return VersionV2 + keyID + ":" +
		base64.StdEncoding.EncodeToString(wrappedDEK) + ":" +
		base64.StdEncoding.EncodeToString(append(nonce, sealed...)), nil
}

// Open 解密 Seal 的产物。keyID 必须由 provider 认识；未知一律报错（fail-closed）。
func (e *Envelope) Open(cipher string) ([]byte, error) {
	if e == nil || e.provider == nil {
		return nil, ErrKeyUnavailable
	}
	if !strings.HasPrefix(cipher, VersionV2) {
		return nil, ErrInvalidCipher
	}
	parts := strings.Split(strings.TrimPrefix(cipher, VersionV2), ":")
	if len(parts) != 3 {
		return nil, ErrInvalidCipher
	}
	keyID := parts[0]
	if !keyIDPattern.MatchString(keyID) {
		return nil, ErrInvalidCipher
	}
	wrappedDEK, err := base64.StdEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, ErrInvalidCipher
	}
	body, err := base64.StdEncoding.DecodeString(parts[2])
	if err != nil || len(body) <= nonceSize {
		return nil, ErrInvalidCipher
	}
	dek, err := e.provider.UnwrapKey(keyID, wrappedDEK)
	if err != nil {
		return nil, err
	}
	defer wipe(dek)

	aead, err := aeadFor(dek)
	if err != nil {
		return nil, err
	}
	nonce, sealed := body[:nonceSize], body[nonceSize:]
	plain, err := aead.Open(nil, nonce, sealed, nil)
	if err != nil {
		return nil, ErrInvalidCipher
	}
	return plain, nil
}

// IsSealed 判断一条落库字符串是否是「信封密文」（v2）。v1 由 settings 单独识别。
func IsSealed(s string) bool { return strings.HasPrefix(s, VersionV2) }

// KeyID 返回密文里携带的 keyID（不做解密）；格式不对返回空串。
func KeyID(cipher string) string {
	if !strings.HasPrefix(cipher, VersionV2) {
		return ""
	}
	rest := strings.TrimPrefix(cipher, VersionV2)
	i := strings.IndexByte(rest, ':')
	if i <= 0 {
		return ""
	}
	id := rest[:i]
	if !keyIDPattern.MatchString(id) {
		return ""
	}
	return id
}

// Rewrap 在当前 provider 下重包一个 v2 密文：数据密文原样保留，只换包裹层。
// 已经是活跃 keyID 时返回 changed=false，调用方不必写库。
//
// v1 密文不能 Rewrap（它没有独立 DEK），要走"解明文 → Seal"的一次性迁移；
// 这条差异由调用方（settings.Store）处理。
func (e *Envelope) Rewrap(cipher string) (out string, changed bool, err error) {
	current := KeyID(cipher)
	if current == "" {
		return "", false, ErrInvalidCipher
	}
	if current == e.provider.ActiveKeyID() {
		return cipher, false, nil
	}
	plain, err := e.Open(cipher)
	if err != nil {
		return "", false, err
	}
	out, err = e.Seal(plain)
	if err != nil {
		return "", false, err
	}
	return out, true, nil
}

func aeadFor(key []byte) (cipher.AEAD, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	return cipher.NewGCM(block)
}

func wipe(b []byte) {
	for i := range b {
		b[i] = 0
	}
}
