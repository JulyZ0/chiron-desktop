"""跨副本冲突一致性：登记落共享存储，其他副本可见、可裁决。

多副本部署下（docker-compose 的 --scale python-engine=2），冲突若只存在进程内，
用户在副本 A 登记的冲突在副本 B 的记忆页上就看不到。这里用共享同一实例的
InMemoryConflictManager 模拟 Redis，验证「一个副本写的，另一个副本能读能裁决」。
"""

import pytest

from app.memory.service import MemoryService
from tests.fakes import InMemoryConflictManager, InMemoryProfileStore


def _two_replicas():
    """两个 MemoryService 共享同一份「Redis」与同一个 store（PG）。"""
    shared = InMemoryConflictManager()
    store = InMemoryProfileStore()
    return (
        MemoryService(store=store, conflict_manager=shared),
        MemoryService(store=store, conflict_manager=shared),
        shared,
    )


async def _seed_conflict(svc):
    """让 svc 产生一条冲突：user_confirmed 被 derived 覆盖。"""
    await svc.upsert("t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed")
    return await svc.upsert("t1", "u1", "fact", "city", "北京", confidence=50, source="derived")


class TestConflictSharedStorage:
    @pytest.mark.asyncio
    async def test_conflict_from_one_replica_visible_on_other(self):
        a, b, _ = _two_replicas()
        r = await _seed_conflict(a)
        assert "conflict" in r

        # 副本 B 从未见过这次 upsert，但它是共享存储意义上的「同一个系统」
        seen_by_b = await b.list_conflicts_shared("t1", "u1")
        assert len(seen_by_b) == 1
        assert seen_by_b[0]["key"] == "city"
        assert seen_by_b[0]["old_value"] == "上海"

    @pytest.mark.asyncio
    async def test_resolve_on_other_replica_clears_for_both(self):
        a, b, _ = _two_replicas()
        await _seed_conflict(a)
        conflict_id = (await b.list_conflicts_shared("t1", "u1"))[0]["conflict_id"]

        # 在 B 上裁决 —— B 本地没有这条，必须从共享存储取回
        result = await b.resolve_conflict(conflict_id, "adopt_new")
        assert result["status"] == "resolved"
        assert result["resolved_value"] == "北京"

        assert await a.list_conflicts_shared("t1", "u1") == []
        assert await b.list_conflicts_shared("t1", "u1") == []
        # 裁决结果已写回共享的 L2
        data = await a.list_entries("t1", "u1")
        assert data["entries"][0]["value"] == "北京"

    @pytest.mark.asyncio
    async def test_dismiss_on_one_replica_clears_for_both(self):
        a, b, _ = _two_replicas()
        await _seed_conflict(a)
        conflict_id = (await b.list_conflicts_shared("t1", "u1"))[0]["conflict_id"]

        assert await b.delete_conflict(conflict_id) is True
        assert await a.list_conflicts_shared("t1", "u1") == []

    @pytest.mark.asyncio
    async def test_tenant_isolation_across_replicas(self):
        a, b, _ = _two_replicas()
        await _seed_conflict(a)
        assert await b.list_conflicts_shared("t2", "u1") == []

    @pytest.mark.asyncio
    async def test_without_shared_store_falls_back_to_process_local(self):
        """未配置共享存储时（单机/测试），冲突仍在本实例可见可裁决。

        这是**降级路径**而非缓存：整块能力不该因为 Redis 缺席而消失。
        """
        svc = MemoryService(store=InMemoryProfileStore())
        await _seed_conflict(svc)

        assert len(svc.list_conflicts("t1", "u1")) == 1
        assert len(await svc.list_conflicts_shared("t1", "u1")) == 1

        conflict_id = svc.list_conflicts("t1", "u1")[0]["conflict_id"]
        result = await svc.resolve_conflict(conflict_id, "keep_old")
        assert result["resolved_value"] == "上海"
        assert svc.list_conflicts("t1", "u1") == []
