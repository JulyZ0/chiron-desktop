"""知识库用户级密钥的加解密 —— 与 Go 侧 ``internal/settings`` / ``internal/kms`` 字节级同构。

为什么必须同构、不能各写各的:用户级 PageIndex Cloud key 由引擎（FastAPI）读写，而
``APP_SECRET`` 派生的 AEAD 是网关侧 ``internal/settings`` 与 ``internal/auth`` 已在用的
密钥体系。如果两侧算法有任何一处不同（派生域、nonce 长度、base64 变体、拼接顺序），
"网关写入、引擎读取"就会静默解不开 —— 而这类失败只会在用户保存密钥之后才暴露。

两种密文都支持：

* ``v1:`` —— 每条值直接用主密钥派生 AEAD 加密（历史格式，仍可读）；
* ``v2:`` —— **信封加密**（H20）：``v2:<keyID>:<base64(nonce||wrappedDEK)>:
  <base64(nonce||ciphertext)>``。数据密钥（DEK）每条随机，KEK 来自 ``KMS_KEK_KEYS``
  或由 ``APP_SECRET`` 派生（keyID ``app-secret-v1``）。

v1 的算法逐条对齐 ``internal/settings/settings.go``：

* 密钥 = ``SHA-256(b"chiron-settings:" + APP_SECRET)``；
* ``AES-256-GCM``，nonce **12 字节**随机；
* 密文串 = ``"v1:" + base64(nonce || ciphertext || tag)``（标准字母表 + padding）。

``APP_SECRET`` 为空时**不**回退到任何默认值：调用方必须显式判断
``encryption_available()``，然后拒绝保存密钥（与 Go 侧 ``EncryptEnabled()`` 一致），
否则会用一把空密钥把密文写进库。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CIPHER_VERSION = "v1:"
V2_VERSION = "v2:"
NONCE_SIZE = 12
DATA_KEY_SIZE = 32
#: 零配置部署的 KEK 标识：由 APP_SECRET 派生，与 Go 侧 kms.AppSecretKeyID 一致。
APP_SECRET_KEY_ID = "app-secret-v1"

# 与 Go 侧 internal/settings 的派生域保持一致；不要改成 "chiron-encrypt:" ——
# 那是 internal/auth 给 SSO/短信凭据用的域，换了就解不开另一侧的密文。
_KEY_DOMAIN = b"chiron-settings:"
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SecretCryptoError(Exception):
    """密钥不可用、密文损坏或密钥不匹配。消息里不携带明文。"""


def encryption_available(app_secret: str) -> bool:
    """部署主密钥是否可用于加解密。"""
    return bool(app_secret)


def derive_key(app_secret: str) -> bytes:
    """由部署主密钥派生 32 字节 AES-256 密钥。"""
    if not app_secret:
        raise SecretCryptoError("APP_SECRET is not configured")
    return hashlib.sha256(_KEY_DOMAIN + app_secret.encode("utf-8")).digest()


def encrypt_secret(plaintext: str, app_secret: str) -> str:
    """加密为可落库的 ``v1:`` 密文串。"""
    key = derive_key(app_secret)
    nonce = os.urandom(NONCE_SIZE)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return CIPHER_VERSION + base64.b64encode(nonce + sealed).decode("ascii")


def decrypt_secret(ciphertext: str, app_secret: str) -> str:
    """解密 ``encrypt_secret`` / Go ``settings.EncryptString`` 产生的密文（v1 / v2）。"""
    if isinstance(ciphertext, str) and ciphertext.startswith(V2_VERSION):
        return decrypt_envelope(ciphertext, app_secret)
    return decrypt_v1(ciphertext, app_secret)


def decrypt_v1(ciphertext: str, app_secret: str) -> str:
    """解密 ``v1:`` 密文（历史格式）。"""
    key = derive_key(app_secret)
    if not isinstance(ciphertext, str) or not ciphertext.startswith(CIPHER_VERSION):
        raise SecretCryptoError("unsupported ciphertext version")
    try:
        raw = base64.b64decode(ciphertext[len(CIPHER_VERSION):], validate=True)
    except Exception as exc:  # noqa: BLE001 — 统一收敛为一种可预期的失败
        raise SecretCryptoError("invalid ciphertext encoding") from exc
    if len(raw) <= NONCE_SIZE:
        raise SecretCryptoError("invalid ciphertext")
    nonce, sealed = raw[:NONCE_SIZE], raw[NONCE_SIZE:]
    try:
        plain = AESGCM(key).decrypt(nonce, sealed, None)
    except Exception as exc:  # noqa: BLE001
        raise SecretCryptoError("decryption failed") from exc
    return plain.decode("utf-8")


def _kek_material(app_secret: str) -> dict[str, bytes]:
    """可用 KEK 集合：``KMS_KEK_KEYS`` / ``_FILE`` 优先，APP_SECRET 派生值兜底。"""
    out: dict[str, bytes] = {}
    raw = os.getenv("KMS_KEK_KEYS") or ""
    if not raw:
        path = os.getenv("KMS_KEK_KEYS_FILE") or ""
        if path:
            try:
                raw = open(path, encoding="utf-8").read()  # noqa: SIM115 - 一次性读取
            except OSError:
                raw = ""
    if raw.strip():
        try:
            decoded = json.loads(raw)
        except ValueError as exc:
            raise SecretCryptoError("invalid KMS_KEK_KEYS json") from exc
        if not isinstance(decoded, dict):
            raise SecretCryptoError("invalid KMS_KEK_KEYS json")
        for key_id, b64 in decoded.items():
            if not _KEY_ID_RE.match(str(key_id)):
                raise SecretCryptoError("invalid KMS key id")
            try:
                material = base64.b64decode(str(b64), validate=True)
            except Exception as exc:  # noqa: BLE001
                raise SecretCryptoError("invalid KMS key material") from exc
            if len(material) != DATA_KEY_SIZE:
                raise SecretCryptoError("invalid KMS key size")
            out[str(key_id)] = material
    if app_secret:
        # 与 Go 侧 local provider 的兜底 KEK 完全一致：老部署不配 KMS_KEK_KEYS 也能读 v2。
        out.setdefault(APP_SECRET_KEY_ID, derive_key(app_secret))
    return out


def decrypt_envelope(ciphertext: str, app_secret: str) -> str:
    """解密 Go ``kms.Envelope`` 产生的 ``v2:`` 密文。

    未知 keyID / KEK 不匹配一律抛 ``SecretCryptoError`` —— 引擎必须**跳过**这把 key，
    而不是把密文当明文去调上游（那会变成一条带着 `v2:` 前缀的 401）。
    """
    if not isinstance(ciphertext, str) or not ciphertext.startswith(V2_VERSION):
        raise SecretCryptoError("unsupported ciphertext version")
    parts = ciphertext[len(V2_VERSION):].split(":")
    if len(parts) != 3:
        raise SecretCryptoError("invalid envelope format")
    key_id, wrapped_b64, body_b64 = parts
    if not _KEY_ID_RE.match(key_id):
        raise SecretCryptoError("invalid key id")
    keks = _kek_material(app_secret)
    kek = keks.get(key_id)
    if kek is None:
        raise SecretCryptoError("unknown key id")
    try:
        wrapped = base64.b64decode(wrapped_b64, validate=True)
        body = base64.b64decode(body_b64, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretCryptoError("invalid ciphertext encoding") from exc
    if len(wrapped) <= NONCE_SIZE or len(body) <= NONCE_SIZE:
        raise SecretCryptoError("invalid ciphertext")
    try:
        dek = AESGCM(kek).decrypt(wrapped[:NONCE_SIZE], wrapped[NONCE_SIZE:], None)
    except Exception as exc:  # noqa: BLE001
        raise SecretCryptoError("key unwrap failed") from exc
    if len(dek) != DATA_KEY_SIZE:
        raise SecretCryptoError("invalid data key")
    try:
        plain = AESGCM(dek).decrypt(body[:NONCE_SIZE], body[NONCE_SIZE:], None)
    except Exception as exc:  # noqa: BLE001
        raise SecretCryptoError("decryption failed") from exc
    return plain.decode("utf-8")


def key_fingerprint(plaintext: str) -> str:
    """不可逆指纹，仅用于列表展示与去重；不参与解密。"""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:16]
