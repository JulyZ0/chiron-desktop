"""worker 启动自愈：卡在 indexing 的库会被重新投递。"""

from __future__ import annotations

import asyncio

from app import index_worker


class _Store:
    async def stuck_indexing(self):
        return [{"id": "kb-1", "user_id": "u-1"}]


class _Redis:
    def __init__(self) -> None:
        self.added: list[tuple[str, dict]] = []

    async def xadd(self, stream, fields, maxlen=None):  # noqa: ANN001
        self.added.append((stream, fields))


class _Cfg:
    kb_stream = "engine:tasks:kb"
    kb_stream_maxlen = 1000


def test_recover_stuck_reenqueues_indexing_kbs() -> None:
    redis = _Redis()
    asyncio.run(index_worker._recover_stuck(redis, _Cfg(), _Store()))
    assert len(redis.added) == 1
    stream, fields = redis.added[0]
    assert stream == "engine:tasks:kb"
    assert '"kb_id": "kb-1"' in fields["payload"]
