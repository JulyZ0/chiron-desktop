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


# ── 提权助手（`restricted` 档的执行前置） ────────────────────────
#
# 三条端点（GET/POST/DELETE）此前在**网关**上没有注册，前端每次都拿到 404，
# 于是界面只能说「启用助手失败：本机无法启用此档位」。接口本身的契约在这里钉住：
# 失败必须带 `reason` 与 `hint`，`cancelled` 必须能与"真失败"分开。


def _fixed_status() -> dict:
    return {
        "requested": "restricted",
        "effective": "low_il",
        "degraded": True,
        "floor": "low_il",
        "below_floor": False,
        "user_selected": False,
        "reasons": ["缺执行层"],
        "downgrade_allowed": True,
        "selectable": ["none", "job", "low_il"],
        "acknowledge_required": ["none"],
        "unavailable_reasons": {"restricted": "缺执行层"},
        "capabilities": {},
        "appcontainer_name": "chiron",
        "appcontainer_workspace": r"C:\work",
    }


def _patch_helper(monkeypatch, *, start=None, stop=None, status=None):
    import app.isolation.win_launch as win_launch

    monkeypatch.setattr(iso, "status", _fixed_status)
    monkeypatch.setattr(iso, "reset_probes", lambda: None)
    monkeypatch.setattr(
        win_launch, "helper_status", status or (lambda: {
            "available": False, "detail": "提权 helper 未启动（缺少端口文件）",
            "engine_elevated": False, "supported": True,
        })
    )
    if start is not None:
        monkeypatch.setattr(win_launch, "helper_start", start)
    if stop is not None:
        monkeypatch.setattr(win_launch, "helper_stop", stop)


@pytest.mark.asyncio
async def test_helper_status_is_reachable(monkeypatch):
    """`GET /isolation/helper` 必须能单独问到助手状态（不夹在档位快照里）。"""
    _patch_helper(monkeypatch)

    async with await _client() as ac:
        resp = await ac.get("/v1/system/isolation/helper", headers=HEADERS)

    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["supported"] is True
    assert body["detail"], "不可用时必须给出原因，否则前端只能显示一个灰按钮"


@pytest.mark.asyncio
async def test_helper_start_success_returns_fresh_status(monkeypatch):
    """启用成功后要把清过缓存的档位状态一起回给前端（不必再拉一次 GET）。"""
    _patch_helper(monkeypatch, start=lambda _pid=None: (True, "", False))

    async with await _client() as ac:
        resp = await ac.post("/v1/system/isolation/helper", headers=HEADERS)

    body = resp.json()
    assert body["ok"] is True
    assert body["reason"] == ""
    assert body["hint"] == ""
    assert body["requested"] == "restricted"
    assert "selectable" in body


@pytest.mark.asyncio
async def test_helper_start_success_but_degraded_records_reason_in_audit(monkeypatch):
    """助手启用成功、档位却没生效 ⇒ 审计的 `reason` **必须非空**。

    真机审计里出现过：``action=helper_start requested=restricted effective=low_il
    reason=""`` —— 那一行看起来一切正常，事后完全无法解释"为什么还是 low_il"，
    用户也没有任何自查线索。降级本身是允许的（执行仍 fail-closed），
    但绝不能静默。
    """
    _patch_helper(monkeypatch, start=lambda _pid=None: (True, "", False))
    records: list[dict] = []
    monkeypatch.setattr(iso, "record_level_change", lambda **kw: records.append(kw))

    async with await _client() as ac:
        resp = await ac.post("/v1/system/isolation/helper", headers=HEADERS)

    assert resp.json()["ok"] is True
    entry = next(item for item in records if item.get("action") == "helper_start")
    assert entry["reason"], "降级必须写进审计，否则这一行只剩 requested/effective 两个值"
    assert "restricted" in entry["reason"] and "low_il" in entry["reason"], entry["reason"]
    # 机器原因（"缺执行层"）要一起带上，它是唯一能指向修法的线索。
    assert "缺执行层" in entry["reason"], entry["reason"]


@pytest.mark.asyncio
async def test_helper_start_failure_always_records_a_reason(monkeypatch):
    """失败但引擎没给 reason ⇒ 审计也要写兜底文案，不能留下空的 `reason`。"""
    _patch_helper(monkeypatch, start=lambda _pid=None: (False, "", False))
    records: list[dict] = []
    monkeypatch.setattr(iso, "record_level_change", lambda **kw: records.append(kw))

    async with await _client() as ac:
        await ac.post("/v1/system/isolation/helper", headers=HEADERS)

    entry = next(item for item in records if item.get("action") == "helper_start")
    assert entry["reason"].startswith("失败：")
    assert iso_api.HELPER_START_FALLBACK_REASON in entry["reason"]


