"""Agent 协同 DAG 调度器测试。

核心意图:
- 拓扑分波必须尊重依赖关系: 依赖未完成的任务不能进入当前波次
- 无依赖的任务应被分到同一波次 (可并发执行)
- 依赖环必须被检测并降级为线性执行,而非死循环
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.agent.collaboration import AgentHub


def _hub() -> AgentHub:
    # _topological_waves 是纯函数,不依赖真实 gateway
    return AgentHub(gateway=MagicMock())


def test_no_dependencies_all_in_one_wave():
    """全部无依赖 → 单波次并发执行。"""
    hub = _hub()
    subtasks = [
        {"role": "researcher", "description": "a", "dependencies": []},
        {"role": "coder", "description": "b", "dependencies": []},
        {"role": "reviewer", "description": "c", "dependencies": []},
    ]
    waves = hub._topological_waves(subtasks)
    assert len(waves) == 1
    assert sorted(waves[0]) == [0, 1, 2]


def test_chain_dependencies_form_sequential_waves():
    """链式依赖 0→1→2 → 三波次顺序执行。"""
    hub = _hub()
    subtasks = [
        {"role": "researcher", "description": "a", "dependencies": []},
        {"role": "coder", "description": "b", "dependencies": ["subtask_0"]},
        {"role": "reviewer", "description": "c", "dependencies": ["subtask_1"]},
    ]
    waves = hub._topological_waves(subtasks)
    assert waves == [[0], [1], [2]]


def test_diamond_dependency():
    """菱形依赖: 0 → (1, 2) → 3。1 和 2 应同波并发。"""
    hub = _hub()
    subtasks = [
        {"role": "planner", "description": "root", "dependencies": []},
        {"role": "researcher", "description": "left", "dependencies": ["subtask_0"]},
        {"role": "coder", "description": "right", "dependencies": ["subtask_0"]},
        {"role": "orchestrator", "description": "join", "dependencies": ["subtask_1", "subtask_2"]},
    ]
    waves = hub._topological_waves(subtasks)
    assert waves[0] == [0]
    assert sorted(waves[1]) == [1, 2]
    assert waves[2] == [3]


def test_cycle_detected_and_degraded_to_linear():
    """依赖环 → 检测到环,剩余任务合并为单波线性执行,不死循环。"""
    hub = _hub()
    subtasks = [
        {"role": "researcher", "description": "a", "dependencies": ["subtask_1"]},
        {"role": "coder", "description": "b", "dependencies": ["subtask_0"]},
    ]
    waves = hub._topological_waves(subtasks)
    # 环: 无 ready 节点 → 剩余合并到一波
    assert len(waves) == 1
    assert sorted(waves[0]) == [0, 1]


def test_partial_cycle_continues_with_acyclic_part():
    """部分成环: 无环节点正常分波,环节点降级。"""
    hub = _hub()
    subtasks = [
        {"role": "researcher", "description": "free", "dependencies": []},
        {"role": "coder", "description": "a", "dependencies": ["subtask_2"]},
        {"role": "reviewer", "description": "b", "dependencies": ["subtask_1"]},
    ]
    waves = hub._topological_waves(subtasks)
    # subtask_0 无依赖进第一波, 1/2 互为环 → 降级到第二波
    assert waves[0] == [0]
    assert sorted(waves[1]) == [1, 2]


def test_integer_dependency_references_supported():
    """整数依赖引用 (兼容格式) 也能正确解析。"""
    hub = _hub()
    subtasks = [
        {"role": "researcher", "description": "a", "dependencies": []},
        {"role": "coder", "description": "b", "dependencies": [0]},
    ]
    waves = hub._topological_waves(subtasks)
    assert waves == [[0], [1]]


def test_unknown_dependency_string_ignored():
    """无法解析的依赖串 (不匹配 subtask_N) 被忽略,不阻塞调度。"""
    hub = _hub()
    subtasks = [
        {"role": "researcher", "description": "a", "dependencies": ["nonexistent_ref"]},
    ]
    waves = hub._topological_waves(subtasks)
    assert waves == [[0]]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
