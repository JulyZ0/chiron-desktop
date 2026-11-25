"""H51（组件侧）：manifest 必须带 Ed25519 发布者签名，否则拒绝安装。

缺陷形态：manifest 自带 `sha256`，但**摘要与 manifest 同源** —— 谁改了文件就顺手
改摘要，校验值只能防传输损坏，防不了"源被换掉"。而组件会以用户身份长期运行。

做法与服务端更新清单（H45）同一口径：
  · manifest 顶层 `signature` 是**规范化 JSON 的 Ed25519 签名**（base64），
    公钥内嵌/由 `CHIRON_COMPONENT_PUBKEY` 提供；
  · 没配公钥 ⇒ **拒绝**（fail-closed，不是"跳过验签"）；
  · 验签失败 ⇒ 拒绝安装，且错误信息不暴露摘要细节。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

ENV_PUBKEY = "CHIRON_COMPONENT_PUBKEY"
#: 构建期签名私钥（Ed25519 seed 或 PKCS8 DER 的 base64）。
ENV_SIGNING_KEY = "CHIRON_COMPONENT_SIGNING_KEY"

#: 参与签名的字段（按此顺序、此集合做规范化）。刻意**排除** `signature` 自身。
_SIGNED_KEYS = ("name", "version", "entry", "files", "sha256", "args", "health")


def embedded_public_key() -> str:
    """内嵌/环境提供的 Ed25519 公钥（SPKI DER 的 base64）。空串表示未配置。"""
    return str(os.environ.get(ENV_PUBKEY, "")).strip()


def canonical_payload(payload: Mapping[str, Any]) -> bytes:
    """把参与签名的字段规范化成字节（键序固定、无多余空白）。"""
    signed = {key: payload.get(key) for key in _SIGNED_KEYS if key in payload}
    return json.dumps(signed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def verify_manifest_signature(payload: Mapping[str, Any], public_key_b64: str = "") -> None:
    """校验 manifest 签名；不通过就抛 `ManifestInvalidError`。

    为什么失败信息里**不带**摘要值：错误信息会进用户界面与工单，带上摘要等于把
    "哪一份被改过"的线索一起发出去，对排查帮助不大，对攻击者倒是方便。
    """
    from app.components.manager import ManifestInvalidError

    signature = str(payload.get("signature") or "").strip()
    key = (public_key_b64 or embedded_public_key()).strip()
    if not key:
        raise ManifestInvalidError(
            "manifest 未配置发布者公钥（CHIRON_COMPONENT_PUBKEY）："
            "无法验证来源，拒绝安装组件"
        )
    if not signature:
        raise ManifestInvalidError("manifest 缺少 signature：拒绝安装未签名的组件")

    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.hazmat.primitives.serialization import load_der_public_key
    except Exception as exc:  # noqa: BLE001 - 缺依赖也要 fail-closed
        raise ManifestInvalidError(f"缺少验签依赖（cryptography）：{exc}") from exc

    try:
        der = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestInvalidError("CHIRON_COMPONENT_PUBKEY 不是合法 base64") from exc
    try:
        pub = load_der_public_key(der)
    except Exception as exc:  # noqa: BLE001
        raise ManifestInvalidError("CHIRON_COMPONENT_PUBKEY 不是合法的 DER 公钥") from exc
    if not isinstance(pub, Ed25519PublicKey):
        raise ManifestInvalidError("CHIRON_COMPONENT_PUBKEY 不是 Ed25519 公钥")

    try:
        sig = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestInvalidError("manifest.signature 不是合法 base64") from exc

    try:
        pub.verify(sig, canonical_payload(payload))
    except InvalidSignature as exc:
        logger.warning("component manifest signature mismatch (name=%s)", payload.get("name"))
        raise ManifestInvalidError("manifest 签名不匹配：拒绝安装（源可能已被替换）") from exc


def manifest_fingerprint(payload: Mapping[str, Any]) -> str:
    """规范化负载的 sha256（便于日志里引用"哪一份"，而不暴露摘要字段值）。"""
    return hashlib.sha256(canonical_payload(payload)).hexdigest()[:16]


def signing_key_b64() -> str:
    """构建期签名私钥（base64）。空串表示未配置。"""
    return str(os.environ.get(ENV_SIGNING_KEY, "")).strip()


def sign_payload(payload: Mapping[str, Any], private_key_b64: str = "") -> dict[str, Any]:
    """给 manifest 打签名并返回新 dict（不修改入参）。

    构建期没有私钥就**失败**：产出一个"装不进去的包"比直接报错更浪费时间 ——
    安装端一定会拒绝，而且失败点离原因更远。
    """
    from app.components.manager import ManifestInvalidError

    key = (private_key_b64 or signing_key_b64()).strip()
    if not key:
        raise ManifestInvalidError(
            "构建缺少签名私钥（CHIRON_COMPONENT_SIGNING_KEY）："
            "无法产出可安装的组件包"
        )
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except Exception as exc:  # noqa: BLE001
        raise ManifestInvalidError(f"缺少验签依赖（cryptography）：{exc}") from exc

    try:
        raw = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ManifestInvalidError("CHIRON_COMPONENT_SIGNING_KEY 不是合法 base64") from exc

    # 32 字节 = 裸 seed；更长 = PKCS8 DER
    try:
        if len(raw) == 32:
            priv = Ed25519PrivateKey.from_private_bytes(raw)
        else:
            from cryptography.hazmat.primitives.serialization import load_der_private_key

            loaded = load_der_private_key(raw, password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ManifestInvalidError("签名私钥不是 Ed25519")
            priv = loaded
    except ManifestInvalidError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ManifestInvalidError("CHIRON_COMPONENT_SIGNING_KEY 无法解析") from exc

    # 用规范化负载签名：与安装端同一个函数，避免"两边各自拼 JSON"的漂移。
    signature = priv.sign(canonical_payload(payload))
    signed = dict(payload)
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def public_key_for(private_key_b64: str = "") -> str:
    """由私钥导出公钥（base64 SPKI），方便运维把两边配对好。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = (private_key_b64 or signing_key_b64()).strip()
    raw = base64.b64decode(key, validate=True)
    priv = (
        Ed25519PrivateKey.from_private_bytes(raw)
        if len(raw) == 32
        else None
    )
    if priv is None:
        raise ValueError("只支持 32 字节 seed 形态")
    der = priv.public_key().public_bytes(
        encoding=Encoding.DER, format=PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode("ascii")
