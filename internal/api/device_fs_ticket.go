package api

// ── H46（服务端半）：设备桥的连接票据与设备归属证明 ──────────────────────────
//
// 修复前：`GET /ws/device-fs?token=…&device_id=任意非空串` 就是全部授权。
// 后果有两条，都很实在：
//   1. **token 走 query**：URL 会进代理日志、`Referer`、崩溃上报与 shell 历史；
//   2. **device_id 没有归属证明**：任何持有同一用户 token 的进程（含被 XSS 打穿的
//      renderer）都能自称是那台已批准过根目录的设备，从而读到用户在**这台机器**上
//      批准过的目录。
//
// 现在的口径（与 `electron/device-fs.cjs` 逐字段对齐，两侧必须同版本）：
//   · `POST /v1/device-fs/ticket`：Authorization 头带 token，body 带
//     `{device_id, version, nonce, issued_at, signature, public_key, key_id}`；
//     服务端用 `public_key` 验 Ed25519 签名（签名文本见 `deviceProofMessage`），
//     并把 `device_id → 公钥指纹` **首次信任绑定**（TOFU）。之后同一 device_id 换
//     公钥一律拒绝 —— 否则"抢注一个 device_id"就能顶替原设备。
//   · 票据 ≤60s、**一次性**、内存存储（单实例；多副本路由是 S3 的事）。
//   · `GET /ws/device-fs` **只认 `?ticket=`**：带 token 的旧请求一律 401，
//     不再"兼容一下让它过去"—— 那条兼容路径正是缺陷本身。

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/athenavi/chiron/internal/auth"
)

const (
	// deviceProofVersion 与客户端 `PROOF_VERSION` 一致；不一致即拒绝（协议升版）。
	deviceProofVersion = 1
	// deviceTicketTTL 是票据寿命上限（PRD：≤60s）。
	deviceTicketTTL = 60 * time.Second
	// deviceProofSkew 允许的 issued_at 偏差（含时钟漂移）。
	deviceProofSkew = 5 * time.Minute
)

// deviceProofMessage 重建客户端的签名文本（`electron/device-identity.cjs`）。
//
// 两侧必须逐字符一致：多了/少了一个换行就会全部验签失败，而报错只是"签名无效"，
// 排查成本极高。所以客户端也单独导出了 `proofMessage`，并有同口径单测。
func deviceProofMessage(deviceID string, issuedAt int64, nonce string) string {
	return strings.Join([]string{
		fmt.Sprintf("chiron-device-proof/v%d", deviceProofVersion),
		"device_id=" + deviceID,
		fmt.Sprintf("issued_at=%d", issuedAt),
		"nonce=" + nonce,
	}, "\n")
}

// deviceTicketRequest 是 `POST /v1/device-fs/ticket` 的请求体。
type deviceTicketRequest struct {
	DeviceID  string `json:"device_id"`
	Version   int    `json:"version"`
	Nonce     string `json:"nonce"`
	IssuedAt  int64  `json:"issued_at"`
	Signature string `json:"signature"`
	PublicKey string `json:"public_key"`
	KeyID     string `json:"key_id"`
}

// deviceTicket 是一张已签发、未使用的票据。
type deviceTicket struct {
	tenant    string
	user      string
	device    string
	keyID     string
	expiresAt time.Time
	// issuedAt 是票据签发时刻：撤销判定用"撤是否晚于它"，与 token 的 iat 同语义。
	issuedAt time.Time
}

// deviceTicketStore 保存一次性票据与设备公钥绑定（进程内）。
type deviceTicketStore struct {
	mu      sync.Mutex
	tickets map[string]deviceTicket
	// bindings: device_id → keyID（首次信任绑定，防顶替）
	bindings map[string]string
	now      func() time.Time
}

// deviceTicketSingleton 是进程内票据/绑定表（单实例语义与 deviceHub 一致）。
var deviceTicketSingleton = newDeviceTicketStore()

