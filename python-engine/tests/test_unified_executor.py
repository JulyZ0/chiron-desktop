"""统一执行器 (UnifiedChatHandler) 测试。

核心意图 (fail loud):
- 已实现路径 (auto / agent) 必须返回真实执行结果,绝不接受伪造的固定文案
- 执行错误 (gateway 缺失 / LLM 报错) 必须传播为 success=False,不得被吞掉
"""
from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.api import unified_executor
from app.api.unified_executor import ChatSession, UnifiedChatHandler, get_chat_handler
from app.config import settings

TENANT = "tenant-exec-test"


# ── 测试替身 ────────────────────────────────────────────────────────


class _Chunk:
    """SubAgent.run 消费的流式 chunk 形状"""

    def __init__(self, content: str = "", finish_reason: str | None = None):
        self.content = content
        self.tool_calls = None
        self.input_tokens = 0
        self.output_tokens = 0
        self.finish_reason = finish_reason


class _FakeGateway:
    """单次返回固定内容的 gateway"""

    def __init__(self, content: str = "agent-real-output"):
        self._content = content

    async def chat_stream(self, **kwargs):
        yield _Chunk(content=self._content, finish_reason="stop")


class _ErrorGateway:
    """LLM 返回 error 的 gateway"""

    async def chat_stream(self, **kwargs):
        yield _Chunk(finish_reason="error")


def _patch_gateway(monkeypatch, gateway):
    async def _get_gateway():
        return gateway

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)


def _patch_gateway_unavailable(monkeypatch):
    async def _get_gateway():
        raise RuntimeError("Gateway not initialized")

    monkeypatch.setattr(app.main, "get_gateway", _get_gateway)


# ── 请求解析与会话管理 ──────────────────────────────────────────────


async def test_submit_creates_session_and_records_history():
    """提交任务必须创建会话、生成 trace_id 并写入用户/助手消息历史。"""
    handler = UnifiedChatHandler()
    user_input = "帮我分析一下这份销售数据"

    res = await handler.submit_task(user_input=user_input, tenant_id=TENANT)

    assert res["success"] is True
    assert res["trace_id"], "未提供 trace_id 时必须自动生成"
    session_id = res["session_id"]
    session = handler.sessions[_cache_key(TENANT, TENANT, session_id)]
    assert session.tenant_id == TENANT
    assert session.title == user_input[:50]
    assert [m["role"] for m in session.messages] == ["user", "assistant"]
    assert session.shared_context["last_trace_id"] == res["trace_id"]


async def test_submit_reuses_existing_session():
    """显式传入的 session_id 必须复用而非新建。"""
    handler = UnifiedChatHandler()
    first = await handler.submit_task(user_input="第一条", tenant_id=TENANT)
    second = await handler.submit_task(
        user_input="第二条", tenant_id=TENANT, session_id=first["session_id"]
    )
    assert second["session_id"] == first["session_id"]
    # 两次对话都应留在同一会话历史中 (2 轮 user+assistant)
    assert len(handler.sessions[_cache_key(TENANT, TENANT, first["session_id"])].messages) == 4


async def test_get_session_messages_not_found():
    """查询不存在的会话必须明确报错,而非返回空列表冒充成功。"""
    handler = UnifiedChatHandler()
    res = await handler.get_session_messages(
        session_id="missing-session", tenant_id=TENANT, user_id=TENANT
    )
    assert res["success"] is False
    assert "error" in res


async def test_get_session_messages_requires_tenant():
    """S 安全修复:不提供 tenant_id 必须被拒绝,杜绝无归属读取。"""
    handler = UnifiedChatHandler()
    res = await handler.get_session_messages(session_id="missing-session")
    assert res["success"] is False
    assert res["error"] == "tenant_id is required"


async def test_get_session_messages_returns_history():
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="你好", tenant_id=TENANT)
    history = await handler.get_session_messages(
        session_id=res["session_id"], tenant_id=TENANT, user_id=TENANT
    )
    assert history["success"] is True
    assert history["messages"][0]["role"] == "user"
    assert "shared_context" in history


