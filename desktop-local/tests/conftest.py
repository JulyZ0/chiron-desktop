"""测试夹具。

只提供 ``Settings`` 构造助手。业务层的 ``create_app`` 已随 desktop-local 的
FastAPI 外壳一并移除（见 launcher.py 的模块文档：引导器不再是 HTTP 服务），
因此这里不再有 ``TestClient`` 夹具。
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "pg_enabled": False,
        "redis_enabled": False,
        # 心跳间隔压到 10ms + 只发 2 条，让 SSE 测试不用等 15s
        "sse_heartbeat_seconds": 0.01,
        "sse_max_heartbeats": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)


# ── H51：组件 manifest 验签夹具 ─────────────────────────────────────────────
# 安装路径现在强制验证发布者签名，所以测试要把测试密钥对的公钥注入环境变量；
# 私钥在同目录的 `manifest_signing.py`（夹具用它签 manifest）。
import pytest as _pytest  # noqa: E402

from app.components import manifest_signature as _manifest_signature  # noqa: E402
from manifest_signing import public_key_b64 as _public_key_b64  # noqa: E402


@_pytest.fixture(autouse=True)
def _component_pubkey(monkeypatch):  # type: ignore[no-untyped-def]
    """公钥给安装端验签，私钥给构建器签名 —— 两边是同一对密钥。"""
    from manifest_signing import private_key_b64

    monkeypatch.setenv(_manifest_signature.ENV_PUBKEY, _public_key_b64())
    monkeypatch.setenv(_manifest_signature.ENV_SIGNING_KEY, private_key_b64())
    yield
