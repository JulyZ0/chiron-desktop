"""P1-1：破坏性动作永不并发（即使误标 concurrency_safe）。

ZCode 的工具元数据把 `destructive` 作为独立轴；Chiron 之前只有 `concurrency_safe`
一个布尔。一个删除/覆盖类工具如果被误标可并发，顺序就变得不可预测 —— 这类错误不该
靠 review 兜底，应由调度器一票否决。
"""

from __future__ import annotations

import app.main  # noqa: F401 — 初始化 app 包
from app.agent.runtime import _split_tool_batch
from app.tools.registry import registry


def _register(name: str, *, safe: bool, destructive: bool = False) -> None:
    async def _handler(**_kwargs: object) -> dict[str, object]:
        return {}

    registry.register(
        name=name,
        description=name,
        parameters={"type": "object", "properties": {}},
        handler=_handler,  # type: ignore[arg-type]
        concurrency_safe=safe,
        destructive=destructive,
    )


def _call(name: str, cid: str, args: str = "{}") -> dict[str, str]:
    return {"id": cid, "name": name, "arguments": args}


def test_destructive_flag_wins_over_concurrency_safe():
    try:
        _register("t_safe_read", safe=True)
        _register("t_danger_delete", safe=True, destructive=True)
        _register("t_plain", safe=False)

        assert registry.is_parallel_safe("t_safe_read") is True
        assert registry.is_parallel_safe("t_danger_delete") is False
        assert registry.is_parallel_safe("t_plain") is False

        parallel, serial = _split_tool_batch(
            [
                _call("t_safe_read", "1"),
                _call("t_danger_delete", "2"),
                _call("t_plain", "3"),
            ]
        )
        assert [c["id"] for c in parallel] == ["1"]
        assert sorted(c["id"] for c in serial) == ["2", "3"]
    finally:
        for name in ("t_safe_read", "t_danger_delete", "t_plain"):
            registry._tools.pop(name, None)


def test_same_path_still_forces_serial_for_safe_tools():
    try:
        _register("t_read_two", safe=True)
        parallel, serial = _split_tool_batch(
            [
                _call("t_read_two", "1", '{"path": "same.py"}'),
                _call("t_read_two", "2", '{"path": "same.py"}'),
            ]
        )
        assert parallel == []
        assert sorted(c["id"] for c in serial) == ["1", "2"]
    finally:
        registry._tools.pop("t_read_two", None)
