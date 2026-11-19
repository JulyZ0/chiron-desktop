"""C3 第二段：项目级记忆的**端到端隔离**（service → 工具 → HTTP）。

验收口径（方案 02 §4 · C3）：**同一用户的两个项目互不串记忆**。

隔离有四条独立通道，任何一条漏掉，都会让"隔离"看起来生效而实际串味：

1. **写入**：同名 key 在两个项目里是两条独立记忆（去重键含 project）；
2. **读取**：list / search / recall 只返回本项目条目；
3. **删除**：按 id / 按 key / 清空都限定在项目内；
4. **整理**：近重复检测与超限淘汰只在项目内进行（否则 A 的条目会被当成 B 的重复项删掉）。

另有一条**向后兼容**口径：不传 project = 未分组（空串），既有调用点行为不变。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.memory.service import MemoryService
from app.tools import context as tool_context
from tests.fakes import InMemoryProfileStore


def _svc() -> MemoryService:
    return MemoryService(store=InMemoryProfileStore())


def _values(data: dict[str, Any]) -> list[Any]:
    """条目值（`list_entries` 用 `entries`、`search` 用 `results`）。"""
    items = data.get("entries") or data.get("results") or []
    return sorted(str(e.get("value")) for e in items)


# ── 写入 / 读取 ─────────────────────────────────────────────────────────


async def test_same_key_in_two_projects_are_independent():
    svc = _svc()

    await svc.upsert(1, "u", slot="fact", key="lang", value="中文", project="proj-a")
    await svc.upsert(1, "u", slot="fact", key="lang", value="English", project="proj-b")

    assert _values(await svc.list_entries(1, "u", project="proj-a")) == ["中文"]
    assert _values(await svc.list_entries(1, "u", project="proj-b")) == ["English"]


async def test_ungrouped_is_global_and_visible_in_every_project():
    """不传 project = **全局**：所有具名项目都能看到它，但它不属于任何一个项目。

    C3 修订（按产品决定）：早先 `project=''` 是独立的一档「未分组」，项目里看不见；
    现在它是**全局**范围 —— "用户的通用偏好"本就该在每个项目里生效，否则老用户升级后
    会以为记忆丢了。具名项目之间仍然互不可见（见上一条用例）。
    """
    svc = _svc()

    await svc.upsert(1, "u", slot="fact", key="k", value="全局")
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")

    # 全局视图只看得到全局条目
    assert _values(await svc.list_entries(1, "u")) == ["全局"]
    # 项目视图 = 本项目专属 + 全局（同名 key 各自独立，是两条记忆）
    assert _values(await svc.list_entries(1, "u", project="proj-a")) == ["A", "全局"]
    # 另一个项目看不到 proj-a 的，但看得到全局的
    assert _values(await svc.list_entries(1, "u", project="proj-b")) == ["全局"]


async def test_entries_carry_scope_for_the_ui():
    """条目带 `scope`：前端据此把项目视图里的全局条目渲染成只读。"""
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="g", value="全局")
    await svc.upsert(1, "u", slot="fact", key="p", value="项目", project="proj-a")

    types = {
        e["key"]: (e["scope"], e["project"])
        for e in (await svc.list_entries(1, "u", project="proj-a"))["entries"]
    }

    assert types["g"] == ("global", "")
    assert types["p"] == ("project", "proj-a")


async def test_search_does_not_leak_across_projects():
    svc = _svc()

    await svc.upsert(1, "u", slot="fact", key="lang", value="中文", project="proj-a")
    await svc.upsert(1, "u", slot="fact", key="lang", value="English", project="proj-b")

    hit_a = await svc.search(1, "u", "lang", project="proj-a")

    assert _values(hit_a) == ["中文"]


async def test_recall_only_includes_current_project():
    svc = _svc()

    await svc.upsert(1, "u", slot="preference", key="lang", value="中文", project="proj-a")
    await svc.upsert(1, "u", slot="preference", key="lang", value="English", project="proj-b")

    recalled_a = await svc.recall(1, "u", "lang", project="proj-a")
    recalled_b = await svc.recall(1, "u", "lang", project="proj-b")

    assert "中文" in recalled_a.profile_block
    assert "English" not in recalled_a.profile_block
    assert "English" in recalled_b.profile_block


# ── 冲突：跨项目同名**不是**冲突 ────────────────────────────────────────


async def test_cross_project_same_key_is_not_a_conflict():
    """两个项目的同名 key 是两条独立记忆 —— 不该触发"要覆盖你确认过的值"的登记。"""
    svc = _svc()

    await svc.upsert(
        1, "u", slot="preference", key="theme", value="dark", project="proj-a"
    )
    result = await svc.upsert(
        1, "u", slot="preference", key="theme", value="light", project="proj-b"
    )

    assert "conflict" not in result


async def test_same_project_same_key_still_conflicts():
    """反向保护：**同项目内**的覆盖仍然要登记冲突（别把隔离做成"啥都不冲突"）。"""
    svc = _svc()

    await svc.upsert(1, "u", slot="preference", key="theme", value="dark", project="proj-a")
    result = await svc.upsert(
        1, "u", slot="preference", key="theme", value="light", source="derived",
        project="proj-a",
    )

    assert result.get("conflict"), "同项目内的覆盖仍须登记待裁决"


# ── 删除 ────────────────────────────────────────────────────────────────


async def test_forget_by_key_is_scoped():
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert(1, "u", slot="fact", key="k", value="B", project="proj-b")

    assert await svc.forget_by_key(1, "u", "k", project="proj-a") == 1
    assert _values(await svc.list_entries(1, "u", project="proj-b")) == ["B"]
    assert await svc.forget_by_key(1, "u", "k", project="proj-b") == 1


async def test_clear_all_is_scoped():
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert(1, "u", slot="fact", key="k", value="B", project="proj-b")

    assert await svc.clear_all(1, "u", project="proj-a") == 1
    assert _values(await svc.list_entries(1, "u", project="proj-b")) == ["B"]


async def test_delete_entry_is_scoped_by_id():
    """拿 A 项目的 id、却声明 B 项目 —— 不该删得动（按 id 也要过 project 这道关）。"""
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    entry = (await svc.list_entries(1, "u", project="proj-a"))["entries"][0]

    assert await svc.delete_entry(1, "u", entry["id"], project="proj-b") is False
    assert await svc.delete_entry(1, "u", entry["id"], project="proj-a") is True


# ── 整理 ────────────────────────────────────────────────────────────────


async def test_organize_is_scoped_to_one_project():
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert(1, "u", slot="fact", key="k", value="B", project="proj-b")

    await svc.organize_now(1, "u", project="proj-a")

    assert _values(await svc.list_entries(1, "u", project="proj-b")) == ["B"], (
        "整理另一个项目不该动到这里"
    )


def test_organize_single_flight_is_per_project():
    """单飞行按项目记账：A 在整理时，B 不该被判定成"已在运行"而永远排不上队。"""
    svc = _svc()
    svc._organize_state[(1, "u", "proj-a")] = {"running": True}

    assert svc.organize_status(1, "u", "proj-a")["running"] is True
    assert svc.organize_status(1, "u", "proj-b")["running"] is False


# ── 工具层：project 来自 tool context ───────────────────────────────────


async def test_remember_uses_context_project(monkeypatch):
    from app.tools import memory as memory_tools

    svc = _svc()
    monkeypatch.setattr(memory_tools, "_get_memory_service", lambda: svc)

    tool_context.set_tool_context(user_id="u", tenant_id=1, project="proj-a")
    out = await memory_tools.remember("lang", "中文")
    assert out.get("success") is True

    tool_context.set_tool_context(user_id="u", tenant_id=1, project="proj-b")
    assert (await svc.list_entries(1, "u", project="proj-b"))["total"] == 0
    assert (await svc.list_entries(1, "u", project="proj-a"))["total"] == 1


async def test_forget_uses_context_project(monkeypatch):
    from app.tools import memory as memory_tools

    svc = _svc()
    monkeypatch.setattr(memory_tools, "_get_memory_service", lambda: svc)

    tool_context.set_tool_context(user_id="u", tenant_id=1, project="proj-a")
    await memory_tools.remember("lang", "中文")

    # 换到另一个项目再遗忘 —— 不该动到 proj-a 的那条
    tool_context.set_tool_context(user_id="u", tenant_id=1, project="proj-b")
    out = await memory_tools.forget("lang")

    assert out.get("deleted") == 0
    assert (await svc.list_entries(1, "u", project="proj-a"))["total"] == 1


# ── HTTP 层：project 从 query 读 ────────────────────────────────────────


def _patch_api_service(monkeypatch: pytest.MonkeyPatch, svc: MemoryService) -> None:
    monkeypatch.setattr("app.api.memory.get_service", lambda: svc)


async def test_http_profile_is_project_scoped(monkeypatch):
    svc = _svc()
    _patch_api_service(monkeypatch, svc)
    params = {"user_id": "u", "tenant_id": 1}

    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/memory/profile",
            json={"slot": "fact", "key": "k", "value": "A"},
            params={**params, "project": "proj-a"},
        )
        assert resp.status_code == 200

        in_a = await client.get("/v1/memory/profile", params={**params, "project": "proj-a"})
        in_b = await client.get("/v1/memory/profile", params={**params, "project": "proj-b"})

    assert in_a.json()["total"] == 1
    assert in_b.json()["total"] == 0, "HTTP 层绝不能把 A 项目的记忆返回给 B 项目"


async def test_http_clear_only_clears_that_project(monkeypatch):
    svc = _svc()
    _patch_api_service(monkeypatch, svc)
    params = {"user_id": "u", "tenant_id": 1}
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    await svc.upsert(1, "u", slot="fact", key="k", value="B", project="proj-b")

    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        resp = await client.post(
            "/v1/memory/profile/clear",
            json={"confirm": True},
            params={**params, "project": "proj-a"},
        )

    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    assert (await svc.list_entries(1, "u", project="proj-b"))["total"] == 1


# ── 范围迁移（全局 ↔ 项目） ─────────────────────────────────────────────


async def test_move_project_entry_to_global():
    """项目里的条目改成全局后：全局视图能看见，原项目视图仍能看见（并集）。"""
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    entry = (await svc.list_entries(1, "u", project="proj-a"))["entries"][0]

    moved = await svc.move_entry_scope(
        1, "u", entry["id"], from_project="proj-a", to_project=""
    )

    assert moved is not None, "同范围内的条目应当迁移成功"
    assert _values(await svc.list_entries(1, "u")) == ["A"]
    # 项目视图下仍然看得到（项目专属 + 全局的并集），只是变成了只读的全局条目
    data = await svc.list_entries(1, "u", project="proj-a")
    assert _values(data) == ["A"]
    assert data["entries"][0]["scope"] == "global"


async def test_move_global_entry_to_project():
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="G")
    entry = (await svc.list_entries(1, "u"))["entries"][0]

    await svc.move_entry_scope(1, "u", entry["id"], from_project="", to_project="proj-a")

    assert _values(await svc.list_entries(1, "u")) == [], "全局视图里不该再看到它"
    assert _values(await svc.list_entries(1, "u", project="proj-a")) == ["G"]
    assert _values(await svc.list_entries(1, "u", project="proj-b")) == []


async def test_move_refuses_when_not_in_current_scope():
    """项目视图里动不了全局条目 —— `from_project` 就是权限边界。

    这条与"项目视图里全局条目只读"是同一套口径：前端只是不渲染入口，
    真正的拦截必须在服务端，否则绕过界面直接发请求就能改到所有项目共用的记忆。
    """
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="G")
    entry = (await svc.list_entries(1, "u"))["entries"][0]

    out = await svc.move_entry_scope(
        1, "u", entry["id"], from_project="proj-a", to_project="proj-a"
    )

    assert out is None
    assert _values(await svc.list_entries(1, "u")) == ["G"]


async def test_move_rejects_when_target_has_same_key():
    """目标范围已有同 (slot, key)：抛 ValueError（HTTP 层转 409），绝不静默覆盖。

    覆盖会凭空丢掉一条用户看得见的记忆，而"该留哪条"属于智能整理的职责。
    """
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="G")
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    global_entry = (await svc.list_entries(1, "u"))["entries"][0]

    with pytest.raises(ValueError):
        await svc.move_entry_scope(
            1, "u", global_entry["id"], from_project="", to_project="proj-a"
        )

    assert _values(await svc.list_entries(1, "u", project="proj-a")) == ["A", "G"]


async def test_move_to_same_scope_is_noop():
    svc = _svc()
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    entry = (await svc.list_entries(1, "u", project="proj-a"))["entries"][0]

    out = await svc.move_entry_scope(
        1, "u", entry["id"], from_project="proj-a", to_project="proj-a"
    )

    assert out is not None and out.item_key == "k"


async def test_http_move_scope_reports_409_on_key_clash(monkeypatch):
    svc = _svc()
    _patch_api_service(monkeypatch, svc)
    params = {"user_id": "u", "tenant_id": 1}
    await svc.upsert(1, "u", slot="fact", key="k", value="G")
    await svc.upsert(1, "u", slot="fact", key="k", value="A", project="proj-a")
    global_entry = (await svc.list_entries(1, "u"))["entries"][0]

    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        clash = await client.post(
            f"/v1/memory/profile/{global_entry['id']}/scope",
            json={"to_project": "proj-a"},
            params=params,  # from_project 空 = 全局视图
        )
        gone = await client.post(
            "/v1/memory/profile/not-a-real-id/scope",
            json={"to_project": ""},
            params={**params, "project": "proj-a"},
        )

    assert clash.status_code == 409
    assert gone.status_code == 404
