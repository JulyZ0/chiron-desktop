"""Tests for web tools — web_search 与 HTML→markdown 渲染。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.tools.web import (
    DuckDuckGoProvider,
    WebProvider,
    html_to_markdown,
    set_web_provider,
    web_fetch,
    web_search,
)


class FakeProvider(WebProvider):
    def __init__(self, results=None, fetch_result=None):
        self._results = results or []
        self._fetch = fetch_result or {}

    async def search(self, query, max_results):
        return self._results

    async def fetch(self, url, max_chars):
        return self._fetch


class TestHtmlToMarkdown:
    def test_headings_paragraphs(self):
        md = html_to_markdown("<h1>Title</h1><p>Hello <strong>world</strong>.</p>")
        assert "# Title" in md
        assert "Hello" in md and "world" in md

    def test_list_and_code(self):
        md = html_to_markdown("<ul><li>a</li><li>b</li></ul><pre>code line</pre>")
        assert "- a" in md and "- b" in md
        assert "```" in md and "code line" in md

    def test_links_and_tables(self):
        md = html_to_markdown('<p><a href="https://x.com">X</a></p><table><tr><td>c1</td><td>c2</td></tr></table>')
        assert "[X](https://x.com)" in md
        assert "| c1 | c2 |" in md

    def test_strips_script_style(self):
        md = html_to_markdown("<script>bad()</script><style>.x{}</style><p>good</p>")
        assert "bad" not in md and "good" in md


class TestWebTools:
    @pytest.mark.asyncio
    async def test_web_search_returns_results(self):
        set_web_provider(FakeProvider(results=[
            {"title": "T", "url": "https://e.com", "snippet": "s"},
        ]))
        out = await web_search("python")
        assert out["count"] == 1
        assert out["results"][0]["title"] == "T"

    @pytest.mark.asyncio
    async def test_web_search_empty_query(self):
        out = await web_search("  ")
        assert "error" in out

    @pytest.mark.asyncio
    async def test_web_fetch_returns_content(self):
        set_web_provider(FakeProvider(fetch_result={
            "url": "https://e.com", "status_code": 200, "content_type": "text/html", "content": "ok",
        }))
        out = await web_fetch("https://e.com")
        assert out["content"] == "ok"

    @pytest.mark.asyncio
    async def test_web_search_provider_error(self):
        class Broken(FakeProvider):
            async def search(self, query, max_results):
                raise RuntimeError("provider down")
        set_web_provider(Broken())
        out = await web_search("q")
        assert "error" in out and "unavailable" in out["error"]

    def test_ddg_redirect_decode(self):
        url = DuckDuckGoProvider._decode_redirect("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage")
        assert url == "https://example.com/page"


class TestDuckDuckGoProvider:
    """DuckDuckGoProvider 真实路径测试 (mock httpx)"""

    @pytest.mark.asyncio
    async def test_search_parses_ddg_html(self):
        """search: 解析 DDG HTML 返回结果列表"""
        html = '''
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage">Example</a>
          <a class="result__snippet">A snippet here</a>
        </div>
        <div class="result">
          <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Ffoo.com">Foo</a>
        </div>
        '''
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            provider = DuckDuckGoProvider()
            results = await provider.search("test query", max_results=5)

        assert len(results) == 2
        assert results[0]["title"] == "Example"
        assert results[0]["url"] == "https://example.com/page"
        assert results[0]["snippet"] == "A snippet here"
        assert results[1]["url"] == "https://foo.com"

    @pytest.mark.asyncio
    async def test_search_respects_max_results(self):
        html = "".join(
            f'<div class="result"><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fsite{i}.com">Site {i}</a></div>'
            for i in range(10)
        )
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            provider = DuckDuckGoProvider()
            results = await provider.search("q", max_results=3)

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_fetch_renders_html_to_markdown(self):
        html = "<html><body><h1>Test</h1><p>Content here</p></body></html>"
        mock_resp = MagicMock()
        mock_resp.text = html
        mock_resp.headers = {"content-type": "text/html"}
        mock_resp.url = "https://example.com/page"
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            provider = DuckDuckGoProvider()
            result = await provider.fetch("https://example.com/page")

        assert result["url"] == "https://example.com/page"
        assert "# Test" in result["content"]
        assert "Content here" in result["content"]

    @pytest.mark.asyncio
    async def test_fetch_truncates_to_max_chars(self):
        long_body = "<p>" + "x" * 300_000 + "</p>"
        mock_resp = MagicMock()
        mock_resp.text = long_body
        mock_resp.headers = {"content-type": "text/plain"}
        mock_resp.url = "https://example.com"
        mock_resp.status_code = 200

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            provider = DuckDuckGoProvider()
            result = await provider.fetch("https://example.com", max_chars=1000)

        assert len(result["content"]) <= 1100
        assert "[truncated]" in result["content"]

    @pytest.mark.asyncio
    async def test_web_fetch_handles_http_error(self):
        """web_fetch: HTTP 状态错误返回结构化错误"""
        set_web_provider(DuckDuckGoProvider())

        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.headers = {"content-type": "text/html"}
        http_err = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=mock_resp,
        )

        with patch("httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=http_err)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            out = await web_fetch("https://example.com/missing")

        assert out["status_code"] == 404

    @pytest.mark.asyncio
    async def test_web_search_ssrf_called(self):
        """search: assert_safe_url 被调用（SSRF 防护）"""
        with patch("app.tools.web.assert_safe_url") as mock_ssrf, \
             patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.text = ""
            mock_resp.raise_for_status = MagicMock()
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            provider = DuckDuckGoProvider()
            await provider.search("q", 5)

        mock_ssrf.assert_called_once_with("https://html.duckduckgo.com/html/")

    @pytest.mark.asyncio
    async def test_web_fetch_ssrf_called(self):
        """fetch: assert_safe_url 被调用（SSRF 防护）"""
        target = "https://example.com/page"
        with patch("app.tools.web.assert_safe_url") as mock_ssrf, \
             patch("httpx.AsyncClient") as mock_cls:
            mock_resp = MagicMock()
            mock_resp.text = "<p>ok</p>"
            mock_resp.headers = {"content-type": "text/html"}
            mock_resp.url = target
            mock_resp.status_code = 200
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_resp)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_cls.return_value = mock_client

            provider = DuckDuckGoProvider()
            await provider.fetch(target)

        mock_ssrf.assert_called_once_with(target)
