"""隔离档位 API 契约测试：GET / PUT / DELETE / POST revoke。

这组测试锁死四件事，每一件都对应一个容易被"顺手改坏"的点：

1. **鉴权 fail-closed**：未配置 `LLM_GATEWAY_KEY` ⇒ 503；密钥不对 ⇒ 401。
   这些端点能改安全边界，开放等于把隔离开关交给任何人。
2. **切换只改内存**：`PUT` 之后 `configured_level()` 变，但**没有任何文件被写** ——
   撤销因此是免费的。
3. **低于底线的请求会被抬上来且说清楚**：`PUT none` 在 Windows 上实际生效 `low_il`，
   同时 `reasons` 非空。不能静默改写。
4. **撤销幂等**：`DELETE` 两次都 200，第二次 `cleared` 为 None。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.api.isolation as iso_api
import app.isolation as iso
from app.config import settings
from app.main import create_app

KEY = "test-gateway-key-for-isolation-api"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture(autouse=True)
def _clean_isolation_state(monkeypatch):
    """每个测试都从"没有运行时覆盖"开始，并把 Windows 探测固定下来。

    不加这个 fixture 会让测试互相污染：`_runtime_override` 是模块级全局变量，
    上一个测试设的值会带进下一个 —— 正是那种"单跑绿、全跑红"的经典成因。

    **不动 `internal_token`**：`AuthMiddleware` 在 `create_app()` 时捕获它，改它会让
    中间件 fail-close，请求根本到不了 handler，测出来的 401 与被测逻辑无关。
    """
    monkeypatch.setattr(settings, "llm_gateway_key", KEY)
    monkeypatch.setattr(iso, "_runtime_override", None)
    monkeypatch.setattr(iso.win, "IS_WINDOWS", True)
    monkeypatch.setattr(iso, "_probe_job", lambda: True)
    monkeypatch.setattr(iso, "_probe_low_il", lambda: True)
    monkeypatch.setattr(iso, "_ensure_workspace_low_label", lambda _p: (True, ""))
    # 审计写盘指到临时目录，避免污染仓库内 logs/
    monkeypatch.setattr(iso, "record_level_change", lambda **_kw: None)
    yield
    iso.clear_runtime_level()


async def _client():
    app = create_app()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakeRequest:
    """只实现 `_verify_gateway_key` 用到的那一小块 Request 接口。

    直接测试凭据判定是必要的：经 ASGI 走一遍时，`AuthMiddleware` 会先拦掉
    没有内部 token 的请求，于是"缺凭据"这条分支永远到不了 handler —— 用 HTTP
    测它只能测到中间件，测不到端点本身是否 fail-closed。
    """

    def __init__(self, headers: dict[str, str]):
        self.headers = headers
        self.client = None


# ── 鉴权（handler 层，绕过中间件）────────────────────────────────────────


def test_rejects_when_no_credential_configured(monkeypatch):
    """两个凭据都没配 ⇒ 503。**不能**因为"没配就放宽"而开放端点。"""
    monkeypatch.setattr(settings, "llm_gateway_key", "")
    monkeypatch.setattr(settings, "internal_token", "")

    with pytest.raises(HTTPException) as exc:
        iso_api._verify_gateway_key(_FakeRequest({}))

    assert exc.value.status_code == 503


def test_rejects_wrong_key(monkeypatch):
    """配了密钥但请求带的是错的 ⇒ 401。"""
    monkeypatch.setattr(settings, "internal_token", "")

    with pytest.raises(HTTPException) as exc:
        iso_api._verify_gateway_key(_FakeRequest({"X-API-Key": "wrong"}))

    assert exc.value.status_code == 401


def test_rejects_missing_credential(monkeypatch):
    monkeypatch.setattr(settings, "internal_token", "")

    with pytest.raises(HTTPException) as exc:
        iso_api._verify_gateway_key(_FakeRequest({}))

    assert exc.value.status_code == 401


def test_accepts_gateway_internal_token():
    """网关注入的 `X-Internal-Token` 是前端的主路径，必须认可。

    网关用 `PythonClient.GetJSON` 重建请求，只注入这个头；客户端带来的
    `X-API-Key` 根本到不了引擎。不认它，前端永久 401。
    """
    token = settings.internal_token
    if not token:
        pytest.skip("本环境未配置 internal_token")

    iso_api._verify_gateway_key(_FakeRequest({"X-Internal-Token": token}))


def test_accepts_api_key_for_direct_calls(monkeypatch):
    """直连脚本用 `X-API-Key`（与 `/v1/plugins/*` 同款）。"""
    monkeypatch.setattr(settings, "internal_token", "")

    iso_api._verify_gateway_key(_FakeRequest({"X-API-Key": KEY}))


# ── GET ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_reports_requested_effective_and_floor():
    async with await _client() as ac:
        resp = await ac.get("/v1/system/isolation", headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    for field in ("requested", "effective", "floor", "below_floor", "selectable", "reasons"):
        assert field in body, f"缺少 {field}：前端无法据此渲染选项卡"
    assert body["floor"] == iso.FLOOR_LEVEL.value
    assert body["runtime_override"] is None


# ── PUT ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_switches_level_in_memory_only():
    """切换后状态改变，且**没有**写入运行时覆盖以外的东西。"""
    async with await _client() as ac:
        resp = await ac.put(
            "/v1/system/isolation",
            json={"level": "appcontainer", "reason": "测试切换"},
            headers=HEADERS,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["runtime_override"] == "appcontainer"
    assert iso.runtime_override() is iso.Level.APPCONTAINER


@pytest.mark.asyncio
async def test_put_below_floor_is_raised_and_explained():
    """请求 `none` ⇒ 实际 `low_il`，且 reasons 说明原因（不是静默改写）。"""
    async with await _client() as ac:
        resp = await ac.put(
            "/v1/system/isolation", json={"level": "none"}, headers=HEADERS
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["requested"] == "none", "要如实回报用户请求了什么"
    assert body["effective"] == iso.FLOOR_LEVEL.value, "底线必须生效"
    assert body["reasons"], "抬到底线必须在 reasons 里说明，否则就是静默降级"


@pytest.mark.asyncio
async def test_put_rejects_unknown_level():
    """拼错的档位必须 400，不能猜一个近似的档位继续跑。"""
    async with await _client() as ac:
        resp = await ac.put(
            "/v1/system/isolation", json={"level": "lowil"}, headers=HEADERS
        )

    assert resp.status_code == 400


# ── DELETE ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_clears_override_and_is_idempotent():
    """撤销两次都 200；第二次 `cleared` 为 None。"""
    async with await _client() as ac:
        await ac.put("/v1/system/isolation", json={"level": "appcontainer"}, headers=HEADERS)
        first = await ac.delete("/v1/system/isolation", headers=HEADERS)
        second = await ac.delete("/v1/system/isolation", headers=HEADERS)

    assert first.status_code == 200
    assert first.json()["cleared"] == "appcontainer"
    assert second.status_code == 200
    assert second.json()["cleared"] is None, "幂等：没有覆盖时不该报错，也不该谎报撤销了什么"
    assert iso.runtime_override() is None


# ── revoke ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_returns_disk_artifact_report(monkeypatch):
    """`POST /revoke` 要回报它到底撤销了什么，而不是只回一个 ok。"""

    def fake_revoke():
        return {
            "appcontainer_profile": "已删除",
            "path_grants": [{"path": "x", "removed": True, "message": ""}],
            "integrity_labels": [{"path": "x", "restored_to": "medium", "ok": True, "message": ""}],
        }

    monkeypatch.setattr(iso, "revoke_all", fake_revoke)

    async with await _client() as ac:
        resp = await ac.post("/v1/system/isolation/revoke", headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["appcontainer_profile"] == "已删除"
    assert body["integrity_labels"], "必须回报完整性标签是否已还原"
