"""``GET /internal/diagnostics/tables`` 契约测试。

这个端点存在的唯一理由是**清单只能有一份**：``app/db.py::REQUIRED_TABLES`` 是
引擎启动自检的权威源，Go 网关不复制它，而是来这里取结论。因此这里钉住：

1. 缺失表**逐名**返回（页面要能直接说"缺哪几张"，只说"缺表"等于没说）；
2. 只读 —— 不建表、不迁移（``information_schema`` 查询语句本身也断言一次）；
3. 数据库读不到时 ``checked=false`` + ``detail``，**不是 500**：
   "表都不在"与"看不见"在排障时结论相反，用 5xx 会让页面把后者读成前者。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.api import internal_diagnostics
from app.config import settings
from app.db import REQUIRED_TABLES
from app.main import create_app

PATH = "/internal/diagnostics/tables"


class _FakePool:
    """只实现 ``fetch``：本端点不写库，多余的接口不该被伪装出来。"""

    def __init__(self, names: list[str] | None = None, raises: Exception | None = None) -> None:
        self._names = names or []
        self._raises = raises
        self.queries: list[str] = []

    async def fetch(self, query: str) -> list[dict[str, Any]]:
        self.queries.append(query)
        if self._raises is not None:
            raise self._raises
        return [{"table_name": n} for n in self._names]


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


async def test_all_required_tables_present(monkeypatch) -> None:
    pool = _FakePool(list(REQUIRED_TABLES))
    monkeypatch.setattr(internal_diagnostics, "get_pool", lambda: pool)

    body = await internal_diagnostics.diagnostics_tables()

    assert body["checked"] is True
    assert body["missing"] == []
    assert body["missing_count"] == 0
    assert body["required_count"] == len(REQUIRED_TABLES)
    assert body["present_count"] == len(REQUIRED_TABLES)
    # 只读：语句必须是 information_schema 的 SELECT，不能是 DDL。
    assert pool.queries and "information_schema.tables" in pool.queries[0]
    assert "CREATE" not in pool.queries[0].upper()


async def test_missing_tables_are_listed_by_name(monkeypatch) -> None:
    present = [t for t in REQUIRED_TABLES if t not in ("project_checkpoints", "checkpoint_untracked")]
    monkeypatch.setattr(internal_diagnostics, "get_pool", lambda: _FakePool(present))

    body = await internal_diagnostics.diagnostics_tables()

    assert body["checked"] is True
    assert body["missing"] == ["project_checkpoints", "checkpoint_untracked"]
    assert body["missing_count"] == 2
    assert "alembic" in body["detail"]


async def test_db_unreachable_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setattr(
        internal_diagnostics, "get_pool", lambda: _FakePool(raises=RuntimeError("pool not initialized"))
    )

    body = await internal_diagnostics.diagnostics_tables()

    # 关键：不抛（否则 FastAPI 会回 500，页面只能显示"诊断坏了"）。
    assert body["checked"] is False
    assert body["missing"] == []
    assert "pool not initialized" in body["detail"]


async def test_http_requires_internal_token() -> None:
    async with _client() as client:
        resp = await client.get(PATH, headers={"X-Internal-Token": "definitely-wrong"})
    assert resp.status_code in (401, 403), (resp.status_code, resp.text)


async def test_http_returns_payload_with_valid_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "correct-token", raising=False)
    monkeypatch.setattr(internal_diagnostics, "get_pool", lambda: _FakePool(list(REQUIRED_TABLES)))

    async with _client() as client:
        resp = await client.get(PATH, headers={"X-Internal-Token": "correct-token"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["checked"] is True


@pytest.mark.parametrize("name", ["project_checkpoints", "checkpoint_untracked", "agent_runs", "subagent_runs"])
def test_core_checkpoint_tables_are_in_required_list(name: str) -> None:
    """核心表必须在权威清单里 —— 否则"表齐了"的结论会漏掉检查点链路。"""
    assert name in REQUIRED_TABLES
