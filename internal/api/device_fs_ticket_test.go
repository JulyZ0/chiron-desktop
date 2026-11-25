package api

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

// ── H46（服务端半）：设备票据 + 归属证明 + WS 只认票据 ────────────────────────
//
// 修复前 `GET /ws/device-fs?token=…&device_id=任意非空串` 就是全部授权：
//   · token 走 query（进代理日志/Referer/崩溃上报）；
//   · device_id 没有归属证明（任何拿到同一用户 token 的进程都能自称那台设备）。
//
// 这一组覆盖：证明文本一致性、票据一次性与 TTL、换公钥被拒、旧 query token 被拒。

func newDeviceIdentity(t *testing.T) (ed25519.PrivateKey, string, string) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatalf("keygen: %v", err)
	}
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	return priv, base64.StdEncoding.EncodeToString(der), keyIDOf(pub)
}

func signedProof(deviceID string, priv ed25519.PrivateKey, pubKey, keyID string) deviceTicketRequest {
	issuedAt := time.Now().UnixMilli()
	nonce := "b1946ac92492d2347c6235b4d2611184"
	sig := ed25519.Sign(priv, []byte(deviceProofMessage(deviceID, issuedAt, nonce)))
	return deviceTicketRequest{
		DeviceID:  deviceID,
		Version:   deviceProofVersion,
		Nonce:     nonce,
		IssuedAt:  issuedAt,
		Signature: base64.StdEncoding.EncodeToString(sig),
		PublicKey: pubKey,
		KeyID:     keyID,
	}
}

func TestDeviceProofMessageMatchesClientFormat(t *testing.T) {
	// 客户端（electron/device-identity.cjs）的格式：4 行、LF 分隔、固定前缀。
	got := deviceProofMessage("dev-1", 1735689600000, "abc")
	want := "chiron-device-proof/v1\ndevice_id=dev-1\nissued_at=1735689600000\nnonce=abc"
	if got != want {
		t.Fatalf("证明文本与客户端不一致：\n got=%q\nwant=%q", got, want)
	}
}

func TestDeviceProofRejectsTampering(t *testing.T) {
	priv, pubB64, keyID := newDeviceIdentity(t)
	proof := signedProof("dev-1", priv, pubB64, keyID)

	if _, err := verifyDeviceProof(proof, time.Now()); err != nil {
		t.Fatalf("合法证明应通过: %v", err)
	}

	cases := []struct {
		name   string
		mutate func(p *deviceTicketRequest)
	}{
		{"换 device_id", func(p *deviceTicketRequest) { p.DeviceID = "dev-2" }},
		{"改 issued_at", func(p *deviceTicketRequest) { p.IssuedAt += 1000 }},
		{"改 nonce", func(p *deviceTicketRequest) { p.Nonce = "other" }},
		{"签名被改", func(p *deviceTicketRequest) {
			raw, _ := base64.StdEncoding.DecodeString(p.Signature)
			raw[0] ^= 0xff
			p.Signature = base64.StdEncoding.EncodeToString(raw)
		}},
		{"key_id 与公钥不符", func(p *deviceTicketRequest) { p.KeyID = strings.Repeat("0", 32) }},
		{"版本不一致", func(p *deviceTicketRequest) { p.Version = 99 }},
		{"公钥不是 ed25519 spki", func(p *deviceTicketRequest) {
			p.PublicKey = base64.StdEncoding.EncodeToString(make([]byte, 44))
		}},
	}
	for _, c := range cases {
		p := proof
		c.mutate(&p)
		if _, err := verifyDeviceProof(p, time.Now()); err == nil {
			t.Fatalf("%s 之后仍被认为有效", c.name)
		}
	}
}

func TestDeviceProofRejectsStaleIssuedAt(t *testing.T) {
	priv, pubB64, keyID := newDeviceIdentity(t)
	proof := signedProof("dev-1", priv, pubB64, keyID)
	// issued_at 在 10 分钟前 ⇒ 超出 ±5 分钟窗口（重放防护）
	proof.IssuedAt = time.Now().Add(-10 * time.Minute).UnixMilli()
	proof.Signature = base64.StdEncoding.EncodeToString(
		ed25519.Sign(priv, []byte(deviceProofMessage(proof.DeviceID, proof.IssuedAt, proof.Nonce))),
	)
	if _, err := verifyDeviceProof(proof, time.Now()); err == nil {
		t.Fatal("过期的证明必须拒绝")
	}
}

