"""H28: conflict identifiers and every conflict operation are owner-scoped."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.memory.conflict_audit import EVENT_DISMISSED, EVENT_RESOLVED
from app.memory.conflict_manager import ConflictManager
from app.memory.layers import MemoryConflict, ProfileItem, SlotType, SourceType
from app.memory.service import MemoryService
from tests.fakes import InMemoryProfileStore
from tests.memory.fake_redis import FakeRedis


class RecordingAuditor:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def record(self, event: str, conflict: dict[str, Any]) -> None:
        self.events.append((event, conflict))


def _conflict(conflict_id: str = "legacy") -> MemoryConflict:
    return MemoryConflict(
        conflict_id=conflict_id,
        slot=SlotType.FACT,
        item_key="city",
        old_value="上海",
        new_value="北京",
        source=SourceType.DERIVED,
        tenant_id="t1",
        user_id="u1",
        created_at=1.0,
    )


async def _seed_service_conflict(svc: MemoryService) -> str:
    await svc.upsert(
        "t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed"
    )
    result = await svc.upsert(
        "t1", "u1", "fact", "city", "北京", confidence=50, source="derived"
    )
    return result["conflict"]["conflict_id"]


async def test_manager_generates_unpredictable_unique_ids(monkeypatch):
    monkeypatch.setattr("app.memory.conflict_manager.time.time", lambda: 1700000000.0)
    manager = ConflictManager(FakeRedis())
    item = ProfileItem(
        slot=SlotType.FACT,
        item_key="city",
        item_value="上海",
        confidence=95,
        source=SourceType.USER_CONFIRMED,
        version=1,
        confirmed_at=1.0,
        last_referenced_at=None,
        created_at=1.0,
        updated_at=1.0,
    )
    ids = []
    for _ in range(20):
        blocked, conflict = await manager.detect_and_handle_conflict(
            "t1", "u1", SlotType.FACT, "city", "北京", SourceType.TOOL_WRITTEN, item
        )
        assert blocked and conflict is not None
        ids.append(conflict.conflict_id)

    assert len(set(ids)) == len(ids)
    assert all(len(cid.removeprefix("conf_")) >= 32 for cid in ids)


async def test_service_generates_unpredictable_unique_ids(monkeypatch):
    monkeypatch.setattr("app.memory.service.time.time", lambda: 1700000000.0)
    svc = MemoryService(store=InMemoryProfileStore())
    ids = []
    for _ in range(20):
        cid = await svc._register_conflict(
            "t1", "u1", "fact", "city", "上海", "北京", "derived"
        )
        ids.append(cid["conflict_id"])

    assert len(set(ids)) == len(ids)
    assert all(len(cid.removeprefix("cfl_")) >= 32 for cid in ids)


@pytest.mark.parametrize("scope", [("t2", "u1"), ("t1", "u2")])
@pytest.mark.parametrize("operation", ["get", "list", "resolve", "delete"])
async def test_manager_rejects_cross_scope_legacy_ids(scope, operation):
    redis = FakeRedis()
    auditor = RecordingAuditor()
    manager = ConflictManager(redis, auditor=auditor)
    legacy_id = "conf_1700000000_1234"
    await manager.register_conflict(_conflict(legacy_id))
    tenant, user = scope

    if operation == "get":
        assert await manager.get_conflict(legacy_id, tenant, user) is None
    elif operation == "list":
        await redis.sadd(manager._pending_list_key(tenant, user), legacy_id)
        assert await manager.get_pending_conflicts(tenant, user) == []
    elif operation == "resolve":
        assert await manager.resolve_conflict(legacy_id, tenant, user, "use_new") == (
            False,
            None,
        )
    else:
        assert await manager.delete_conflict(legacy_id, tenant, user) is False

    assert await redis.get(manager._pending_key(legacy_id)) is not None
    assert [event for event, _ in auditor.events] == ["detected"]
    assert await manager.get_conflict(legacy_id, "t1", "u1") is not None


@pytest.mark.parametrize("operation", ["get", "list", "resolve", "delete"])
async def test_manager_unknown_id_is_indistinguishable_from_cross_scope(operation):
    manager = ConflictManager(FakeRedis())
    if operation == "get":
        assert await manager.get_conflict("unknown", "t1", "u1") is None
    elif operation == "list":
        assert await manager.get_pending_conflicts("t1", "u1") == []
    elif operation == "resolve":
        assert await manager.resolve_conflict("unknown", "t1", "u1", "keep_old") == (
            False,
            None,
        )
    else:
        assert await manager.delete_conflict("unknown", "t1", "u1") is False


async def test_legacy_id_remains_usable_with_matching_scope():
    manager = ConflictManager(FakeRedis())
    legacy_id = "conf_1700000000_1234"
    await manager.register_conflict(_conflict(legacy_id))

    assert (await manager.get_conflict(legacy_id, "t1", "u1")).conflict_id == legacy_id
    assert await manager.delete_conflict(legacy_id, "t1", "u1") is True
    assert await manager.get_conflict(legacy_id, "t1", "u1") is None


async def test_resolution_and_dismissal_audit_the_operator():
    redis = FakeRedis()
    auditor = RecordingAuditor()
    manager = ConflictManager(redis, auditor=auditor)
    await manager.register_conflict(_conflict("resolved"))
    await manager.register_conflict(_conflict("dismissed"))

    assert (
        await manager.resolve_conflict("resolved", "t1", "u1", "use_new")
    )[0] is True
    assert await manager.delete_conflict("dismissed", "t1", "u1") is True

    resolved = auditor.events[-2][1]
    dismissed = auditor.events[-1][1]
    assert auditor.events[-2][0] == EVENT_RESOLVED
    assert auditor.events[-1][0] == EVENT_DISMISSED
    for event in (resolved, dismissed):
        assert event["actor_tenant_id"] == "t1"
        assert event["actor_user_id"] == "u1"
        assert event["owner_tenant_id"] == "t1"
        assert event["owner_user_id"] == "u1"
        assert event["tenant_id"] == event["actor_tenant_id"]
        assert event["user_id"] == event["actor_user_id"]


@pytest.mark.parametrize("scope", [("t2", "u1"), ("t1", "u2")])
@pytest.mark.parametrize("operation", ["resolve", "delete"])
async def test_service_rejects_cross_scope_local_conflicts(scope, operation):
    svc = MemoryService(store=InMemoryProfileStore())
    cid = await _seed_service_conflict(svc)
    tenant, user = scope

    if operation == "resolve":
        with pytest.raises(ValueError, match="conflict not found"):
            await svc.resolve_conflict(cid, tenant, user, "adopt_new")
    else:
        assert await svc.delete_conflict(cid, tenant, user) is False

    assert [c["conflict_id"] for c in svc.list_conflicts("t1", "u1")] == [cid]
    entry = await svc._store.get_by_key("t1", "u1", "fact", "city")
    assert entry.item_value == "上海"


@pytest.mark.parametrize("operation", ["resolve", "delete"])
async def test_service_unknown_id_has_no_side_effects(operation):
    svc = MemoryService(store=InMemoryProfileStore())
    if operation == "resolve":
        with pytest.raises(ValueError, match="conflict not found"):
            await svc.resolve_conflict("unknown", "t1", "u1", "keep_old")
    else:
        assert await svc.delete_conflict("unknown", "t1", "u1") is False
    assert svc.list_conflicts("t1", "u1") == []


@pytest.mark.parametrize("scope", [("t2", "u1"), ("t1", "u2")])
@pytest.mark.parametrize("operation", ["resolve", "delete"])
async def test_service_rejects_cross_scope_shared_legacy_ids(scope, operation):
    redis = FakeRedis()
    auditor = RecordingAuditor()
    manager = ConflictManager(redis, auditor=auditor)
    svc = MemoryService(store=InMemoryProfileStore(), conflict_manager=manager)
    await svc.upsert(
        "t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed"
    )
    legacy_id = "conf_1700000000_1234"
    await manager.register_conflict(_conflict(legacy_id))
    tenant, user = scope

    if operation == "resolve":
        with pytest.raises(ValueError, match="conflict not found"):
            await svc.resolve_conflict(legacy_id, tenant, user, "adopt_new")
    else:
        assert await svc.delete_conflict(legacy_id, tenant, user) is False

    assert await redis.get(manager._pending_key(legacy_id)) is not None
    assert [event for event, _ in auditor.events] == ["detected"]
    entry = await svc._store.get_by_key("t1", "u1", "fact", "city")
    assert entry.item_value == "上海"


async def test_shared_legacy_id_resolves_with_matching_scope():
    redis = FakeRedis()
    manager = ConflictManager(redis)
    svc = MemoryService(store=InMemoryProfileStore(), conflict_manager=manager)
    await svc.upsert(
        "t1", "u1", "fact", "city", "上海", confidence=95, source="user_confirmed"
    )
    legacy_id = "conf_1700000000_1234"
    await manager.register_conflict(_conflict(legacy_id))

    assert [c["conflict_id"] for c in await svc.list_conflicts_shared("t1", "u1")] == [
        legacy_id
    ]
    result = await svc.resolve_conflict(legacy_id, "t1", "u1", "adopt_new")
    assert result["resolved_value"] == "北京"
    assert json.loads(await redis.get(manager._pending_key(legacy_id)) or "null") is None
