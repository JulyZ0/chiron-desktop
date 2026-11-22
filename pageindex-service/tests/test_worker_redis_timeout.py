"""worker 的 Redis 客户端必须给阻塞读留出足够的 socket 超时。

真实故障：`XREADGROUP ... BLOCK 5000` 与 redis-py 默认的阻塞读超时同量级，
每次读都抛 `TimeoutError` 并被 worker 的外层 except 吞掉 —— 进程活着、心跳不写、
索引队列永远没有消费者，界面表现为「一直索引中」。
"""

from __future__ import annotations

from types import SimpleNamespace

from app import index_worker


def test_redis_client_socket_timeout_exceeds_block(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_from_url(url, **kwargs):  # noqa: ANN001, ANN003
        captured["url"] = url
        captured.update(kwargs)
        return object()

    import redis.asyncio as aioredis

    monkeypatch.setattr(aioredis, "from_url", fake_from_url)
    index_worker._redis_client(SimpleNamespace(redis_url="redis://127.0.0.1:6379/0"))

    timeout = float(captured.get("socket_timeout") or 0)
    assert timeout > 5.0, "socket_timeout 必须大于 XREADGROUP 的 block(5s)"
    assert captured.get("socket_connect_timeout")