func TestDeviceTicketIsSingleUseAndExpires(t *testing.T) {
	store := newDeviceTicketStore()
	now := time.Now()
	store.now = func() time.Time { return now }

	ticket, expires := store.issue(deviceTicket{tenant: "1", user: "u1", device: "dev-1", keyID: "k"}, time.Minute)
	if ticket == "" || !expires.After(now) {
		t.Fatalf("应签发票据：%q %v", ticket, expires)
	}
	if _, ok := store.consume(ticket); !ok {
		t.Fatal("首次消费应成功")
	}
	if _, ok := store.consume(ticket); ok {
		t.Fatal("票据必须一次性：第二次消费应失败")
	}

	ticket2, _ := store.issue(deviceTicket{tenant: "1", user: "u1", device: "dev-1", keyID: "k"}, time.Minute)
	now = now.Add(2 * time.Minute)
	if _, ok := store.consume(ticket2); ok {
		t.Fatal("过期票据必须拒绝")
	}
}

func TestDeviceTicketRejectsKeySwapForSameDevice(t *testing.T) {
	store := newDeviceTicketStore()
	if _, _ = store.issue(deviceTicket{tenant: "1", user: "u1", device: "dev-1", keyID: "key-a"}, time.Minute); false {
		t.Fatal("unreachable")
	}
	// 同一 device_id 换了公钥指纹：必须拒绝（防顶替原设备）
	if ticket, _ := store.issue(deviceTicket{tenant: "1", user: "u1", device: "dev-1", keyID: "key-b"}, time.Minute); ticket != "" {
		t.Fatal("换公钥后不应签发票据")
	}
	// 不同用户/设备互不影响
	if ticket, _ := store.issue(deviceTicket{tenant: "1", user: "u2", device: "dev-1", keyID: "key-b"}, time.Minute); ticket == "" {
		t.Fatal("不同用户的同名 device_id 应各自独立")
	}
}

func TestDeviceFSTicketHandlerRejectsBadProof(t *testing.T) {
	store := newDeviceTicketStore()
	h := DeviceFSTicketHandler(store, nil)
	rec := httptest.NewRecorder()
	h(rec, httptest.NewRequest(http.MethodPost, "/v1/device-fs/ticket", strings.NewReader("{}")))
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("没有 authenticator 时应 503，得到 %d", rec.Code)
	}
}

// WS 侧：带 token 的旧请求必须 401（这条兼容路径就是缺陷本身）
func TestDeviceFSHandlerRejectsLegacyQueryToken(t *testing.T) {
	store := newDeviceTicketStore()
	h := DeviceFSHandler(newDeviceHub(), store)

	for _, target := range []string{
		"/ws/device-fs?token=leaked-in-url&device_id=dev-1",
		"/ws/device-fs?device_id=dev-1",
		"/ws/device-fs?ticket=not-a-real-ticket",
	} {
		rec := httptest.NewRecorder()
		h(rec, httptest.NewRequest(http.MethodGet, target, nil))
		if rec.Code != http.StatusUnauthorized {
			t.Fatalf("%s 应 401，得到 %d", target, rec.Code)
		}
		var body map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &body)
		if !strings.Contains(rec.Body.String(), "ticket") && !strings.Contains(rec.Body.String(), "token") {
			t.Fatalf("%s 的错误信息应说明凭据问题：%s", target, rec.Body.String())
		}
	}
}

func TestBearerTokenOnlyFromHeader(t *testing.T) {
	req := httptest.NewRequest(http.MethodPost, "/x", nil)
	if bearerToken(req) != "" {
		t.Fatal("没有头时应为空")
	}
	req.Header.Set("Authorization", "Bearer abc")
	if bearerToken(req) != "abc" {
		t.Fatal("应取出 Bearer token")
	}
	req.Header.Set("Authorization", "Basic abc")
	if bearerToken(req) != "" {
		t.Fatal("非 Bearer 方案不应被接受")
	}
}
