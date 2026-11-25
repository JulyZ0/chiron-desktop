"""测试用的 manifest 签名助手（H51）。

生产代码在 `app.components.manifest_signature`；这里持有测试专用私钥，并且必须与
conftest 注入 `CHIRON_COMPONENT_PUBKEY` 的那对密钥一致 —— 所以两处都从本模块取。
"""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.components import manifest_signature

PRIVATE_KEY = ed25519.Ed25519PrivateKey.generate()


def private_key_b64() -> str:
    """私钥的**裸 seed**（32 字节）base64 —— 构建器接受这个形态。"""
    return base64.b64encode(
        PRIVATE_KEY.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode("ascii")


def public_key_b64() -> str:
    der = PRIVATE_KEY.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return base64.b64encode(der).decode("ascii")


def sign_manifest(payload: dict) -> dict:
    """就地写入 `signature` 并返回同一 dict。"""
    payload.pop("signature", None)
    payload["signature"] = base64.b64encode(
        PRIVATE_KEY.sign(manifest_signature.canonical_payload(payload))
    ).decode("ascii")
    return payload