async def test_get_session_messages_requires_user():
    """不提供 user_id 必须被拒绝：只校验租户时,同租户内可跨用户读消息。"""
    handler = UnifiedChatHandler()
    res = await handler.get_session_messages(session_id="s", tenant_id=TENANT)
    assert res["success"] is False
    assert res["error"] == "user_id is required"


async def test_get_session_messages_cross_tenant_blocked():
    """S 安全修复:其他租户不得读取本租户会话消息(跨租户隔离)。"""
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="机密消息", tenant_id=TENANT)
    blocked = await handler.get_session_messages(
        session_id=res["session_id"], tenant_id="other-tenant", user_id=TENANT
    )
    assert blocked["success"] is False
    assert blocked["error"] == "Session not found"


async def test_get_session_messages_cross_user_in_same_tenant_blocked():
    """S 安全修复:同租户的**其他用户**也不得读取本会话消息。

    会话归属是用户级的,租户只是它的外边界 —— 只校验 tenant 时,
    同租户用户只要拿到 session_id(或枚举它)就能读走别人的完整会话。
    """
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="同一租户的机密消息", tenant_id=TENANT)
    blocked = await handler.get_session_messages(
        session_id=res["session_id"], tenant_id=TENANT, user_id="another-user"
    )
    assert blocked["success"] is False
    assert blocked["error"] == "Session not found"


async def test_get_session_messages_cross_user_db_path_blocked():
    """DB 路径的归属查询必须同时带 tenant_id 与 user_id(不能只带 tenant)。"""
    from unittest.mock import AsyncMock, patch

    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=None)

    handler = UnifiedChatHandler()
    with patch("app.api.unified_executor.get_pool", return_value=pool):
        res = await handler.get_session_messages(
            session_id="s-1", tenant_id="t-1", user_id="u-1"
        )

    assert res["success"] is False
    query = pool.fetchrow.call_args[0][0]
    assert "tenant_id = $2" in query
    assert "user_id = $3" in query, "DB 路径缺少 user 归属条件"


# ── H26 session owner enforcement ──────────────────────────────────


OWNER_SESSION_ID = str(UUID("2d48966d-8494-4d9d-bd1c-3f5c0bb38e8a"))
OWNER_USER_ID = str(UUID("0f8fad5b-d9cb-469f-a165-0ddeb9fca4b2"))
OTHER_USER_ID = str(UUID("7a6a35b2-18b4-4e8f-a4f6-29d5567b624d"))
OWNER_TENANT_ID = 17
OTHER_TENANT_ID = 18


def _cache_key(tenant_id, user_id, session_id):
    return (str(tenant_id), str(user_id or tenant_id), session_id)


class _SessionPool:
    def __init__(self, rows, *, fetchrow_error: Exception | None = None):
        self._rows = list(rows)
        self._default_row = self._rows[-1] if self._rows else None
        self._fetchrow_error = fetchrow_error
        self.fetchrow_calls: list[tuple[str, tuple]] = []
        self.execute_calls: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *args):
        self.fetchrow_calls.append((query, args))
        if self._fetchrow_error is not None:
            raise self._fetchrow_error
        if self._rows:
            result = self._rows.pop(0)
        else:
            result = self._default_row
        if isinstance(result, Exception):
            raise result
        return result

    async def execute(self, query, *args):
        self.execute_calls.append((query, args))
        return "INSERT 0 1"

    async def fetch(self, query, *args):
        self.fetch_calls.append((query, args))
        return []


def _owned_session_row(*, tenant_id=OWNER_TENANT_ID, user_id=OWNER_USER_ID):
    return {
        "id": OWNER_SESSION_ID,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "title": "private session",
        "mode": "auto",
        "shared_context": {"private": "context"},
        "created_at": 1.0,
        "updated_at": 2.0,
    }


