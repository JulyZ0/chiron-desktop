# LLM client 测试 — 统一嵌入入口：本地优先、API 回退、fail-loud
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.llm.client import LLMClient


def make_client() -> LLMClient:
    return LLMClient()


class TestEmbedFailLoud:
    """无可用后端时必须显式抛错，绝不返回零向量"""

    async def test_no_backend_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "")
        client = make_client()  # gateway 未绑定
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            await client.embed("hello")

    async def test_gateway_embed_failure_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "")
        client = make_client()
        fake_gw = MagicMock()
        fake_gw.embed = AsyncMock(side_effect=RuntimeError("provider down"))
        client.bind_gateway(fake_gw)
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            await client.embed("hello")

    async def test_empty_embedding_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "")
        client = make_client()
        fake_gw = MagicMock()
        fake_resp = MagicMock()
        fake_resp.embedding = []
        fake_gw.embed = AsyncMock(return_value=fake_resp)
        client.bind_gateway(fake_gw)
        with pytest.raises(RuntimeError, match="embedding unavailable"):
            await client.embed("hello")


class TestEmbedBackends:
    """本地优先、API 回退"""

    async def test_gateway_embedding(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "")
        monkeypatch.setattr(settings, "embedding_model", "text-embedding-3-small")
        client = make_client()
        fake_resp = MagicMock()
        fake_resp.embedding = [0.1, 0.2, 0.3]
        fake_gw = MagicMock()
        fake_gw.embed = AsyncMock(return_value=fake_resp)
        client.bind_gateway(fake_gw)

        result = await client.embed("hi")

        assert result == [0.1, 0.2, 0.3]
        fake_gw.embed.assert_awaited_once_with("hi", "text-embedding-3-small")

    async def test_local_embedding_skips_gateway(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "mock-bge")
        client = make_client()
        fake_gw = MagicMock()
        fake_gw.embed = AsyncMock()
        client.bind_gateway(fake_gw)

        fake_encoder = MagicMock()
        fake_encoder.encode.return_value = MagicMock(tolist=lambda: [0.5, 0.6])
        fake_st = MagicMock()
        fake_st.SentenceTransformer.return_value = fake_encoder

        with patch.dict(sys.modules, {"sentence_transformers": fake_st}):
            result = await client.embed("hello")

        assert result == [0.5, 0.6]
        fake_gw.embed.assert_not_awaited()  # 本地成功则不调 API

    async def test_local_failure_falls_back_to_gateway(self, monkeypatch):
        monkeypatch.setattr(settings, "local_embedding_model", "mock-bge")
        client = make_client()
        fake_resp = MagicMock()
        fake_resp.embedding = [0.7]
        fake_gw = MagicMock()
        fake_gw.embed = AsyncMock(return_value=fake_resp)
        client.bind_gateway(fake_gw)

        with patch.dict(sys.modules, {"sentence_transformers": None}):  # 依赖缺失
            result = await client.embed("hello")

        assert result == [0.7]
        fake_gw.embed.assert_awaited_once()
