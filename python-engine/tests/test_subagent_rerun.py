"""`rerun_subagent` 工具 + 共用重跑实现的判定（P0-2）。

回归背景：后台子 Agent 与父回合解耦后，"实例重启判 lost / 用户停止判 cancelled /
provider 报错判 failed / 撞预算墙判 partial"都是常态。这些 run 的任务与 Profile 都还在
DB 里，重跑必须**按原样**再派一次 —— 让模型重新拼任务文本几乎必然与原来不一致，
那就不叫重跑了。同时：

* 重跑**不能**变成提权通道（原 run 只读 ⇒ 重跑也必须只读）；
* 只认**终态**（running 的已有 owner，重跑就是两份并发作业）；
* 取不到原任务就**拒绝**（409），由前端把任务放回输入框 —— 绝不猜一个任务。

判定与派发的实现在 ``app.subagent.rerun``（工具与网关内网端点共用一份），
因此断言打在那一层；工具层只验证"错误被转成结构化 error 而不是假 run_id"。
"""
from __future__ import annotations

import pytest

from app.subagent import rerun as rerun_impl
from app.tools import subagent_rerun


class _FakePool:
    def __init__(self, row=None, error: Exception | None = None):
        self._row = row
        self._error = error
        self.calls: list[tuple] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if self._error:
            raise self._error
        return self._row


def _row(**overrides):
    row = {
        "id": "rs_old",
        "task": "把 utils.py 里的日期解析改成时区安全",
        "profile_name": "reviewer",
        "read_only": True,
        "status": "lost",
        "depth": 1,
    }
    row.update(overrides)
    return row


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(rerun_impl, "_tenant_id", lambda: 1)
    monkeypatch.setattr(rerun_impl, "_user_id", lambda: "u1")
    monkeypatch.setattr(rerun_impl, "_session_id", lambda: "s1")


def _patch_pool(monkeypatch, pool):
    monkeypatch.setattr("app.db.get_pool", lambda: pool)


def _patch_delegate(monkeypatch, captured: list[dict]):
    async def _fake(**kwargs):
        captured.append(kwargs)
        return {"status": "async_launched", "isAsync": True, "run_id": "rs_new",
                "result_ref": "rs_new", "note": "detail"}

    monkeypatch.setattr("app.tools.subagent.subagent", _fake)


@pytest.mark.asyncio
async def test_rerun_reuses_task_profile_and_keeps_readonly(monkeypatch, ctx):
    pool = _FakePool(_row())
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")

    assert result["run_id"] == "rs_new" and result["rerun_of"] == "rs_old"
    assert "note" not in result, "重跑不该把'请继续做别的事'那类提示原样带给模型"
    call = captured[0]
    assert call["task"] == "把 utils.py 里的日期解析改成时区安全"
    assert call["profile"] == "reviewer"
    assert call["run_in_background"] is True
    assert call["rerun_of"] == "rs_old"
    # 关键：原 run 只读 ⇒ 重跑仍只读（重跑不是提权通道）
    assert call["allow_write"] is False


@pytest.mark.asyncio
async def test_rerun_preserves_write_permission(monkeypatch, ctx):
    pool = _FakePool(_row(read_only=False))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    await subagent_rerun.rerun_subagent("rs_old")
    assert captured[0]["allow_write"] is True


@pytest.mark.asyncio
async def test_rerun_links_new_run_to_original(monkeypatch, ctx):
    """新 run 在任务树里挂在原 run 下面（parent_run_id），深度 = 原深度 + 1。"""
    pool = _FakePool(_row(depth=2))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")

    assert captured[0]["parent_run_id"] == "rs_old"
    assert result["parent_run_id"] == "rs_old"
    assert result["depth"] == 3


@pytest.mark.asyncio
async def test_rerun_clamps_depth_to_delegation_limit(monkeypatch, ctx):
    """最深那一层的 run 也能重跑：深度夹到 MAX_DEPTH-1，不会被递归闸门拒掉。"""
    from app.tools.subagent import MAX_DEPTH

    pool = _FakePool(_row(depth=MAX_DEPTH))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert result["depth"] == MAX_DEPTH
    assert captured, "夹住深度后必须真的派出去了"


