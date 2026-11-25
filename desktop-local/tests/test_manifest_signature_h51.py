"""H51：组件 manifest 验签（无公钥/无签名/签名不匹配一律拒绝）。

缺陷形态：manifest 自带 `sha256`，但摘要与 manifest **同源** —— 换掉源的人顺手改摘要
即可。校验值只防传输损坏，防不了"源被替换"。组件会以用户身份长期运行，所以来源必须
可验证；没配公钥时也必须**拒绝**，不能"跳过验签继续装"。
"""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app.components import manifest_signature
from app.components.manager import ManifestInvalidError, parse_manifest


def _keypair():  # type: ignore[no-untyped-def]
    priv = ed25519.Ed25519PrivateKey.generate()
    pub_der = priv.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv, base64.b64encode(pub_der).decode("ascii")


def _manifest(name: str = "whisper") -> dict:
    return {
        "name": name,
        "version": "1.2.3",
        "entry": "run.py",
        "files": [{"path": "run.py", "sha256": "a" * 64}],
    }


def _signed(priv, payload: dict) -> dict:  # type: ignore[no-untyped-def]
    body = dict(payload)
    sig = priv.sign(manifest_signature.canonical_payload(body))
    body["signature"] = base64.b64encode(sig).decode("ascii")
    return body


def test_valid_signature_passes() -> None:
    priv, pub = _keypair()
    payload = _signed(priv, _manifest())
    manifest_signature.verify_manifest_signature(payload, pub)  # 不抛即通过


def test_missing_public_key_is_refused(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv(manifest_signature.ENV_PUBKEY, raising=False)
    priv, _ = _keypair()
    payload = _signed(priv, _manifest())
    with pytest.raises(ManifestInvalidError, match="未配置发布者公钥"):
        manifest_signature.verify_manifest_signature(payload)


def test_missing_signature_is_refused() -> None:
    _, pub = _keypair()
    with pytest.raises(ManifestInvalidError, match="缺少 signature"):
        manifest_signature.verify_manifest_signature(_manifest(), pub)


def test_tampered_manifest_is_refused() -> None:
    """改一个字节（这里改 version）就必须验签失败。"""
    priv, pub = _keypair()
    payload = _signed(priv, _manifest())
    payload["version"] = "9.9.9"
    with pytest.raises(ManifestInvalidError, match="签名不匹配"):
        manifest_signature.verify_manifest_signature(payload, pub)


def test_signature_from_another_key_is_refused() -> None:
    priv_a, _ = _keypair()
    _, pub_b = _keypair()
    payload = _signed(priv_a, _manifest())
    with pytest.raises(ManifestInvalidError, match="签名不匹配"):
        manifest_signature.verify_manifest_signature(payload, pub_b)


def test_bad_public_key_is_refused() -> None:
    priv, _ = _keypair()
    payload = _signed(priv, _manifest())
    with pytest.raises(ManifestInvalidError, match="DER"):
        manifest_signature.verify_manifest_signature(payload, "bm90LWEta2V5")


def test_canonical_payload_ignores_signature_and_key_order() -> None:
    a = {"name": "x", "version": "1.0.0", "entry": "a.py", "signature": "zzz"}
    b = {"entry": "a.py", "version": "1.0.0", "name": "x"}
    assert manifest_signature.canonical_payload(a) == manifest_signature.canonical_payload(b)


def test_signature_does_not_replace_digest_checks() -> None:
    """解析仍然要求每个文件有 sha256：签名过了也不代表摘要可以缺。"""
    priv, pub = _keypair()
    payload = _manifest()
    payload["files"] = [{"path": "run.py"}]  # 无 sha256
    signed = _signed(priv, payload)
    with pytest.raises(ManifestInvalidError, match="sha256"):
        manifest_signature.verify_manifest_signature(signed, pub)
        parse_manifest(signed, "whisper")


def test_signed_manifest_parses_after_verification() -> None:
    priv, pub = _keypair()
    signed = _signed(priv, _manifest())
    manifest_signature.verify_manifest_signature(signed, pub)
    parsed = parse_manifest(signed, "whisper")
    assert parsed.version == "1.2.3"


def test_fingerprint_is_stable_and_short() -> None:
    payload = _manifest()
    fp = manifest_signature.manifest_fingerprint(payload)
    assert len(fp) == 16
    assert fp == manifest_signature.manifest_fingerprint(dict(payload, signature="x" * 10))
    assert json.dumps(payload)  # 确保原字典未被改写