def _successful_router_result():
    return {
        "task_id": "task-owner-test",
        "status": "completed",
        "total_duration_ms": 1,
        "subtasks": [],
        "output": {"outputs": [{"output": {"answer": "ok"}}]},
    }


def _mock_session_writes(handler, monkeypatch):
    writes = {
        name: AsyncMock()
        for name in ("_db_ensure_session", "_db_append_message", "_db_touch_session")
    }
    for name, mock in writes.items():
        monkeypatch.setattr(handler, name, mock)
    return writes


@pytest.mark.parametrize(
    ("tenant_id", "user_id"),
    [
        (OTHER_TENANT_ID, OWNER_USER_ID),
        (OWNER_TENANT_ID, OTHER_USER_ID),
    ],
)
async def test_submit_rejects_cached_session_for_other_owner(monkeypatch, tenant_id, user_id):
    handler = UnifiedChatHandler()
    session = ChatSession(
        session_id=OWNER_SESSION_ID,
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
        mode="auto",
        messages=[{"role": "user", "content": "private message"}],
        updated_at=123.0,
        shared_context={"private": "context"},
    )
    handler.sessions[_cache_key(OWNER_TENANT_ID, OWNER_USER_ID, OWNER_SESSION_ID)] = session
    before = deepcopy((session.messages, session.shared_context, session.mode, session.updated_at))
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())
    writes = _mock_session_writes(handler, monkeypatch)

    result = await handler.submit_task(
        "attacker input",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=OWNER_SESSION_ID,
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert result["error"] == "Session not found"
    assert (session.messages, session.shared_context, session.mode, session.updated_at) == before
    handler.router.route_task.assert_not_awaited()
    for write in writes.values():
        write.assert_not_awaited()


@pytest.mark.parametrize(
    ("row", "tenant_id", "user_id"),
    [
        (_owned_session_row(tenant_id=OTHER_TENANT_ID), OWNER_TENANT_ID, OWNER_USER_ID),
        (_owned_session_row(user_id=OTHER_USER_ID), OWNER_TENANT_ID, OWNER_USER_ID),
    ],
)
async def test_submit_rejects_restored_session_for_other_owner(
    monkeypatch, row, tenant_id, user_id
):
    pool = _SessionPool([row])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())
    writes = _mock_session_writes(handler, monkeypatch)

    result = await handler.submit_task(
        "attacker input",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=OWNER_SESSION_ID,
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert pool.fetchrow_calls[0][1] == (OWNER_SESSION_ID, tenant_id, user_id)
    assert "tenant_id = $2" in pool.fetchrow_calls[0][0]
    assert "user_id = $3" in pool.fetchrow_calls[0][0]
    assert _cache_key(tenant_id, user_id, OWNER_SESSION_ID) not in handler.sessions
    handler.router.route_task.assert_not_awaited()
    for write in writes.values():
        write.assert_not_awaited()


@pytest.mark.parametrize(
    ("recheck_result", "error_code"),
    [(None, "not_found"), (RuntimeError("db unavailable"), "session_unavailable")],
)
async def test_submit_rechecks_restored_owner_before_any_write(
    monkeypatch, recheck_result, error_code
):
    pool = _SessionPool([_owned_session_row(), recheck_result])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())
    writes = _mock_session_writes(handler, monkeypatch)

    result = await handler.submit_task(
        "input after ownership change",
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
        session_id=OWNER_SESSION_ID,
    )

    assert result["success"] is False
    assert result["error_code"] == error_code
    assert len(pool.fetchrow_calls) == 2
    assert all(call[1] == (OWNER_SESSION_ID, OWNER_TENANT_ID, OWNER_USER_ID) for call in pool.fetchrow_calls)
    assert _cache_key(OWNER_TENANT_ID, OWNER_USER_ID, OWNER_SESSION_ID) not in handler.sessions
    handler.router.route_task.assert_not_awaited()
    for write in writes.values():
        write.assert_not_awaited()