@pytest.mark.asyncio
async def test_rerun_rejects_running_run(monkeypatch, ctx):
    pool = _FakePool(_row(status="running"))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert "error" in result and "only finished runs" in result["error"]
    assert result["code"] == "run_not_terminal"
    assert not captured, "running 的 run 已经有 owner，不能再派一份并发作业"


@pytest.mark.asyncio
async def test_rerun_rejects_missing_task(monkeypatch, ctx):
    """库里没有任务描述 ⇒ 拒绝（409 口径），绝不猜一个任务去派发。"""
    pool = _FakePool(_row(task="   "))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert "no stored task" in result["error"]
    assert result["code"] == "task_missing"
    assert not captured


@pytest.mark.asyncio
@pytest.mark.parametrize("status", rerun_impl.RERUNNABLE)
async def test_every_terminal_status_is_rerunnable(monkeypatch, ctx, status):
    """终态白名单里的每一个都能重跑 —— partial 尤其重要（撞预算墙的典型终态）。"""
    pool = _FakePool(_row(status=status))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert result["run_id"] == "rs_new", f"{status} 应当可重跑：{result}"
    assert captured


@pytest.mark.asyncio
async def test_rerun_is_scoped_to_session_tenant_and_user(monkeypatch, ctx):
    pool = _FakePool(None)
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    result = await subagent_rerun.rerun_subagent("rs_other")
    assert "not found" in result["error"]
    assert result["code"] == "not_found"
    sql, args = pool.calls[0]
    assert "root_session_id = $4" in sql and "tenant_id = $2" in sql
    assert args == ("rs_other", 1, "u1", "s1")
    # 租户必须是**整数**：`tenant_id` 列是 smallint，传字符串会让 asyncpg 直接报错，
    # 那时重跑会以"store unavailable"失败 —— 看起来像迁移问题，实际是参数类型。
    assert isinstance(args[1], int)


@pytest.mark.asyncio
async def test_task_override_replaces_stored_task(monkeypatch, ctx):
    pool = _FakePool(_row())
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    await subagent_rerun.rerun_subagent("rs_old", task_override="只跑测试，别改代码")
    assert captured[0]["task"] == "只跑测试，别改代码"


@pytest.mark.asyncio
async def test_missing_column_degrades(monkeypatch, ctx):
    """老库还没有 rerun_of 列（迁移没执行）：必须报错而不是假成功。"""
    pool = _FakePool(error=RuntimeError('column "rerun_of" does not exist'))
    _patch_pool(monkeypatch, pool)

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert "error" in result and "migrations" in result["error"]
    assert result["code"] == "store_unavailable"


@pytest.mark.asyncio
async def test_without_session_context(monkeypatch):
    monkeypatch.setattr(rerun_impl, "_tenant_id", lambda: 1)
    monkeypatch.setattr(rerun_impl, "_user_id", lambda: "u1")
    monkeypatch.setattr(rerun_impl, "_session_id", lambda: "")
    _patch_pool(monkeypatch, _FakePool(_row()))

    result = await subagent_rerun.rerun_subagent("rs_old")
    assert "error" in result and result["code"] == "no_session"


@pytest.mark.asyncio
async def test_context_is_restored_after_rerun(monkeypatch, ctx):
    """重跑改过的上下文（depth）必须精确还原，否则会污染父 Agent 后续委派。"""
    from app.tools.context import get_all, restore_context, set_tool_context

    pool = _FakePool(_row(depth=2))
    _patch_pool(monkeypatch, pool)
    captured: list[dict] = []
    _patch_delegate(monkeypatch, captured)

    before = get_all()
    set_tool_context(subagent_depth=0, marker="keep-me")
    try:
        await subagent_rerun.rerun_subagent("rs_old")
        assert get_all().get("marker") == "keep-me"
        assert get_all().get("subagent_depth") == 0
    finally:
        restore_context(before)


def test_rerun_tool_registered_as_write():
    """重跑会真的再起一个作业（消耗 token、复用写权限）→ 必须按 write 级别确认。"""
    from app.agent.tool_policy import WRITE, tool_level
    from app.tools.registry import registry

    assert "rerun_subagent" in registry.list_names()
    assert tool_level("rerun_subagent") == WRITE
