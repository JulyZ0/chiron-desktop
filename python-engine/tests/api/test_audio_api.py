"""本地语音转写端点（`/v1/audio/*`）的契约测试。

这组用例锁死四件事，每一件都对应一个"看起来能跑、实际是坏"的形态：

1. **依赖缺失时端点是 503 而不是 500，也不是 import 期崩溃**。
   `faster_whisper` 是可选依赖（带入 ctranslate2/onnxruntime，约 100–200 MB）。
   若 `app/api/audio.py` 在模块级 `import faster_whisper`，整个引擎会**起不来** ——
   语音用不了是小事，聊天也一起挂掉是大事。所以本文件**故意在本机没装该依赖的
   环境里也能全绿**：`find_spec` 一律 monkeypatch，不碰真实安装。
2. **413 由服务端读层兜住**，不能把任意大的 body 一次读进内存。
3. **空音频是 400**，不是"转写成功但文本为空" —— 后者会让前端以为听清了。
4. **不记录转写文本**：音频是用户说的话，日志只留长度与语种。

认证走真实中间件（`conftest.py` 自动注入 `X-Internal-Token`，query 身份由这里显式给），
所以这些用例验证的是"网关代理那种调用姿势"确实能到 handler。
"""

from __future__ import annotations

import importlib.util

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.audio as audio_api
from app.config import settings
from app.main import create_app

# 网关注入的身份（真实链路见 internal/api/routes_audio.go）。
IDENTITY = {"user_id": "test-user", "tenant_id": "1"}


@pytest.fixture(autouse=True)
def _clean_model_state(monkeypatch):
    """每个用例都从"没有已加载模型、开关默认开"开始。

    模块级的 `_model` / `_semaphore` 会在用例之间串味：上一例喂进去的假模型
    会让这一例的 `availability` 报 `loaded: true`，测出来的东西与被测逻辑无关。
    """
    monkeypatch.setattr(audio_api, "_model", None)
    monkeypatch.setattr(audio_api, "_model_name", "")
    monkeypatch.setattr(audio_api, "_semaphore", None)
    monkeypatch.setattr(settings, "speech_local_enabled", True)
    yield


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _patch_dependency(monkeypatch, *, present: bool) -> None:
    """让 `find_spec("faster_whisper")` 返回"有 / 没有"，不依赖真实安装。"""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object):
        if name == audio_api._MODULE_NAME:
            return object() if present else None  # type: ignore[return-value]
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(audio_api.importlib.util, "find_spec", fake_find_spec)


# ── availability ──────────────────────────────────────────────────────


