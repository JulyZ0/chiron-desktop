"""H14：taskboard 的 revision CAS 与稳定 todo ID（PRD 9.2）。

两个缺陷：
  1. **写覆盖**：`write()` 是"读到什么就基于什么写"。主 agent 与子 agent（或同一 agent
     的两次并发调用）都从 revision=n 读到同一份、各自 +1 写回 —— 后写的静默覆盖先写的，
     表现为"某条待办凭空消失"。
  2. **ID 漂移**：合并时按**位置**重编号（`t{len(result)+1}`），子 agent 一合并，
     已有条目的 id 全变；而 `depends_on` 存的是 id 字符串 ⇒ 依赖整片失效或指错。
"""

from __future__ import annotations

import pytest

from app.agent import taskboard


def _todo(tid: str, content: str, status: str = "pending", deps: list[str] | None = None):
    item = {"id": tid, "content": content, "status": status}
    if deps is not None:
        item["depends_on"] = deps
    return item


# ── 1. ID 稳定性 ───────────────────────────────────────────────────────────
def test_merge_keeps_existing_ids() -> None:
    existing = [_todo("t1", "a"), _todo("t2", "b")]
    incoming = [_todo("t2", "b", "completed")]
    merged = taskboard._merge([dict(i) for i in existing], [dict(i) for i in incoming])
    assert [i["id"] for i in merged] == ["t1", "t2"]
    assert merged[1]["status"] == "completed"


def test_merge_appends_new_items_without_reusing_ids() -> None:
    existing = [_todo("t1", "a"), _todo("t2", "b")]
    incoming = [_todo("t7", "c"), _todo("t8", "d")]
    merged = taskboard._merge([dict(i) for i in existing], [dict(i) for i in incoming])
    ids = [i["id"] for i in merged]
    assert ids[:2] == ["t1", "t2"], "既有条目不得被重编号"
    assert len(ids) == len(set(ids)), "不得出现重复 id"
    # 新条目自带的、**未撞号**的 id 原样保留：模型可能在 depends_on 里已经引用了它，
    # 重编号就得跟着改写（能不改就不改）。
    assert ids[2:] == ["t7", "t8"]


def test_merge_assigns_id_when_incoming_has_none_or_collides() -> None:
    existing = [_todo("t1", "a")]
    incoming = [{"content": "no id", "status": "pending"}, _todo("t1", "dup", "pending")]
    merged = taskboard._merge([dict(i) for i in existing], [dict(i) for i in incoming])
    ids = [i["id"] for i in merged]
    assert len(ids) == len(set(ids)), "撞号或缺失时由 host 分配，绝不产生重复 id"


def test_merge_preserves_depends_on() -> None:
    existing = [_todo("t1", "a"), _todo("t2", "b", deps=["t1"])]
    incoming = [_todo("t2", "b", "in_progress", deps=["t1"])]
    merged = taskboard._merge([dict(i) for i in existing], [dict(i) for i in incoming])
    dep = next(i for i in merged if i["id"] == "t2")
    assert dep["depends_on"] == ["t1"], "合并后依赖必须仍指向同一个 id"


def test_merge_does_not_delete_untouched_items() -> None:
    existing = [_todo("t1", "a"), _todo("t2", "b")]
    incoming = [_todo("t1", "a", "completed")]
    merged = taskboard._merge([dict(i) for i in existing], [dict(i) for i in incoming])
    assert {i["id"] for i in merged} == {"t1", "t2"}, "子 agent 无权删掉别人的活"


def test_renumber_remaps_depends_on() -> None:
    todos = [_todo("x9", "a"), _todo("y8", "b", deps=["x9"])]
    out = taskboard._renumber([dict(i) for i in todos])
    assert [i["id"] for i in out] == ["t1", "t2"]
    assert out[1]["depends_on"] == ["t1"], "整份替换后依赖要跟着映射走"


def test_renumber_drops_dangling_dependencies() -> None:
    todos = [_todo("t1", "a", deps=["t404"])]
    out = taskboard._renumber([dict(i) for i in todos])
    assert out[0]["depends_on"] == [], "指向不存在条目的依赖要丢掉，不留悬空引用"


def test_next_free_id_never_reuses_a_retired_number() -> None:
    """分配用 **max+1**，不去填中间的空号。

    填空号看着更"整齐"，但会把一个**曾经存在过**的编号发给新条目 —— 而旧条目的
    `depends_on` 里可能还记着那个号（比如 t2 被删了，依赖没清）。复用会让它突然
    指向一个完全不相干的新任务，比留空号糟得多。
    """
    assert taskboard._next_free_id({"t1", "t3"}) == "t4"
    assert taskboard._next_free_id({"t1", "t2"}) == "t3"
    assert taskboard._next_free_id(set()) == "t1"


# ── 2. revision CAS ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_write_rejects_stale_expected_revision() -> None:
    """基于旧版本改的写必须被拒，且**不落盘**，并把当前版本原样回报。"""
    first = await taskboard.write([_todo("t1", "a")])
    assert first["revision"] == 1 and first["conflict"] is False

    stale = await taskboard.write([_todo("t1", "a", "completed")], expected_revision=0)
    assert stale["conflict"] is True
    assert stale["revision"] == 1, "冲突时回报**当前**版本，调用方据此重试"
    current = await taskboard.load()
    assert current["todos"][0]["status"] == "pending", "冲突的写不得改变板上内容"


@pytest.mark.asyncio
async def test_write_accepts_matching_expected_revision() -> None:
    await taskboard.write([_todo("t1", "a")])
    ok = await taskboard.write([_todo("t1", "a", "completed")], expected_revision=1)
    assert ok["conflict"] is False
    assert ok["revision"] == 2
    current = await taskboard.load()
    assert current["todos"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_write_without_expected_revision_keeps_legacy_behavior() -> None:
    """不带 expected_revision 的调用保持旧语义（内部单写者路径不强制 CAS）。"""
    await taskboard.write([_todo("t1", "a")])
    second = await taskboard.write([_todo("t1", "a", "completed")])
    assert second["conflict"] is False
    assert second["revision"] == 2


@pytest.mark.asyncio
async def test_conflict_reports_current_owner() -> None:
    await taskboard.write([_todo("t1", "a")])
    stale = await taskboard.write([_todo("t1", "b")], expected_revision=99)
    assert stale["conflict"] is True
    assert "owner" in stale


# ── 3. 源码守卫：Redis 路径必须是原子 CAS ─────────────────────────────────
def test_redis_write_uses_atomic_cas_script() -> None:
    import inspect

    src = inspect.getsource(taskboard)
    assert "_CAS_WRITE_LUA" in src, "Redis 写入必须是原子 CAS（不能只靠先读后写）"
    assert "redis.eval(" in src
    assert "tonumber(ARGV[1]) ~= current_revision" in src, "脚本里必须比较版本"
