package settings

import (
	"encoding/base64"
	"strings"
	"sync"
	"testing"

	"github.com/athenavi/chiron/internal/kms"
)

// H20：新写入必须是 v2 信封，且密文里看不到明文。
func TestStoreEncryptStringUsesEnvelope(t *testing.T) {
	t.Setenv("KMS_KEK_KEYS", "")
	t.Setenv("KMS_KEK_KEYS_FILE", "")
	t.Setenv("KMS_ACTIVE_KEY_ID", "")

	s := New(nil, "deploy-secret-for-tests")
	const plain = "sk-provider-key-should-never-appear"
	enc, err := s.EncryptString(plain)
	if err != nil {
		t.Fatalf("EncryptString: %v", err)
	}
	if !strings.HasPrefix(enc, kms.VersionV2) {
		t.Fatalf("cipher = %q, want v2 envelope", enc)
	}
	if strings.Contains(enc, plain) {
		t.Fatal("密文包含明文")
	}
	got, err := s.DecryptString(enc)
	if err != nil {
		t.Fatalf("DecryptString: %v", err)
	}
	if got != plain {
		t.Fatalf("roundtrip = %q", got)
	}
	if s.KMSKeyID() != kms.AppSecretKeyID {
		t.Fatalf("KMSKeyID = %q", s.KMSKeyID())
	}
}

// 存量 v1 必须继续可读，且 RewrapCipher 能把它升级成 v2。
func TestStoreRewrapLegacyV1(t *testing.T) {
	t.Setenv("KMS_KEK_KEYS", "")
	t.Setenv("KMS_KEK_KEYS_FILE", "")
	t.Setenv("KMS_ACTIVE_KEY_ID", "")

	s := New(nil, "deploy-secret-for-tests")
	v1, err := s.encryptV1(`"sk-legacy"`)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(v1, "v1:") {
		t.Fatalf("expected v1, got %q", v1)
	}
	next, changed, err := s.RewrapCipher(v1)
	if err != nil {
		t.Fatalf("RewrapCipher: %v", err)
	}
	if !changed || !strings.HasPrefix(next, kms.VersionV2) {
		t.Fatalf("changed=%v next=%q", changed, next)
	}
	plain, err := s.DecryptString(next)
	if err != nil || plain != `"sk-legacy"` {
		t.Fatalf("decrypt rewrapped = %q, %v", plain, err)
	}
	// 已是最新 keyID ⇒ 不再变化（幂等，轮换可重复执行）。
	if _, changed, err := s.RewrapCipher(next); err != nil || changed {
		t.Fatalf("idempotent rewrap: changed=%v err=%v", changed, err)
	}
}

// 轮换到新 KEK：旧 keyID 的密文被重包，旧 KEK 删除后仍可解。
func TestStoreRewrapToNewKEK(t *testing.T) {
	oldKEK := base64.StdEncoding.EncodeToString(bytesN(1))
	oldStore := storeWithKEK(t, "old", oldKEK)
	enc, err := oldStore.EncryptString(`"sk-rotating"`)
	if err != nil {
		t.Fatal(err)
	}

	newKEK := base64.StdEncoding.EncodeToString(bytesN(2))
	newStore := storeWithKEKWithOld(t, "new", newKEK, "old", oldKEK)
	next, changed, err := newStore.RewrapCipher(enc)
	if err != nil {
		t.Fatalf("RewrapCipher: %v", err)
	}
	if !changed || kms.KeyID(next) != "new" {
		t.Fatalf("changed=%v keyID=%q", changed, kms.KeyID(next))
	}

	// 只保留新 KEK（模拟删掉旧材料）。
	onlyNew := storeWithKEK(t, "new", newKEK)
	plain, err := onlyNew.DecryptString(next)
	if err != nil || plain != `"sk-rotating"` {
		t.Fatalf("decrypt after rotation = %q, %v", plain, err)
	}
	if _, err := onlyNew.DecryptString(enc); err == nil {
		t.Fatal("旧 KEK 删除后旧密文仍能解开")
	}
}

func TestStoreEncryptUnavailableWithoutSecret(t *testing.T) {
	t.Setenv("KMS_KEK_KEYS", "")
	t.Setenv("KMS_KEK_KEYS_FILE", "")
	t.Setenv("KMS_ACTIVE_KEY_ID", "")
	s := New(nil, "")
	if s.EncryptEnabled() {
		t.Fatal("无密钥时 EncryptEnabled 为 true")
	}
	if _, err := s.EncryptString("x"); err == nil {
		t.Fatal("无密钥时仍然加密成功")
	}
}

func TestEnvelopeConcurrentUse(t *testing.T) {
	t.Setenv("KMS_KEK_KEYS", "")
	t.Setenv("KMS_KEK_KEYS_FILE", "")
	t.Setenv("KMS_ACTIVE_KEY_ID", "")
	s := New(nil, "concurrent-secret")
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 20; j++ {
				enc, err := s.EncryptString("v")
				if err != nil {
					t.Errorf("EncryptString: %v", err)
					return
				}
				if got, err := s.DecryptString(enc); err != nil || got != "v" {
					t.Errorf("roundtrip = %q, %v", got, err)
					return
				}
			}
		}()
	}
	wg.Wait()
}

func bytesN(b byte) []byte {
	out := make([]byte, kms.DataKeySize)
	for i := range out {
		out[i] = b
	}
	return out
}

func storeWithKEK(t *testing.T, id, b64 string) *Store {
	t.Helper()
	return storeWithKEKWithOld(t, id, b64, "", "")
}

func storeWithKEKWithOld(t *testing.T, id, b64, oldID, oldB64 string) *Store {
	t.Helper()
	keks := map[string][]byte{}
	for key, val := range map[string]string{id: b64, oldID: oldB64} {
		if key == "" {
			continue
		}
		raw, err := base64.StdEncoding.DecodeString(val)
		if err != nil {
			t.Fatal(err)
		}
		keks[key] = raw
	}
	provider, err := kms.NewLocalWithKEKs(id, keks)
	if err != nil {
		t.Fatal(err)
	}
	return &Store{env: kms.NewEnvelope(provider)}
}
