"""测试夹具。

只提供 ``Settings`` 构造助手。业务层的 ``create_app`` 已随 desktop-local 的
FastAPI 外壳一并移除（见 launcher.py 的模块文档：引导器不再是 HTTP 服务），
因此这里不再有 ``TestClient`` 夹具。
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "pg_enabled": False,
        "redis_enabled": False,
        # 心跳间隔压到 10ms + 只发 2 条，让 SSE 测试不用等 15s
        "sse_heartbeat_seconds": 0.01,
        "sse_max_heartbeats": 2,
    }
    defaults.update(overrides)
    return Settings(**defaults)
