"""H26 回归：会话写入入口必须携带可信的 owner 身份。

PRD 9.7 H26 验收：submit / quick-execute / load 均以
``(tenant_id, user_id, session_id)`` 为所有权键；跨租户或跨用户会话不得
写入任何消息或上下文。

本文件锁定两类此前会失败的行为：

1. **写入口的 fail-open 退化**。``/v1/chat/submit``（Go 侧 ``/v1/quick-execute``
   是同义别名，代理到同一入口）在**不带任何 query 身份**时会被鉴权中间件当成
   "body 身份端点"放行（见 ``app/middleware/auth.py`` 的分支说明），随后
   ``submit_task`` 退化为 ``owner_user_id = str(user_id or tenant_id)`` ——
   于是同租户里所有"只带 body tenant_id"的调用者共享同一个 owner 域
   （DB 里就是 tenant 级公共会话，可互相续写）。会话写入入口必须 fail-closed。
2. **空白身份**：``user_id="   "`` 曾被当作有效身份，落进同一个"空白"域。
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.api import unified_executor
from app.api.unified_executor import UnifiedChatHandler
from app.config import settings

OWNER_TENANT_ID = 41
OWNER_USER_ID = "0f8fad5b-d9cb-469f-a165-0ddeb9fca4b2"
OWNER_SESSION_ID = "2d48966d-8494-4d9d-bd1c-3f5c0bb38e8a"


class _State:
    """最小 request.state 替身（Starlette State 只是属性容器）。"""

    def __init__(self, user_id: str = "") -> None:
        self.user_id = user_id


class _Request:
    def __init__(
        self,
        query: dict[str, str],
        body: dict[str, object],
        *,
        state_user: str = "",
    ) -> None:
        self.query_params = query
        self._body = body
        self.state = _State(state_user)

    async def json(self):
        return self._body


def _mocked_handler(monkeypatch) -> AsyncMock:
    handler = AsyncMock()
    handler.submit_task = AsyncMock(return_value={"success": True, "session_id": "s"})
    monkeypatch.setattr(unified_executor, "get_chat_handler", lambda: handler)
    return handler


class _Pool:
    """只回放给定行序列的 pool 替身，同时记录所有写操作。"""

    def __init__(self, rows) -> None:
        self._rows = list(rows)
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        return self._rows.pop(0) if self._rows else None

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1"

    async def fetch(self, query, *args):
        return []


def _foreign_row() -> dict[str, object]:
    return {
        "id": OWNER_SESSION_ID,
        "tenant_id": OWNER_TENANT_ID,
        "user_id": OWNER_USER_ID,
        "title": "private session",
        "mode": "auto",
        "shared_context": {"private": "context"},
        "created_at": 1.0,
        "updated_at": 2.0,
    }


# ── 1) 写入口 fail-closed ────────────────────────────────────────────


async def test_submit_route_rejects_missing_user_identity(monkeypatch):
    handler = _mocked_handler(monkeypatch)

    result = await unified_executor.submit_chat(
        _Request({"tenant_id": str(OWNER_TENANT_ID)}, {"message": "hi"})
    )

    assert result["success"] is False
    assert result["error"] == "user_id is required"
    handler.submit_task.assert_not_awaited()


async def test_submit_route_rejects_blank_user_identity(monkeypatch):
    handler = _mocked_handler(monkeypatch)

    result = await unified_executor.submit_chat(
        _Request(
            {"tenant_id": str(OWNER_TENANT_ID), "user_id": "   "},
            {"message": "hi"},
        )
    )

    assert result["success"] is False
    assert result["error"] == "user_id is required"
    handler.submit_task.assert_not_awaited()


async def test_submit_route_rejects_zero_tenant(monkeypatch):
    handler = _mocked_handler(monkeypatch)

    result = await unified_executor.submit_chat(
        _Request({"tenant_id": "0", "user_id": OWNER_USER_ID}, {"message": "hi"})
    )

    assert result["success"] is False
    assert result["error"] == "tenant_id is required"
    handler.submit_task.assert_not_awaited()


async def test_submit_route_body_only_tenant_cannot_write(monkeypatch):
    """真实 HTTP 面：只带 body tenant_id（无 query 身份）不得触达 handler。"""
    handler = _mocked_handler(monkeypatch)
    headers = {"X-Internal-Token": settings.internal_token}

    async with AsyncClient(
        transport=ASGITransport(app=app.main.create_app()),
        base_url="http://test",
        headers=headers,
    ) as client:
        resp = await client.post(
            "/v1/chat/submit",
            json={
                "tenant_id": OWNER_TENANT_ID,
                "session_id": OWNER_SESSION_ID,
                "message": "anonymous write",
            },
        )

    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["error"] == "user_id is required"
    handler.submit_task.assert_not_awaited()


async def test_submit_route_uses_authenticated_request_state_identity(monkeypatch):
    """middleware 已验证的身份也构成可信来源（与消息读取端点同口径）。"""
    handler = _mocked_handler(monkeypatch)

    await unified_executor.submit_chat(
        _Request(
            {"tenant_id": str(OWNER_TENANT_ID)},
            {"message": "hi"},
            state_user=OWNER_USER_ID,
        )
    )

    assert handler.submit_task.await_args.kwargs["user_id"] == OWNER_USER_ID
    assert handler.submit_task.await_args.kwargs["tenant_id"] == OWNER_TENANT_ID


# ── 2) 匿名调用者不得挂到具名用户的会话 ──────────────────────────────


async def test_anonymous_caller_cannot_reattach_named_session(monkeypatch):
    pool = _Pool([_foreign_row()])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock()

    result = await handler.submit_task(
        "anonymous takeover attempt",
        tenant_id=OWNER_TENANT_ID,
        user_id="",
        session_id=OWNER_SESSION_ID,
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert handler.sessions == {}
    handler.router.route_task.assert_not_awaited()
    assert all(
        not call[0].startswith(("INSERT INTO unified_messages", "UPDATE unified_sessions"))
        for call in pool.fetchrow_calls
    )


async def test_anonymous_owner_scope_differs_from_named_user_scope(monkeypatch):
    """内存兜底路径也必须按 user 分域，而不是退化成 tenant 公共域。"""

    def _no_db():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(unified_executor, "get_pool", _no_db)
    monkeypatch.setattr(unified_executor, "publish_result", AsyncMock())
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(
        return_value={"status": "completed", "subtasks": [], "output": {}}
    )

    anonymous = await handler.submit_task("anonymous input", tenant_id=OWNER_TENANT_ID)
    assert anonymous["success"] is True
    session_id = anonymous["session_id"]
    assert (str(OWNER_TENANT_ID), str(OWNER_TENANT_ID), session_id) in handler.sessions

    named = await handler.submit_task(
        "named input",
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
        session_id=session_id,
    )
    assert named["success"] is False
    assert named["error_code"] == "not_found"
    assert (str(OWNER_TENANT_ID), OWNER_USER_ID, session_id) not in handler.sessions
    assert handler.router.route_task.await_count == 1


@pytest.mark.parametrize("user_id", ["", "   "])
async def test_message_read_rejects_missing_or_blank_identity(monkeypatch, user_id):
    monkeypatch.setattr(unified_executor, "get_pool", lambda: None)
    handler = UnifiedChatHandler()

    result = await handler.get_session_messages(
        OWNER_SESSION_ID, tenant_id=OWNER_TENANT_ID, user_id=user_id
    )

    assert result["success"] is False
    assert result["error"] == "user_id is required"
