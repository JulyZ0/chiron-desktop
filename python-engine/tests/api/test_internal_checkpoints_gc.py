"""``POST /v1/internal/checkpoints/gc`` 的安全契约。

这个端点删的是**本机共享**的未跟踪 CAS 分片（所有项目共用
``%LOCALAPPDATA%/chiron/checkpoints/untracked``），因此它的每一条"失败即中止"
都必须被钉住：

1. **引用集合读不到 ⇒ 503 且一个文件都不删**。把"查不到"当成"没人引用"等于
   清空所有项目的未跟踪缓存，而现场只会表现为"别的项目的还原点回滚时找不到 blob"。
2. **``dry_run`` 缺省为 true**：预演不落盘。
3. 真正执行时只删"未被引用且已过保留期"的分片，被引用的分片永远留。

端点函数被直接调用（与 ``tests/api/test_internal_knowledge_runtime.py`` 同款），
不经过 ASGI 中间件 —— 这里要验的是删除决策，不是鉴权链路。
"""

from __future__ import annotations

import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.agent import checkpoint_hooks
from app.api import internal_checkpoints as ic
from app.config import settings

TOKEN = "internal-token-for-tests"


@pytest.fixture(autouse=True)
def _fake_gateway(monkeypatch):
    """让 ``_verify_gateway_key`` 认这个测试令牌。"""
    monkeypatch.setattr(settings, "internal_token", TOKEN, raising=False)
    monkeypatch.setattr(settings, "llm_gateway_key", "", raising=False)


def _request() -> Request:
    return Request({"type": "http", "headers": [(b"x-internal-token", TOKEN.encode())]})


def _store(tmp_path, monkeypatch):
    """把状态目录指到临时目录，避免测试碰到真实用户数据。"""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(checkpoint_hooks, "checkpoint_state_dir", lambda: state)
    store = state / "untracked"
    store.mkdir()
    return store


def _put(store, digest: str, payload: bytes, *, age_days: float) -> None:
    path = store / digest[:2] / digest[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    stamp = time.time() - age_days * 86400
    import os

    os.utime(path, (stamp, stamp))


async def test_gc_aborts_without_reference_set(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    _put(store, "a" * 64, b"x" * 4, age_days=90)

    async def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ic, "_reference_sets", _boom)

    with pytest.raises(HTTPException) as exc:
        await ic.checkpoint_gc(ic.GcRequest(dry_run=False), _request())

    assert exc.value.status_code == 503
    # 关键：一个文件都不许删。
    assert (store / ("a" * 2) / ("a" * 62)).exists()


async def test_gc_dry_run_defaults_to_true(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    referenced = "b" * 64
    stale = "c" * 64
    _put(store, referenced, b"r" * 5, age_days=400)
    _put(store, stale, b"s" * 9, age_days=400)

    async def _refs():
        return {referenced}, {referenced, stale}

    monkeypatch.setattr(ic, "_reference_sets", _refs)

    # 请求体不传 dry_run：默认必须是预演。
    out = await ic.checkpoint_gc(ic.GcRequest(), _request())

    assert out["dry_run"] is True
    assert out["deleted"] == 1
    assert out["referenced"] == 1
    assert out["freed_bytes"] == 9
    assert out["retention_days"] == 30
    assert (store / ("c" * 2) / ("c" * 62)).exists(), "预演不得删文件"


async def test_gc_execute_keeps_referenced(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path, monkeypatch)
    referenced = "d" * 64
    stale = "e" * 64
    _put(store, referenced, b"r" * 5, age_days=4000)
    _put(store, stale, b"s" * 3, age_days=400)

    async def _refs():
        return {referenced}, {referenced, stale}

    monkeypatch.setattr(ic, "_reference_sets", _refs)

    out = await ic.checkpoint_gc(ic.GcRequest(dry_run=False), _request())

    assert out["deleted"] == 1
    assert (store / ("d" * 2) / ("d" * 62)).exists(), "被引用的分片永远不删"
    assert not (store / ("e" * 2) / ("e" * 62)).exists()
