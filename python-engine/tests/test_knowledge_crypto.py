"""P1 数据层验收：用户级密钥加解密 + 原文对象存储。

跨语言向量 ``CIPHER`` 由固定 nonce（00..0b）与固定 ``APP_SECRET`` 生成；Go 侧
``internal/settings`` 必须能解出同一明文 —— 这条断言是"两侧算法不漂移"的锚点。
"""

from __future__ import annotations

import base64

import pytest

from app.knowledge import blob, crypto

SECRET = "chiron-test-app-secret-0123456789"
PLAIN = "sk-pageindex-test-abcdef"
CIPHER = (
    "v1:AAECAwQFBgcICQoL6/ZxeTz6+PTHhr3MZtxut3G2CIPpCKIhjGyCm0g7mY8pCcqxi5kyHg=="
)


# ── crypto ──


def test_roundtrip() -> None:
    encrypted = crypto.encrypt_secret(PLAIN, SECRET)
    assert encrypted.startswith("v1:")
    assert crypto.decrypt_secret(encrypted, SECRET) == PLAIN


def test_go_compatible_vector() -> None:
    assert crypto.decrypt_secret(CIPHER, SECRET) == PLAIN


def test_wrong_secret_fails() -> None:
    encrypted = crypto.encrypt_secret(PLAIN, SECRET)
    with pytest.raises(crypto.SecretCryptoError):
        crypto.decrypt_secret(encrypted, "another-secret")


def test_tampered_ciphertext_fails() -> None:
    encrypted = crypto.encrypt_secret(PLAIN, SECRET)
    raw = bytearray(base64.b64decode(encrypted[len(crypto.CIPHER_VERSION):]))
    raw[-1] ^= 0xFF
    tampered = crypto.CIPHER_VERSION + base64.b64encode(bytes(raw)).decode()
    with pytest.raises(crypto.SecretCryptoError):
        crypto.decrypt_secret(tampered, SECRET)


def test_missing_secret_is_rejected() -> None:
    assert crypto.encryption_available("") is False
    with pytest.raises(crypto.SecretCryptoError):
        crypto.encrypt_secret(PLAIN, "")


def test_fingerprint_is_stable_and_short() -> None:
    assert crypto.key_fingerprint(PLAIN) == crypto.key_fingerprint(PLAIN)
    assert len(crypto.key_fingerprint(PLAIN)) == 16
    assert crypto.key_fingerprint(PLAIN) != PLAIN


# ── H20：信封加密（v2） ──

#: 由 Go `internal/kms` 生成（keyID=vector，KEK=base64(00..1f)，明文 sk-vector-plaintext）。
#: 两条语言各自的固定向量互解，是"轮换后的密文引擎读得开"的唯一无真机证明。
V2_KEK_B64 = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
V2_CIPHER = (
    "v2:vector:EE3SaVsJ7Miw+oC36zjvAcVHWOB2MOStqPpWkt66CErmrk0P8bj4buMnLxPxvEb2"
    "qWKZYkmz0IHKxqCB:2A/1PLYYgENM3wuxFFwwCOSWxLP0mf/9zHHTP12QGiMVMGnnqNT6a6"
    "sJwKhQu+Y="
)
V2_PLAIN = "sk-vector-plaintext"


def test_envelope_go_vector(monkeypatch) -> None:
    monkeypatch.setenv("KMS_KEK_KEYS", '{"vector":"' + V2_KEK_B64 + '"}')
    monkeypatch.delenv("KMS_KEK_KEYS_FILE", raising=False)
    assert crypto.decrypt_secret(V2_CIPHER, "") == V2_PLAIN


def test_envelope_unknown_key_id_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv("KMS_KEK_KEYS", raising=False)
    monkeypatch.delenv("KMS_KEK_KEYS_FILE", raising=False)
    # 没有任何 KEK 时不得把密文当明文返回。
    with pytest.raises(crypto.SecretCryptoError):
        crypto.decrypt_secret(V2_CIPHER, "")


def test_envelope_app_secret_fallback(monkeypatch) -> None:
    """不配 KMS_KEK_KEYS 时，KEK 由 APP_SECRET 派生（老部署零改动）。"""
    monkeypatch.delenv("KMS_KEK_KEYS", raising=False)
    monkeypatch.delenv("KMS_KEK_KEYS_FILE", raising=False)
    cipher = crypto.encrypt_secret(PLAIN, SECRET)
    assert cipher.startswith("v1:")  # 写入仍是 v1；读取 v2 的兜底只用于网关写入的值
    # 用 Go 侧派生规则构造的 v2 密文：Python 必须用同一把 fallback KEK 解开。
    # 这里只验证 KEK 集合里有 app-secret-v1（向量见 internal/kms 的跨语言测试）。
    assert crypto.APP_SECRET_KEY_ID in crypto._kek_material(SECRET)


# ── blob ──


def test_local_blob_roundtrip(tmp_path) -> None:
    store = blob.LocalBlobStore(str(tmp_path))
    key = blob.build_key("t1", "kb1", "doc1", "a b/c?.pdf")
    store.put(key, b"hello", "application/pdf")
    assert store.exists(key)
    assert store.get(key) == b"hello"
    store.delete(key)
    assert not store.exists(key)


@pytest.mark.parametrize("bad_key", ["../x", "/abs/path", "a/../../b", ""])
def test_blob_key_rejects_traversal(tmp_path, bad_key: str) -> None:
    store = blob.LocalBlobStore(str(tmp_path))
    with pytest.raises(ValueError):
        store.get(bad_key)


def test_build_key_sanitizes_filename() -> None:
    key = blob.build_key("t1", "kb1", "doc1", "..\\evil name.pdf")
    assert ".." not in key
    assert " " not in key
    assert key.startswith("t1/kb1/doc1/")


def test_blob_backend_survives_empty_env(monkeypatch, tmp_path) -> None:
    """compose 用 `${VAR:-}` 注入的是**空串**（键存在）：回退不能被空串吃掉。

    ``os.getenv("K", default)`` 只在键不存在时用 default —— 空串会让
    "knowledge 未配 → 跟随 media → local" 这条链断在第一环，静默落到 local。
    """
    monkeypatch.setenv("KNOWLEDGE_BLOB_BACKEND", "")
    monkeypatch.setenv("MEDIA_STORE_BACKEND", "")
    monkeypatch.setenv("KNOWLEDGE_LOCAL_PATH", str(tmp_path))
    store = blob.create_blob_store()
    assert isinstance(store, blob.LocalBlobStore)


def test_blob_backend_follows_media_backend(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KNOWLEDGE_BLOB_BACKEND", "")
    monkeypatch.setenv("MEDIA_STORE_BACKEND", "local")
    monkeypatch.setenv("KNOWLEDGE_LOCAL_PATH", str(tmp_path))
    store = blob.create_blob_store()
    assert isinstance(store, blob.LocalBlobStore)
    assert tmp_path.exists()