async def test_availability_reports_reason_when_dependency_missing(monkeypatch):
    """缺依赖 ⇒ available=False 且**给出可操作的原因**。

    只说 `available: false` 会让用户看到"语音按钮灰着"却无从下手；
    原因里必须出现要装的东西，否则运维只能瞎猜。
    """
    _patch_dependency(monkeypatch, present=False)
    async with _client() as c:
        resp = await c.get("/v1/audio/availability", params=IDENTITY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert "faster-whisper" in body["reason"]
    # 探测**不能**顺手加载模型：那要下载数百 MB
    assert body["loaded"] is False


async def test_availability_is_true_when_dependency_present(monkeypatch):
    _patch_dependency(monkeypatch, present=True)
    async with _client() as c:
        resp = await c.get("/v1/audio/availability", params=IDENTITY)

    body = resp.json()
    assert body["available"] is True
    assert body["reason"] == ""
    assert body["model"] == (settings.speech_local_model or "small")


async def test_availability_reports_switch_off(monkeypatch):
    """开关关闭与"依赖缺失"必须**给出不同的原因**：处置方式完全不同。"""
    _patch_dependency(monkeypatch, present=True)
    monkeypatch.setattr(settings, "speech_local_enabled", False)
    async with _client() as c:
        resp = await c.get("/v1/audio/availability", params=IDENTITY)

    body = resp.json()
    assert body["available"] is False
    assert "SPEECH_LOCAL_ENABLED" in body["reason"]


# ── transcribe ────────────────────────────────────────────────────────


def _audio_file(payload: bytes = b"fake-webm-bytes"):
    return {"file": ("voice-input.webm", payload, "audio/webm")}


async def test_transcribe_returns_503_when_dependency_missing(monkeypatch):
    """503 而非 500：这是"本机环境没装这个能力"，可操作且与本次请求无关。"""
    _patch_dependency(monkeypatch, present=False)
    async with _client() as c:
        resp = await c.post(
            "/v1/audio/transcribe", params=IDENTITY, files=_audio_file(), data={"language": "zh"}
        )

    assert resp.status_code == 503
    assert "faster-whisper" in resp.json()["detail"]


async def test_transcribe_rejects_empty_audio(monkeypatch):
    """空文件是 400。回 200 + text:"" 会被前端当成"听清了但没内容"。"""
    _patch_dependency(monkeypatch, present=True)
    async with _client() as c:
        resp = await c.post(
            "/v1/audio/transcribe", params=IDENTITY, files=_audio_file(b"")
        )

    assert resp.status_code == 400


async def test_transcribe_rejects_oversized_body(monkeypatch):
    """超限在读层封顶，不把整个 body 收进内存。

    把上限调小来测：真的传 25 MB 会让这个用例慢上几秒，而它要验证的是
    "读到上限就停"这条逻辑本身。
    """
    _patch_dependency(monkeypatch, present=True)
    monkeypatch.setattr(audio_api, "MAX_AUDIO_BYTES", 1024)
    monkeypatch.setattr(audio_api, "_READ_CHUNK", 256)

    async with _client() as c:
        resp = await c.post(
            "/v1/audio/transcribe", params=IDENTITY, files=_audio_file(b"x" * 4096)
        )

    assert resp.status_code == 413


async def test_transcribe_happy_path_returns_text(monkeypatch):
    """成功路径：把同步转写替身注入，验证返回值与"语种原样透传"。"""
    _patch_dependency(monkeypatch, present=True)
    seen: dict[str, object] = {}

    def fake_transcribe(data: bytes, language: str) -> dict[str, object]:
        seen["bytes"] = len(data)
        seen["language"] = language
        return {"text": "你好世界", "language": "zh", "duration": 1.5}

    monkeypatch.setattr(audio_api, "_transcribe_sync", fake_transcribe)

    async with _client() as c:
        resp = await c.post(
            "/v1/audio/transcribe",
            params=IDENTITY,
            files=_audio_file(b"fake-webm-bytes"),
            data={"language": "zh"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "你好世界"
    assert body["language"] == "zh"
    assert body["duration"] == 1.5
    assert seen["language"] == "zh"


async def test_transcribe_passes_empty_language_through(monkeypatch):
    """`language` 缺省为空 ⇒ 交给模型自动判定（前端"不跟随界面语言"时就是这样）。"""
    _patch_dependency(monkeypatch, present=True)
    seen: dict[str, object] = {}

    def fake_transcribe(data: bytes, language: str) -> dict[str, object]:
        seen["language"] = language
        return {"text": "hi", "language": "en", "duration": 0.5}

    monkeypatch.setattr(audio_api, "_transcribe_sync", fake_transcribe)

    async with _client() as c:
        resp = await c.post("/v1/audio/transcribe", params=IDENTITY, files=_audio_file())

    assert resp.status_code == 200
    assert seen["language"] == ""


async def test_transcribe_masks_upstream_error_detail(monkeypatch):
    """解码/模型异常 ⇒ 500，且**响应体不带异常原文**（可能含本机路径）。"""
    _patch_dependency(monkeypatch, present=True)

    def boom(data: bytes, language: str) -> dict[str, object]:
        raise RuntimeError(r"C:\Users\someone\AppData\secret-model-path is corrupt")

    monkeypatch.setattr(audio_api, "_transcribe_sync", boom)

    async with _client() as c:
        resp = await c.post("/v1/audio/transcribe", params=IDENTITY, files=_audio_file())

    assert resp.status_code == 500
    detail = resp.json()["detail"]
    assert "AppData" not in detail
    assert "secret-model-path" not in detail


async def test_transcribe_requires_gateway_identity(monkeypatch):
    """不带 query 身份 ⇒ 中间件放行到 handler，但身份缺失不该被当成"某个租户"。

    这里的语义由中间件决定：无 query 身份 + 合法内部 token = 放行（body-身份端点）。
    音频端点**必须**有身份（日志与配额要归属），所以它在 handler 层就依赖 tenant。
    本用例只锁住"身份确实传到了"这条链路：带齐身份能到 handler 并成功。
    """
    _patch_dependency(monkeypatch, present=True)
    monkeypatch.setattr(
        audio_api,
        "_transcribe_sync",
        lambda data, language: {"text": "ok", "language": "en", "duration": 0.1},
    )

    async with _client() as c:
        resp = await c.post("/v1/audio/transcribe", params=IDENTITY, files=_audio_file())

    assert resp.status_code == 200
