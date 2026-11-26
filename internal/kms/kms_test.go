package kms

import (
	"encoding/base64"
	"strings"
	"testing"
)

func key32(b byte) []byte {
	out := make([]byte, DataKeySize)
	for i := range out {
		out[i] = b
	}
	return out
}

func localWithKeys(t *testing.T, active string, keks map[string][]byte) *localProvider {
	t.Helper()
	if _, ok := keks[active]; !ok {
		t.Fatalf("active %q not in keks", active)
	}
	return &localProvider{active: active, keks: keks}
}

func TestEnvelopeRoundTripAndFreshDEK(t *testing.T) {
	env := NewEnvelope(localWithKeys(t, "k1", map[string][]byte{"k1": key32(1)}))
	const plain = "sk-live-very-secret-0123456789"

	c1, err := env.Seal([]byte(plain))
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	c2, err := env.Seal([]byte(plain))
	if err != nil {
		t.Fatalf("Seal: %v", err)
	}
	if c1 == c2 {
		t.Fatal("两次密封同一明文得到相同密文：DEK 没有每次新生成")
	}
	// "明文永不落盘"的最小断言：密文不包含明文、也不是直接 base64 明文。
	if strings.Contains(c1, plain) || strings.Contains(c1, base64.StdEncoding.EncodeToString([]byte(plain))) {
		t.Fatalf("密文包含明文: %s", c1)
	}
	if KeyID(c1) != "k1" {
		t.Fatalf("KeyID = %q, want k1", KeyID(c1))
	}
	got, err := env.Open(c1)
	if err != nil {
		t.Fatalf("Open: %v", err)
	}
	if string(got) != plain {
		t.Fatalf("Open = %q", got)
	}
}

func TestEnvelopeTamperFailsClosed(t *testing.T) {
	env := NewEnvelope(localWithKeys(t, "k1", map[string][]byte{"k1": key32(2)}))
	c, err := env.Seal([]byte("secret"))
	if err != nil {
		t.Fatal(err)
	}
	// 改动数据段最后一个字符必然破坏 GCM tag。
	last := c[len(c)-1]
	repl := byte('A')
	if last == 'A' {
		repl = 'B'
	}
	if _, err := env.Open(c[:len(c)-1] + string(repl)); err == nil {
		t.Fatal("被篡改的密文仍然解开")
	}
	// 未知 keyID 必须报错，不能静默换一把 key。
	if _, err := env.Open("v2:unknown:" + strings.SplitN(strings.TrimPrefix(c, "v2:"), ":", 3)[1] + ":AAAA"); err == nil {
		t.Fatal("未知 keyID 仍然解开")
	}
}

func TestRewrapKeepsDataCipherAndSwitchesKEK(t *testing.T) {
	oldEntry := map[string][]byte{"old": key32(3), "new": key32(4)}
	// 先用 old 封。
	oldEnv := NewEnvelope(localWithKeys(t, "old", oldEntry))
	c, err := oldEnv.Seal([]byte("provider-key"))
	if err != nil {
		t.Fatal(err)
	}
	if oldEnv.ActiveKeyID() != "old" {
		t.Fatalf("active = %q", oldEnv.ActiveKeyID())
	}

	// 切到 new 后重包。
	newEnv := NewEnvelope(localWithKeys(t, "new", oldEntry))
	rewrapped, changed, err := newEnv.Rewrap(c)
	if err != nil {
		t.Fatalf("Rewrap: %v", err)
	}
	if !changed || KeyID(rewrapped) != "new" {
		t.Fatalf("changed=%v keyID=%q", changed, KeyID(rewrapped))
	}
	got, err := newEnv.Open(rewrapped)
	if err != nil || string(got) != "provider-key" {
		t.Fatalf("Open(rewrapped) = %q, %v", got, err)
	}

	// 只保留 new（模拟运维删掉旧 KEK）：新密文仍可解；旧密文必须失败。
	onlyNew := NewEnvelope(localWithKeys(t, "new", map[string][]byte{"new": key32(4)}))
	if _, err := onlyNew.Open(rewrapped); err != nil {
		t.Fatalf("新密文在只有 new 时解不开: %v", err)
	}
	if _, err := onlyNew.Open(c); err == nil {
		t.Fatal("旧 keyID 的密文在 old 被删除后仍能解开")
	}
}