async def test_submit_restores_owned_uuid_and_scopes_every_db_write(monkeypatch):
    pool = _SessionPool([_owned_session_row()])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    monkeypatch.setattr(unified_executor, "publish_result", AsyncMock())
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())

    result = await handler.submit_task(
        "continue my session",
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
        session_id=OWNER_SESSION_ID,
    )

    assert result["success"] is True
    session = handler.sessions[_cache_key(OWNER_TENANT_ID, OWNER_USER_ID, OWNER_SESSION_ID)]
    assert session.user_id == OWNER_USER_ID
    assert [message["role"] for message in session.messages] == ["user", "assistant"]
    write_calls = [
        call
        for call in [*pool.execute_calls, *pool.fetchrow_calls]
        if call[0].startswith("INSERT INTO unified_messages")
        or call[0].startswith("UPDATE unified_sessions")
    ]
    assert len(write_calls) == 3
    for query, args in write_calls:
        assert "tenant_id = $2" in query
        assert "user_id = $3" in query
        assert args[:3] == (OWNER_SESSION_ID, OWNER_TENANT_ID, OWNER_USER_ID)


async def test_submit_foreign_session_conflict_writes_nothing(monkeypatch):
    pool = _SessionPool([None, None])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())

    result = await handler.submit_task(
        "take over an existing foreign id",
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
        session_id=OWNER_SESSION_ID,
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert handler.sessions == {}
    assert len(pool.fetchrow_calls) == 2
    assert len(pool.execute_calls) == 1
    assert pool.execute_calls[0][0].startswith("INSERT INTO unified_sessions")
    handler.router.route_task.assert_not_awaited()


async def test_submit_db_failure_uses_owner_scoped_memory_session(monkeypatch):
    def _db_unavailable():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(unified_executor, "get_pool", _db_unavailable)
    monkeypatch.setattr(unified_executor, "publish_result", AsyncMock())
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())

    owned = await handler.submit_task(
        "input while db is unavailable",
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
        session_id=OWNER_SESSION_ID,
    )
    assert owned["success"] is True
    owner_key = _cache_key(OWNER_TENANT_ID, OWNER_USER_ID, OWNER_SESSION_ID)
    session = handler.sessions[owner_key]
    before = deepcopy((session.messages, session.shared_context))

    blocked = await handler.submit_task(
        "cross-user input with the same uuid",
        tenant_id=OWNER_TENANT_ID,
        user_id=OTHER_USER_ID,
        session_id=OWNER_SESSION_ID,
    )

    assert blocked["success"] is False
    assert blocked["error_code"] == "not_found"
    assert (session.messages, session.shared_context) == before
    assert _cache_key(OWNER_TENANT_ID, OTHER_USER_ID, OWNER_SESSION_ID) not in handler.sessions
    assert handler.router.route_task.await_count == 1

    owned_history = await handler.get_session_messages(
        OWNER_SESSION_ID,
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
    )
    blocked_history = await handler.get_session_messages(
        OWNER_SESSION_ID,
        tenant_id=OWNER_TENANT_ID,
        user_id=OTHER_USER_ID,
    )
    assert owned_history["success"] is True
    assert len(owned_history["messages"]) == 2
    assert blocked_history["success"] is False
    assert blocked_history["error_code"] == "not_found"
    assert (session.messages, session.shared_context) == before


@pytest.mark.parametrize("operation", ["message", "context"])
async def test_db_message_and_context_writes_require_owner(monkeypatch, operation):
    pool = _SessionPool([None])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()

    with pytest.raises(unified_executor._SessionNotFoundError):
        if operation == "message":
            await handler._db_append_message(
                OWNER_SESSION_ID,
                OWNER_TENANT_ID,
                OWNER_USER_ID,
                "user",
                "private input",
                {},
            )
        else:
            await handler._db_touch_session(
                OWNER_SESSION_ID,
                OWNER_TENANT_ID,
                OWNER_USER_ID,
                "auto",
                {"private": "context"},
            )

    query, args = pool.fetchrow_calls[0]
    assert "tenant_id = $2" in query
    assert "user_id = $3" in query
    assert args[:3] == (OWNER_SESSION_ID, OWNER_TENANT_ID, OWNER_USER_ID)


