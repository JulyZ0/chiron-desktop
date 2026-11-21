"""内网重跑端点 ``POST /v1/internal/subagent/runs/{run_id}/rerun``（P0-2）。

这条端点是**前端面板「重试」按钮**的真实通道：子 Agent 失败/被停/撞预算墙之后，
用户点一下就要真的再跑一次，而不是"把摘要放回输入框再说"。因此这里钉住四件事：

1. 状态码口径可区分：404 不存在 / 409 非终态或没有原任务 / 503 能力不可用 ——
   前端据此决定"重试"还是"回退到输入框"，假成功会让人永远等一个不会来的 run；
2. 身份必填（tenant/user/session）：内网端点也不接受"无归属"的重跑；
3. 成功时返回**新 run id**（立即返回，后台跑）；
4. 任务描述来自库里那一列：请求体里**没有** task 字段（不给伪造上下文的口子）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import app.api.internal_subagent as internal_api
from app.subagent import rerun as rerun_impl
from app.subagent.rerun import RerunRefusedError

BODY = {"tenant_id": 1, "user_id": "u1", "session_id": "s1"}

#: 冲刷循环的**原始实现**：autouse fixture 会把它换成替身（避免后台任务空转），
#: 但"它自己会不会退出"必须测得着，所以在这里留一份真身。
REAL_DRAIN = internal_api._drain_sink_until_done


class _StubSink:
    """替身 sink：真实 EventSink 会起一个常驻投递任务，测试里只想看"接线对不对"。"""

    def __init__(self, *, session_id: str = "", **_: object) -> None:
        self.session_id = session_id
        self.deliverers: list = []
        self.drained = 0

    def attach_persistent(self, deliver) -> None:
        self.deliverers.append(deliver)

    def drain(self) -> list:
        self.drained += 1
        return []


@pytest.fixture(autouse=True)
def _stub_environment(monkeypatch):
    """引擎网关与 sink 都换成替身：本用例只验证端点自身的判定与映射。"""
    # 端点内部是**延迟导入**（`from app.agent.event_sink import EventSink`），
    # 所以要打在源模块上，打 `internal_api.EventSink` 是打不到的。
    import app.agent.event_sink as event_sink_mod

    monkeypatch.setattr(event_sink_mod, "EventSink", _StubSink)

    async def _gateway():
        return object()

    import app.main as main_mod

    monkeypatch.setattr(main_mod, "get_gateway", _gateway)
    # 冲刷任务：不要让它在测试里空转
    monkeypatch.setattr(internal_api, "_drain_sink_until_done", _noop_drain)
    yield


async def _noop(*_args, **_kwargs):
    return None


async def _noop_drain(_sink, _run_id):
    return None


def _client() -> TestClient:
    """只挂本路由的裸 app：验证路径暴露 + 请求体契约（不掺认证中间件）。"""
    app = FastAPI()
    app.include_router(internal_api.router)
    return TestClient(app)


def test_route_is_exposed():
    resp = _client().post("/v1/internal/subagent/runs/rs_old/rerun", json=BODY)
    # 走到 handler 了（未 mock rerun_run ⇒ DB 不可用，503 而不是 404/405）
    assert resp.status_code in (503, 500), resp.text


def test_missing_identity_is_rejected():
    resp = _client().post(
        "/v1/internal/subagent/runs/rs_old/rerun", json={"session_id": "s1"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "identity_required"


def test_missing_session_is_rejected():
    resp = _client().post(
        "/v1/internal/subagent/runs/rs_old/rerun",
        json={"tenant_id": 1, "user_id": "u1"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["code"] == "session_required"


def test_request_body_has_no_task_field():
    """重跑的任务只能来自库里 —— 请求体带 task 不生效（不给伪造上下文的口子）。"""
    assert "task" not in internal_api.InternalSubagentRerunRequest.model_fields
    assert "task_override" not in internal_api.InternalSubagentRerunRequest.model_fields


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code,status",
    [
        ("not_found", 404),
        ("run_not_terminal", 409),
        ("task_missing", 409),
        ("store_unavailable", 503),
    ],
)
async def test_refusals_map_to_http_codes(monkeypatch, code, status):
    """每种拒绝都必须落到它自己的状态码上（前端靠它区分"回退"与"稍后再试"）。"""

    async def _refuse(**_kwargs):
        raise RerunRefusedError(status, f"refused:{code}", code)

    monkeypatch.setattr(rerun_impl, "rerun_run", _refuse)

    with pytest.raises(HTTPException) as exc:
        await internal_api.internal_subagent_rerun(
            "rs_old", internal_api.InternalSubagentRerunRequest(**BODY)
        )
    assert exc.value.status_code == status
    assert exc.value.detail["code"] == code


@pytest.mark.asyncio
async def test_success_returns_new_run_id_and_sets_context(monkeypatch):
    """成功：立即返回新 run id，且派发时上下文里带齐身份（供 subagent() 读取）。"""
    seen: dict = {}

    async def _ok(**_kwargs):
        from app.tools.context import get_all

        seen.update(get_all())
        return {"status": "async_launched", "run_id": "rs_new", "rerun_of": "rs_old",
                "parent_run_id": "rs_old", "depth": 2}

    monkeypatch.setattr(rerun_impl, "rerun_run", _ok)

    payload = await internal_api.internal_subagent_rerun(
        "rs_old", internal_api.InternalSubagentRerunRequest(**BODY)
    )

    assert payload["run_id"] == "rs_new" and payload["rerun_of"] == "rs_old"
    assert payload["ok"] is True
    assert seen.get("session_id") == "s1"
    assert str(seen.get("tenant_id")) == "1"
    assert seen.get("user_id") == "u1"
    assert seen.get("gateway") is not None
    assert isinstance(seen.get("event_sink"), _StubSink), "必须挂上事件旁路，否则前端看不到新 run 的进度"
    assert seen.get("unattended") is True

    # 上下文必须还原：否则同一请求链上的后续调用会继承"无人值守 + 这份 sink"
    from app.tools.context import get_all

    assert get_all().get("session_id") in (None, "")


@pytest.mark.asyncio
async def test_event_delivery_uses_session_and_tenant(monkeypatch):
    """事件投递走 Stream + pub/sub，且 tenant 口径取自请求身份（不能跨租户写缓存）。"""
    calls: list[tuple[str, str]] = []

    class _Cache:
        async def push_event(self, *, run_id, tenant, payload):
            calls.append(("push", f"{run_id}:{tenant}"))

        async def publish_live_event(self, *, payload):
            calls.append(("publish", payload["session_id"]))

    await internal_api._deliver_rerun_event(
        {"type": "subagent.started", "run_id": "rs_new", "session_id": "s1"}, _Cache(), "1"
    )

    assert calls == [("push", "rs_new:1"), ("publish", "s1")]


@pytest.mark.asyncio
async def test_rerun_sink_drain_stops_when_run_is_gone(monkeypatch):
    """冲刷循环在 run 结束后停止（否则每个重跑都会留下一个永久空转的任务）。"""
    from app.subagent import registry as subagent_registry

    monkeypatch.setattr(subagent_registry, "is_active", lambda _rid: False)
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)

    sink = _StubSink(session_id="s1")
    await REAL_DRAIN(sink, "rs_new")
    assert sink.drained >= 1


async def _instant_sleep(_delay):
    return None