func newDeviceTicketStore() *deviceTicketStore {
	return &deviceTicketStore{
		tickets:  make(map[string]deviceTicket),
		bindings: make(map[string]string),
		now:      time.Now,
	}
}

func deviceTicketKey(tenant, user, device string) string {
	return tenant + "\x00" + user + "\x00" + device
}

// issue 绑定设备公钥并签发一张票据。
func (s *deviceTicketStore) issue(t deviceTicket, ttl time.Duration) (string, time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := s.now()
	// 顺手清理过期票据：票据量很小（每设备每分钟一张），不值得单独起 GC。
	for key, ticket := range s.tickets {
		if !ticket.expiresAt.After(now) {
			delete(s.tickets, key)
		}
	}

	bindingKey := deviceTicketKey(t.tenant, t.user, t.device)
	if bound, ok := s.bindings[bindingKey]; ok && bound != t.keyID {
		// 已绑定的设备换了公钥：这是顶替尝试（或用户重装但没清设备身份）。
		return "", time.Time{}
	}
	s.bindings[bindingKey] = t.keyID

	raw := make([]byte, 24)
	if _, err := readRand(raw); err != nil {
		return "", time.Time{}
	}
	value := base64.RawURLEncoding.EncodeToString(raw)
	t.issuedAt = now
	t.expiresAt = now.Add(ttl)
	s.tickets[value] = t
	return value, t.expiresAt
}

// consume 取出并作废票据（一次性）。返回 ok=false 表示无效/过期/已用过。
func (s *deviceTicketStore) consume(value string) (deviceTicket, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	ticket, ok := s.tickets[value]
	if !ok {
		return deviceTicket{}, false
	}
	delete(s.tickets, value)
	if !ticket.expiresAt.After(s.now()) {
		return deviceTicket{}, false
	}
	return ticket, true
}

// boundKeyID 返回设备已绑定的公钥指纹（无绑定返回空串）。
func (s *deviceTicketStore) boundKeyID(tenant, user, device string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.bindings[deviceTicketKey(tenant, user, device)]
}

// verifyDeviceProof 校验 Ed25519 归属证明。返回 `(keyID, err)`。
func verifyDeviceProof(req deviceTicketRequest, now time.Time) (string, error) {
	if req.Version != deviceProofVersion {
		return "", fmt.Errorf("unsupported device proof version %d", req.Version)
	}
	deviceID := strings.TrimSpace(req.DeviceID)
	if deviceID == "" {
		return "", fmt.Errorf("device_id required")
	}
	if strings.TrimSpace(req.Nonce) == "" {
		return "", fmt.Errorf("device proof nonce required")
	}
	if req.IssuedAt <= 0 {
		return "", fmt.Errorf("device proof issued_at required")
	}
	drift := now.Sub(time.UnixMilli(req.IssuedAt))
	if drift > deviceProofSkew || drift < -deviceProofSkew {
		// 允许一定偏差，但拒绝明显过期/来自未来的证明（重放窗口）。
		return "", fmt.Errorf("device proof issued_at out of window")
	}
	pub, err := decodeEd25519PublicKey(req.PublicKey)
	if err != nil {
		return "", err
	}
	sig, err := base64.StdEncoding.DecodeString(strings.TrimSpace(req.Signature))
	if err != nil {
		return "", fmt.Errorf("device proof signature is not base64")
	}
	if len(sig) != ed25519.SignatureSize {
		return "", fmt.Errorf("device proof signature has wrong length")
	}
	msg := deviceProofMessage(deviceID, req.IssuedAt, req.Nonce)
	if !ed25519.Verify(pub, []byte(msg), sig) {
		return "", fmt.Errorf("device proof signature mismatch")
	}

	keyID := strings.TrimSpace(req.KeyID)
	want := keyIDOf(pub)
	if keyID != "" && keyID != want {
		// 客户端自报的 key_id 与公钥不符：要么客户端算错，要么有人在拼字段。
		return "", fmt.Errorf("device proof key_id does not match public key")
	}
	return want, nil
}