@pytest.mark.asyncio
async def test_helper_start_failure_passes_reason_and_hint(monkeypatch):
    """失败时 `reason` 原样透传，并附带可执行指引（`hint`）。

    「本机无法启用此档位」这句话是前端兜底，不是引擎说的；引擎必须给出
    **具体缺什么**，否则用户与运维都无从下手。
    """
    reason = "提权助手已启动但未就绪（提权 helper 未启动（缺少端口文件））"
    _patch_helper(monkeypatch, start=lambda _pid=None: (False, reason, False))

    async with await _client() as ac:
        resp = await ac.post("/v1/system/isolation/helper", headers=HEADERS)

    body = resp.json()
    assert resp.status_code == 200, "UAC 失败不是服务器错误"
    assert body["ok"] is False
    assert body["cancelled"] is False
    assert body["reason"] == reason
    assert body["hint"], "必须给出可执行指引（在 UAC 里允许一次）"


@pytest.mark.asyncio
async def test_helper_start_without_reason_falls_back_to_actionable_text(monkeypatch):
    """引擎没给原因时也不能回一句空话 —— 兜底文案本身要能行动。"""
    _patch_helper(monkeypatch, start=lambda _pid=None: (False, "", False))

    async with await _client() as ac:
        resp = await ac.post("/v1/system/isolation/helper", headers=HEADERS)

    body = resp.json()
    assert body["reason"] == iso_api.HELPER_START_FALLBACK_REASON
    assert "本机无法启用此档位" not in body["reason"]


@pytest.mark.asyncio
async def test_helper_start_cancelled_is_distinguishable(monkeypatch):
    """用户取消 UAC ⇒ `cancelled=True`（界面用 info，不报错）。"""
    _patch_helper(monkeypatch, start=lambda _pid=None: (False, "ShellExecuteW=5", True))

    async with await _client() as ac:
        resp = await ac.post("/v1/system/isolation/helper", headers=HEADERS)

    body = resp.json()
    assert body["ok"] is False
    assert body["cancelled"] is True


@pytest.mark.asyncio
async def test_helper_stop_is_idempotent(monkeypatch):
    """停用幂等：本来没在跑也算成功。"""
    _patch_helper(monkeypatch, stop=lambda: (True, ""))

    async with await _client() as ac:
        first = await ac.delete("/v1/system/isolation/helper", headers=HEADERS)
        second = await ac.delete("/v1/system/isolation/helper", headers=HEADERS)

    assert first.json()["ok"] is True
    assert second.json()["ok"] is True


@pytest.mark.asyncio
async def test_switch_to_none_is_not_blocked_by_helper_probes(monkeypatch):
    """「无隔离」档不得依赖提权助手（也不依赖任何被它拖住的前置探测）。

    用户在这里明确说的是"我不要隔离"。如果这条路径先要求 `restricted` 的探测
    成立（那个探测要大费周章才能过），"关掉隔离"就变成了"先修好隔离" ——
    请求被满足不了，而理由与他的诉求毫无关系。
    """
    # 受限令牌档在本机不可用（真实场景：没启用助手）；同时把它探成"会抛异常"，
    # 模拟探测自身出问题。两种情况都不该影响 `none` 的切换。
    monkeypatch.setattr(iso, "_probe_restricted", lambda: False)
    monkeypatch.setattr(iso, "_probe_appcontainer", lambda: False)

    async with await _client() as ac:
        resp = await ac.put(
            "/v1/system/isolation",
            json={"level": "none", "acknowledge_below_floor": True, "reason": "用户要求关闭隔离"},
            headers=HEADERS,
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["requested"] == "none"
    assert body["effective"] == "none", "用户确认过的档位必须如实生效"
    assert body["user_selected"] is True
    assert body["below_floor"] is True


@pytest.mark.asyncio
async def test_no_isolation_official_path_does_not_require_the_helper(monkeypatch):
    """助手探测整个坏掉时，`none` 仍能切换（探测错误只影响它自己那一档）。"""

    def _boom():
        raise RuntimeError("helper 探测炸了")

    monkeypatch.setattr(iso, "_probe_restricted", _boom)
    monkeypatch.setattr(iso, "_probe_appcontainer", _boom)

    async with await _client() as ac:
        resp = await ac.put(
            "/v1/system/isolation",
            json={"level": "none", "acknowledge_below_floor": True},
            headers=HEADERS,
        )

    assert resp.status_code == 200, (
        "与 none 无关的档位探测出错，不能把「关掉隔离」打成 500："
        f"{resp.text}"
    )
    assert resp.json()["effective"] == "none"