func TestLocalProviderFromEnvAndFallback(t *testing.T) {
	t.Run("app-secret 兜底", func(t *testing.T) {
		t.Setenv("KMS_KEK_KEYS", "")
		t.Setenv("KMS_KEK_KEYS_FILE", "")
		t.Setenv("KMS_ACTIVE_KEY_ID", "")
		p, err := NewLocal("deploy-secret")
		if err != nil {
			t.Fatalf("NewLocal: %v", err)
		}
		if p.ActiveKeyID() != AppSecretKeyID {
			t.Fatalf("active = %q", p.ActiveKeyID())
		}
	})
	t.Run("显式 KEK", func(t *testing.T) {
		t.Setenv("KMS_KEK_KEYS", `{"v1":"`+base64.StdEncoding.EncodeToString(key32(9))+`"}`)
		t.Setenv("KMS_ACTIVE_KEY_ID", "v1")
		p, err := NewLocal("")
		if err != nil {
			t.Fatalf("NewLocal: %v", err)
		}
		if p.ActiveKeyID() != "v1" {
			t.Fatalf("active = %q", p.ActiveKeyID())
		}
	})
	t.Run("非法 key 长度", func(t *testing.T) {
		t.Setenv("KMS_KEK_KEYS", `{"v1":"`+base64.StdEncoding.EncodeToString([]byte("short"))+`"}`)
		if _, err := NewLocal("x"); err == nil {
			t.Fatal("短 KEK 被接受")
		}
	})
	t.Run("多 KEK 未指定 active", func(t *testing.T) {
		t.Setenv("KMS_KEK_KEYS", `{"a":"`+base64.StdEncoding.EncodeToString(key32(1))+`","b":"`+base64.StdEncoding.EncodeToString(key32(2))+`"}`)
		t.Setenv("KMS_ACTIVE_KEY_ID", "")
		if _, err := NewLocal("x"); err == nil {
			t.Fatal("多 KEK 且无 active 时未报错")
		}
	})
	t.Run("无材料", func(t *testing.T) {
		t.Setenv("KMS_KEK_KEYS", "")
		t.Setenv("KMS_KEK_KEYS_FILE", "")
		t.Setenv("KMS_ACTIVE_KEY_ID", "")
		if _, err := NewLocal(""); err == nil {
			t.Fatal("没有任何 KEK 却构造成功")
		}
	})
}

// 固定向量：由 Go 生成、Python 侧 test_knowledge_crypto.py 解同一串。
// KEK = base64(00..1f)，keyID = "vector"，明文 = "sk-vector-plaintext"。
func TestEnvelopeCrossLanguageVector(t *testing.T) {
	keks := map[string][]byte{"vector": func() []byte {
		b := make([]byte, DataKeySize)
		for i := range b {
			b[i] = byte(i)
		}
		return b
	}()}
	env := NewEnvelope(localWithKeys(t, "vector", keks))
	cipher := "v2:vector:EE3SaVsJ7Miw+oC36zjvAcVHWOB2MOStqPpWkt66CErmrk0P8bj4buMnLxPxvEb2qWKZYkmz0IHKxqCB:2A/1PLYYgENM3wuxFFwwCOSWxLP0mf/9zHHTP12QGiMVMGnnqNT6a6sJwKhQu+Y="
	got, err := env.Open(cipher)
	if err != nil {
		t.Fatalf("固定向量解不开（Go 侧格式漂移？）: %v", err)
	}
	if string(got) != "sk-vector-plaintext" {
		t.Fatalf("got %q", got)
	}
}