// decodeEd25519PublicKey 接受 SPKI DER 的 base64（与客户端导出一致）。
func decodeEd25519PublicKey(encoded string) (ed25519.PublicKey, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(encoded))
	if err != nil {
		return nil, fmt.Errorf("device public_key is not base64")
	}
	// Ed25519 SPKI 的前 12 字节是固定头 `302a300506032b6570032100`。
	const spkiPrefixLen = 12
	if len(raw) != spkiPrefixLen+ed25519.PublicKeySize {
		return nil, fmt.Errorf("device public_key has wrong length")
	}
	if hex.EncodeToString(raw[:spkiPrefixLen]) != "302a300506032b6570032100" {
		return nil, fmt.Errorf("device public_key is not an ed25519 spki key")
	}
	return ed25519.PublicKey(raw[spkiPrefixLen:]), nil
}

// keyIDOf 与客户端 `describeIdentity().keyId` 同口径：SPKI sha256 前 32 个 hex。
func keyIDOf(pub ed25519.PublicKey) string {
	spki := append([]byte{0x30, 0x2a, 0x30, 0x05, 0x06, 0x03, 0x2b, 0x65, 0x70, 0x03, 0x21, 0x00}, pub...)
	sum := sha256.Sum256(spki)
	return hex.EncodeToString(sum[:16])
}

// DeviceFSTicketHandler 是 `POST /v1/device-fs/ticket`。
func DeviceFSTicketHandler(store *deviceTicketStore, authenticator *auth.Authenticator) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if store == nil || authenticator == nil {
			JSON(w, http.StatusServiceUnavailable, APIResponse{Success: false, Error: "device bridge unavailable"})
			return
		}
		token := bearerToken(r)
		if token == "" {
			Unauthorized(w, "missing token")
			return
		}
		claims, err := authenticator.ValidateToken(token)
		if err != nil {
			Unauthorized(w, err.Error())
			return
		}
		var body deviceTicketRequest
		if err := DecodeJSON(w, r, &body); err != nil {
			BadRequest(w, ErrInvalidReq)
			return
		}
		body.DeviceID = strings.TrimSpace(body.DeviceID)
		if body.DeviceID == "" {
			BadRequest(w, "device_id required")
			return
		}

		keyID, err := verifyDeviceProof(body, time.Now())
		if err != nil {
			// 401：证明不成立就不是"这台设备"，与 token 无效同级处理。
			slog.Warn("device ticket proof rejected", "device", body.DeviceID, "error", err)
			Unauthorized(w, "device proof rejected")
			return
		}

		ticket, expiresAt := store.issue(deviceTicket{
			tenant: smTenant(claims),
			user:   claims.UserID,
			device: body.DeviceID,
			keyID:  keyID,
		}, deviceTicketTTL)
		if ticket == "" {
			// 绑定冲突：同一 device_id 换了公钥。
			JSON(w, http.StatusForbidden, APIResponse{
				Success: false, Code: CodeForbidden,
				Error: "device key does not match the registered device identity",
			})
			return
		}
		OK(w, map[string]interface{}{
			"ticket":     ticket,
			"expires_in": int(deviceTicketTTL.Seconds()),
			"expires_at": expiresAt.Unix(),
			"protocol":   deviceFSProtocolVersion,
		})
	}
}

// readRand 便于测试注入（默认走 crypto/rand）。
var readRand = rand.Read

// bearerToken 从 Authorization 头取 Bearer token（不回退到 query）。
func bearerToken(r *http.Request) string {
	raw := strings.TrimSpace(r.Header.Get("Authorization"))
	if raw == "" {
		return ""
	}
	if idx := strings.IndexByte(raw, ' '); idx > 0 {
		if strings.EqualFold(raw[:idx], "bearer") {
			return strings.TrimSpace(raw[idx+1:])
		}
		return ""
	}
	return ""
}
