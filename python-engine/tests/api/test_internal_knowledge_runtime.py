"""``GET /v1/internal/knowledge-runtime`` —— 后台「知识库」页的运行时事实来源。

这个端点是"页面显示的引擎状态"的**唯一出处**，所以它撒的谎会直接变成用户
排查方向。这里钉住三件事：

1. **``probe=false`` 不联网**。打开页面只是想知道引擎在打哪台服务，不该为此
   等一次最长 10 秒的请求；探测失败还会把"配置是好的、只是那台机器没起"
   渲染成"出错"。
2. **``url`` 只测不改**。页面要能在保存前验一个候选地址 —— 如果探一下就改了
   生效地址，用户会在"测试"这个动作里悄悄切走服务。
3. **连不上是业务结果，不是 500**。状态码恒 200，``probe.ok=false`` 表达失败；
   否则页面只能从错误串里猜"是地址错了还是引擎坏了"。
"""

from __future__ import annotations

import httpx
import pytest

from app.api import internal_knowledge
from app.config import settings
from app.knowledge import runtime

BOOTSTRAP = "http://127.0.0.1:8100"
REMOTE = "http://pi.internal:9100"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(runtime, "_BOOTSTRAP_URL", BOOTSTRAP)
    monkeypatch.setattr(runtime, "_current_mode", runtime.MODE_LOCAL)
    monkeypatch.setattr(settings, "knowledge_service_url", BOOTSTRAP, raising=False)
    yield


def _patch_http(monkeypatch, *, status: int = 200, body=None, raises: Exception | None = None):
    """让 ``_probe`` 内的 AsyncClient 走 MockTransport，并记录请求过的 URL。"""
    seen: list[str] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if raises is not None:
            raise raises
        return httpx.Response(status, json=body if body is not None else {"ok": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=transport, **kwargs)
    )
    return seen


async def test_probe_false_does_not_touch_network(monkeypatch) -> None:
    seen = _patch_http(monkeypatch)

    state = await internal_knowledge.internal_knowledge_runtime(probe=False)

    assert seen == [], "probe=false 必须完全不联网：打开页面不该等一次超时"
    assert state["service_url"] == BOOTSTRAP
    assert "probe" not in state, "未探测时不该编一个探测结果出来"


async def test_probe_true_reports_success(monkeypatch) -> None:
    seen = _patch_http(monkeypatch, body={"ok": True})

    state = await internal_knowledge.internal_knowledge_runtime(url="", probe=True)

    # 探的是**当前生效**的地址，且打的是 /healthz。
    assert seen and seen[0].startswith(BOOTSTRAP) and seen[0].endswith("/healthz")
    assert state["probe"]["ok"] is True
    assert state["probe"]["status_code"] == 200


async def test_explicit_url_probes_it_without_changing_state(monkeypatch) -> None:
    seen = _patch_http(monkeypatch, body={"ok": True})

    state = await internal_knowledge.internal_knowledge_runtime(url=REMOTE + "/")

    assert seen and seen[0].startswith(REMOTE), "传了 url 就要测它，而不是当前生效地址"
    # 「测试连接」绝不能顺带切走服务：探一下 = 读，不是写。
    assert state["service_url"] == BOOTSTRAP
    assert state["mode"] == runtime.MODE_LOCAL
    assert runtime.current_url() == BOOTSTRAP


async def test_unreachable_service_is_a_business_result(monkeypatch) -> None:
    # 连不上不能抛：调用方（网关 → 页面）要拿到 ok=false，而不是一个 500。
    _patch_http(monkeypatch, raises=httpx.ConnectError("connection refused"))

    state = await internal_knowledge.internal_knowledge_runtime(url=REMOTE)

    assert state["probe"]["ok"] is False
    assert state["probe"]["status_code"] == 0
    assert state["probe"]["detail"], "失败必须给出原因，否则页面只能说『连接失败』"


async def test_http_error_status_is_reported_with_code(monkeypatch) -> None:
    # 服务活着但拒绝：状态码是最有诊断价值的那个数字，必须原样带回。
    _patch_http(monkeypatch, status=503, body={"ok": False, "detail": "index not ready"})

    state = await internal_knowledge.internal_knowledge_runtime(url=REMOTE)

    assert state["probe"]["ok"] is False
    assert state["probe"]["status_code"] == 503
    assert state["probe"]["detail"] == "index not ready"


async def test_body_ok_false_marks_probe_failed(monkeypatch) -> None:
    # 200 也可能表示"活着但不可用"：服务侧用 ok=false 表达，不能只看状态码。
    _patch_http(monkeypatch, status=200, body={"ok": False, "detail": "shard missing"})

    state = await internal_knowledge.internal_knowledge_runtime(url=REMOTE)

    assert state["probe"]["ok"] is False
    assert state["probe"]["detail"] == "shard missing"


async def test_no_configured_url_says_so_instead_of_failing() -> None:
    # 没配地址：明确说"未配置"，而不是给一个看起来像网络故障的探测结果。
    runtime._BOOTSTRAP_URL = ""
    settings.knowledge_service_url = ""

    state = await internal_knowledge.internal_knowledge_runtime(url="", probe=True)

    assert state["probe"]["ok"] is False
    assert "未配置" in state["probe"]["detail"]
