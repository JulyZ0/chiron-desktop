"""模型来源解析（env → 网关 /models）：单测。

这组用例钉住的是**故障修复本身**：
  1. env 里没有模型时，解析必须去网关要 `/v1/internal/knowledge-model` 拿配置；
  2. 网关说"没有可用模型"时，healthz 的说明必须是"去 /models 配置"，而不是
     笼统的"未配置自托管模型"；
  3. env 显式配了就**不**再打网关（运维钉死的值优先）。
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app import models as models_mod


def _cfg(**overrides):
    cfg = MagicMock()
    cfg.internal_token = "t0ken"
    cfg.selfhosted_chat_model = ""
    cfg.selfhosted_index_model = ""
    cfg.selfhosted_llm_base_url = ""
    cfg.selfhosted_llm_api_key = ""
    cfg.selfhosted_root = "/root"
    cfg.selfhosted_optimize = "full"
    cfg.cloud_chat_model = ""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    models_mod.reset_cache()
    monkeypatch.delenv("GATEWAY_INTERNAL_URL", raising=False)
    yield
    models_mod.reset_cache()


def test_env_wins_and_skips_gateway(monkeypatch):
    called = {"n": 0}

    def _boom(_cfg):
        called["n"] += 1
        return models_mod.ResolvedModels(reason="不该被调用")

    monkeypatch.setattr(models_mod, "_fetch_from_gateway", _boom)
    cfg = _cfg(selfhosted_chat_model="qwen2.5:7b", selfhosted_index_model="qwen2.5:7b")
    resolved = pytest.importorskip("asyncio").run(models_mod.resolve(cfg))
    assert resolved.ready() and resolved.source == "env"
    assert called["n"] == 0, "env 已配好时不应再打网关（那会把 pin 住的值顶掉）"


def test_gateway_payload_is_used_when_env_missing(monkeypatch):
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8080")
    payload = {
        "success": True,
        "data": {
            "available": True,
            "provider": "openai",
            "chat_model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test",
        },
    }

    class _Resp:
        def __init__(self, body):
            self._body = body

        def read(self):
            return json.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    seen = {}

    def _urlopen(req, timeout=0):
        seen["url"] = req.full_url
        seen["token"] = req.get_header("X-internal-token")
        return _Resp(payload)

    monkeypatch.setattr(models_mod.urllib.request, "urlopen", _urlopen)
    cfg = _cfg()

    import asyncio

    resolved = asyncio.run(models_mod.resolve(cfg))
    assert resolved.ready()
    assert resolved.source == "gateway"
    assert resolved.chat_model == "gpt-4o-mini"
    # 索引模型与问答模型同源（网关只给一个名字时两边都用它）
    assert resolved.index_model == "gpt-4o-mini"
    assert resolved.api_key == "sk-test"
    assert resolved.llm_backend() == {"api_key": "sk-test", "base_url": "https://api.openai.com/v1"}
    assert seen["url"].endswith("/v1/internal/knowledge-model")
    assert seen["token"] == "t0ken"


def test_gateway_reason_is_surfaced_and_points_to_models_page(monkeypatch):
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8080")

    class _Resp:
        def read(self):
            return json.dumps(
                {"success": True, "data": {"available": False, "reason": "这台机器还没有可用模型，请先在 /models 配置"}}
            ).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(models_mod.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    import asyncio

    resolved = asyncio.run(models_mod.resolve(_cfg()))
    assert not resolved.ready()
    detail = models_mod.unavailable_detail(resolved)
    assert "/models" in detail
    assert "未配置自托管模型" not in detail, "不能退回那句让人去翻 env 的笼统文案"


def test_gateway_unreachable_keeps_actionable_hint(monkeypatch):
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:1")

    def _boom(req, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(models_mod.urllib.request, "urlopen", _boom)
    import asyncio

    resolved = asyncio.run(models_mod.resolve(_cfg()))
    assert not resolved.ready()
    assert "网关不可达" in resolved.reason
    assert "/models" in models_mod.unavailable_detail(resolved)


def test_half_configured_env_is_explained(monkeypatch):
    """只配了 chat 没配 index：原因必须指名缺哪个，而不是"未配置"。"""
    cfg = _cfg(selfhosted_chat_model="qwen2.5:7b")
    resolved = models_mod.from_env(cfg)
    assert not resolved.ready()
    assert "INDEX_MODEL" in resolved.reason


def test_negative_cache_expires(monkeypatch):
    """网关未就绪时的失败只做**短时**缓存：网关起来后下一次探测必须恢复。"""
    monkeypatch.setenv("GATEWAY_INTERNAL_URL", "http://127.0.0.1:8080")
    calls = {"n": 0}

    def _fetch(_cfg):
        calls["n"] += 1
        if calls["n"] == 1:
            return models_mod.ResolvedModels(reason="网关不可达")
        return models_mod.ResolvedModels(chat_model="m", index_model="m", source="gateway")

    monkeypatch.setattr(models_mod, "_fetch_from_gateway", _fetch)
    import asyncio

    cfg = _cfg()
    first = asyncio.run(models_mod.resolve(cfg))
    assert not first.ready()
    # 负缓存窗口内不再重复打网关
    again = asyncio.run(models_mod.resolve(cfg))
    assert not again.ready() and calls["n"] == 1
    # 强制刷新（模拟 TTL 过期后的下一次探测）→ 立刻恢复可用
    third = asyncio.run(models_mod.resolve(cfg, force=True))
    assert third.ready() and third.source == "gateway"