@pytest.mark.parametrize(
    ("tenant_id", "user_id"),
    [
        (OTHER_TENANT_ID, OWNER_USER_ID),
        (OWNER_TENANT_ID, OTHER_USER_ID),
    ],
)
async def test_load_uuid_for_other_owner_is_uniform_not_found(monkeypatch, tenant_id, user_id):
    pool = _SessionPool([None])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()

    result = await handler.get_session_messages(
        OWNER_SESSION_ID,
        tenant_id=tenant_id,
        user_id=user_id,
    )

    assert result["success"] is False
    assert result["error_code"] == "not_found"
    assert result["error"] == "Session not found"
    assert pool.fetchrow_calls[0][1] == (OWNER_SESSION_ID, tenant_id, user_id)
    assert pool.fetch_calls == []


async def test_load_message_query_keeps_owner_scope(monkeypatch):
    row = _owned_session_row()
    pool = _SessionPool([row])
    monkeypatch.setattr(unified_executor, "get_pool", lambda: pool)
    handler = UnifiedChatHandler()

    result = await handler.get_session_messages(
        OWNER_SESSION_ID,
        tenant_id=OWNER_TENANT_ID,
        user_id=OWNER_USER_ID,
    )

    assert result["success"] is True
    query, args = pool.fetch_calls[0]
    assert "JOIN unified_sessions" in query
    assert "tenant_id = $2" in query
    assert "user_id = $3" in query
    assert args[:3] == (OWNER_SESSION_ID, OWNER_TENANT_ID, OWNER_USER_ID)


async def test_submit_chat_rejects_query_body_user_mismatch(monkeypatch):
    class _Request:
        query_params = {"tenant_id": str(OWNER_TENANT_ID), "user_id": OWNER_USER_ID}

        async def json(self):
            return {
                "message": "input",
                "tenant_id": OWNER_TENANT_ID,
                "user_id": OTHER_USER_ID,
            }

    handler = AsyncMock()
    monkeypatch.setattr(unified_executor, "get_chat_handler", lambda: handler)

    result = await unified_executor.submit_chat(_Request())

    assert result["success"] is False
    assert result["error"] == "user_id mismatch"
    handler.submit_task.assert_not_awaited()


async def test_http_cross_user_session_access_writes_nothing(monkeypatch):
    def _db_unavailable():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(unified_executor, "get_pool", _db_unavailable)
    monkeypatch.setattr(unified_executor, "publish_result", AsyncMock())
    handler = UnifiedChatHandler()
    handler.router.route_task = AsyncMock(return_value=_successful_router_result())
    monkeypatch.setattr(unified_executor, "get_chat_handler", lambda: handler)
    headers = {"X-Internal-Token": settings.internal_token}
    owner_params = {"tenant_id": str(OWNER_TENANT_ID), "user_id": OWNER_USER_ID}
    other_params = {**owner_params, "user_id": OTHER_USER_ID}

    async with AsyncClient(
        transport=ASGITransport(app=app.main.create_app()),
        base_url="http://test",
        headers=headers,
    ) as client:
        owned = await client.post(
            "/v1/chat/submit",
            params=owner_params,
            json={"session_id": OWNER_SESSION_ID, "message": "private message"},
        )
        assert owned.status_code == 200
        assert owned.json()["success"] is True
        session = handler.sessions[_cache_key(OWNER_TENANT_ID, OWNER_USER_ID, OWNER_SESSION_ID)]
        before = deepcopy((session.messages, session.shared_context, session.mode, session.updated_at))

        blocked_submit = await client.post(
            "/v1/chat/submit",
            params=other_params,
            json={"session_id": OWNER_SESSION_ID, "message": "must not be written"},
        )
        assert blocked_submit.status_code == 200
        assert blocked_submit.json()["success"] is False
        assert blocked_submit.json()["error_code"] == "not_found"
        assert blocked_submit.json()["error"] == "Session not found"

        blocked_history = await client.get(
            f"/v1/chat/sessions/{OWNER_SESSION_ID}/messages", params=other_params
        )
        assert blocked_history.status_code == 200
        assert blocked_history.json()["success"] is False
        assert blocked_history.json()["error_code"] == "not_found"
        assert "messages" not in blocked_history.json()

        owned_history = await client.get(
            f"/v1/chat/sessions/{OWNER_SESSION_ID}/messages", params=owner_params
        )
        assert owned_history.status_code == 200
        assert owned_history.json()["success"] is True
        assert owned_history.json()["messages"] == session.messages

    assert (session.messages, session.shared_context, session.mode, session.updated_at) == before
    assert _cache_key(OWNER_TENANT_ID, OTHER_USER_ID, OWNER_SESSION_ID) not in handler.sessions
    assert handler.router.route_task.await_count == 1


