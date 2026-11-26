"""库级索引状态里的 **worker 心跳**（A1）。

修前：`/v1/kb/{id}/status` 只在"库 indexing 且已停滞 2 分钟"时才探测
pageindex-worker 心跳 —— 用户在那段时间里只看到「索引中」，真实原因（worker
根本没起）要等看门狗把库改成 error 才浮现。现在心跳作为 `worker_alive` 字段
**无条件**下发（10s 缓存），页面第一次轮询就能提示。

本文件钉住四件事：
1. 停滞 + 无心跳 ⇒ 收口成 error（与原看门狗一致），并带可操作原因；
2. 未停滞 + 无心跳 ⇒ **不改状态**，但字段必须是 False（前端才能提前提示）；
3. 库已 ready ⇒ 心跳仍如实上报（不影响状态）；
4. Cloud 模式没有本地 worker ⇒ `None`（不误报）。
"""

from __future__ import annotations

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.knowledge import service


class _Pool:
    def __init__(self, kb: dict, documents: list[dict]):
        self._kb = kb
        self._docs = documents

    async def fetchrow(self, sql: str, *args: object) -> dict:
        return self._kb

    async def fetch(self, sql: str, *args: object) -> list[dict]:
        return self._docs


def _kb(*, status: str = "indexing", provider: str = "selfhosted", stale: bool = True) -> dict:
    return {"status": status, "error_message": "", "provider": provider, "stale": stale}


def _doc(status: str = "indexing") -> dict:
    return {
        "id": "d1",
        "name": "d1.pdf",
        "status": status,
        "page_count": 1,
        "error_message": "",
        "file_size_bytes": 10,
        "updated_at": None,
    }


async def _status(monkeypatch, *, kb: dict, alive: bool | None, docs: list[dict] | None = None) -> dict:
    pool = _Pool(kb, docs if docs is not None else [_doc()])
    monkeypatch.setattr(service, "_pool", lambda: _async_value(pool))

    async def _alive() -> bool | None:
        return alive

    monkeypatch.setattr(service, "_kb_worker_alive", _alive)
    return await service.index_status(kb_id="kb-1")


async def _async_value(value):
    return value


async def test_stalled_indexing_without_worker_becomes_error(monkeypatch):
    out = await _status(monkeypatch, kb=_kb(stale=True), alive=False)
    assert out["status"] == "error"
    assert out["active"] is False
    assert out["worker_alive"] is False
    assert "索引执行器未运行" in out["error_message"]


async def test_fresh_indexing_without_worker_only_flags_it(monkeypatch):
    """未停滞时**不改状态**（任务可能刚投递），但字段必须让前端能提前提示。"""
    out = await _status(monkeypatch, kb=_kb(stale=False), alive=False)
    assert out["status"] == "indexing"
    assert out["active"] is True
    assert out["worker_alive"] is False


async def test_ready_library_still_reports_worker_health(monkeypatch):
    out = await _status(
        monkeypatch, kb=_kb(status="ready", stale=False), alive=False, docs=[_doc("ready")]
    )
    assert out["status"] == "ready"
    assert out["worker_alive"] is False


async def test_alive_worker_never_rewrites_status(monkeypatch):
    out = await _status(monkeypatch, kb=_kb(stale=True), alive=True)
    assert out["status"] == "indexing"
    assert out["worker_alive"] is True
    assert not out["error_message"]


async def test_cloud_mode_reports_unknown_worker(monkeypatch):
    """Cloud 模式的索引在远端执行：本地没有心跳是正常的，不能判 error。"""
    out = await _status(monkeypatch, kb=_kb(provider="cloud", stale=True), alive=False)
    assert out["status"] == "indexing"
    assert out["worker_alive"] is None
    assert not out["error_message"]