# ── auto 模式 (TaskRouter,已实现) ───────────────────────────────────


async def test_auto_mode_runs_task_router_end_to_end():
    """auto 模式必须真实走 TaskRouter 编排并返回成功状态。

    无 LLM gateway 时 TaskRouter 降级到关键词意图识别,
    但整条链路必须完整返回而非抛错或伪造结果。
    """
    handler = UnifiedChatHandler()
    res = await handler.submit_task(user_input="搜索一下相关资料", tenant_id=TENANT)
    assert res["success"] is True
    assert res["metadata"]["task_id"], "TaskRouter 必须返回真实 task_id"
    # 输出必须是 TaskRouter 聚合结果的序列化,而非伪造固定文案
    assert "Agent 执行结果" not in res["output"]
    assert "工作流执行结果" not in res["output"]


async def test_auto_mode_propagates_router_output():
    """TaskRouter 的聚合输出必须原样透传给用户 (输出管道不被改写)。"""
    handler = UnifiedChatHandler()

    async def _fake_route_task(**kwargs):
        return {
            "task_id": "task_fake_1",
            "status": "completed",
            "total_duration_ms": 5,
            "subtasks": [],
            "output": {
                "subtasks_completed": 1,
                "outputs": [{"output": {"answer": 42}}],
            },
        }

    handler.router.route_task = _fake_route_task  # type: ignore[method-assign]

    res = await handler.submit_task(user_input="1+1=?", tenant_id=TENANT)
    assert res["success"] is True
    assert res["metadata"]["task_id"] == "task_fake_1"
    assert "42" in res["output"]


async def test_executor_error_status_must_not_become_success():
    """执行器返回 status=error 时,submit_task 必须报失败。

    意图: 错误状态不得被包装成 success=True 的响应。
    """
    handler = UnifiedChatHandler()

    async def _failing_route_task(**kwargs):
        return {
            "task_id": "task_err",
            "status": "error",
            "output": {"error": "boom from router"},
        }

    handler.router.route_task = _failing_route_task  # type: ignore[method-assign]

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT)
    assert res["success"] is False
    assert "boom from router" in res["error"]


async def test_all_subtasks_failed_must_not_become_success():
    """子任务全部失败时,聚合层的 status=completed 不得被当成成功。

    现场: 首页指挥台的 quick-execute 拿到 success=true + subtasks_failed 的 JSON,
    界面把它当正常回答展示 —— 用户看到"点了执行只跳过去一坨 JSON"。
    """
    handler = UnifiedChatHandler()

    async def _all_failed(**kwargs):
        return {
            "task_id": "task_failed",
            "status": "completed",
            "total_duration_ms": 3,
            "subtasks": [
                {
                    "subtask_id": "sub_0_default",
                    "capability_id": "agent:general_chat",
                    "status": "failed",
                    "duration_ms": 3,
                }
            ],
            "output": {
                "subtasks_completed": 0,
                "subtasks_failed": 1,
                "outputs": [
                    {
                        "subtask_id": "sub_0_default",
                        "error": "LLM capability returned empty response (model=deepseek-chat)",
                    }
                ],
            },
            "usage": {
                "model": "deepseek-chat",
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
            },
        }

    handler.router.route_task = _all_failed  # type: ignore[method-assign]

    res = await handler.submit_task(user_input="只回复两个字：正常", tenant_id=TENANT)

    assert res["success"] is False
    assert "empty response" in res["error"]
    # 失败前真实花掉的用量仍要带回，不能因为失败就整笔丢掉
    assert res["usage"]["model"] == "deepseek-chat"
    last = handler.sessions[_cache_key(TENANT, TENANT, res["session_id"])].messages[-1]
    assert last["role"] == "assistant"
    assert "执行失败" in last["content"]


async def test_partial_subtask_success_keeps_result():
    """有成功子任务时保留部分结果，不能把整轮判成失败。"""
    handler = UnifiedChatHandler()

    async def _partial(**kwargs):
        return {
            "task_id": "task_partial",
            "status": "completed",
            "total_duration_ms": 4,
            "subtasks": [
                {"subtask_id": "a", "capability_id": "agent:general_chat", "status": "completed"},
                {"subtask_id": "b", "capability_id": "agent:general_chat", "status": "failed"},
            ],
            "output": {
                "subtasks_completed": 1,
                "subtasks_failed": 1,
                "outputs": [{"subtask_id": "a", "output": {"result": "ok"}}],
            },
        }

    handler.router.route_task = _partial  # type: ignore[method-assign]

    res = await handler.submit_task(user_input="并行跑两个任务", tenant_id=TENANT)
    assert res["success"] is True


# ── agent 模式 (SubAgent.run,已实现) ────────────────────────────────


async def test_agent_mode_returns_real_agent_output(monkeypatch):
    """agent 模式必须真实调用 SubAgent.run 并透传 LLM 输出。

    意图: 返回内容来自真实执行,而非硬编码占位文案。
    """
    _patch_gateway(monkeypatch, _FakeGateway(content="unique-agent-answer-001"))
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="帮我写首诗", tenant_id=TENANT, mode="agent")

    assert res["success"] is True
    assert "unique-agent-answer-001" in res["output"]


async def test_agent_mode_without_gateway_fails_loud(monkeypatch):
    """gateway 未初始化时 agent 模式必须返回明确失败。"""
    _patch_gateway_unavailable(monkeypatch)
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT, mode="agent")

    assert res["success"] is False
    assert "gateway" in res["error"].lower()


async def test_agent_mode_llm_error_fails_loud(monkeypatch):
    """LLM 返回 error 时必须传播为 success=False,不得静默成功。"""
    _patch_gateway(monkeypatch, _ErrorGateway())
    handler = UnifiedChatHandler()

    res = await handler.submit_task(user_input="任意任务", tenant_id=TENANT, mode="agent")

    assert res["success"] is False
    assert "Agent execution failed" in res["error"]
    # 失败必须进入会话历史,便于用户看到原因
    session = handler.sessions[_cache_key(TENANT, TENANT, res["session_id"])]
    assert session.messages[-1].get("error")


# ── 输出提取 ────────────────────────────────────────────────────────


def test_extract_output_prefers_last_subtask_output():
    handler = UnifiedChatHandler()
    result = {
        "output": {
            "outputs": [
                {"output": {"step": 1}},
                {"output": {"step": 2}},
            ]
        }
    }
    out = handler._extract_output(result)
    assert "2" in out and "1" not in out


def test_extract_output_handles_plain_string():
    handler = UnifiedChatHandler()
    assert handler._extract_output({"output": "plain-text"}) == "plain-text"


# ── 单例 ────────────────────────────────────────────────────────────


def test_get_chat_handler_is_singleton():
    assert get_chat_handler() is get_chat_handler()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
