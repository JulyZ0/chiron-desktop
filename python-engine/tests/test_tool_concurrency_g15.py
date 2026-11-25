"""G15：并发安全由工具**显式声明**，未声明一律串行。

## 修前会怎样

`runtime._split_tool_batch` 用"动作分级 = read"**推断**能不能并发。分级表回答的是
"要不要人确认"，不是"能不能并发"：两者只是**大部分重叠**，不是同一个问题 ——
`compact_conversation` 是 read 级却要就地改写 `messages`（runtime 只能为它写一条
只看名字的硬排除），而 `skill_run` 也在 read 表里却会真的执行技能。

## 修后是什么

并发安全成为**工具自己的声明**（`register(concurrency_safe=True)`），registry 提供
`is_concurrency_safe(name) -> bool`；**未声明即 False**（保守默认，同 `tool_policy`
的 fail-closed 取向）。runtime 侧的接线另行进行，本文件只钉住 registry 的 API 与声明数据。

本文件按"修前失败"写的：`is_concurrency_safe` / `concurrency_safe` 参数都不存在时全红。
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolRegistry, is_concurrency_safe, registry


def _register(
    reg: ToolRegistry, name: str, *, concurrency_safe: bool = False
) -> None:
    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    reg.register(
        name=name,
        description="",
        parameters={"type": "object", "properties": {}},
        handler=handler,
        concurrency_safe=concurrency_safe,
    )


def test_undeclared_tool_is_not_concurrency_safe() -> None:
    """① 未声明 → False（保守默认）。未知工具名同样 False。"""
    reg = ToolRegistry()
    _register(reg, "fixture_undeclared")

    assert reg.is_concurrency_safe("fixture_undeclared") is False
    assert reg.is_concurrency_safe("no_such_tool") is False
    assert reg.is_concurrency_safe("") is False


def test_declared_tool_is_concurrency_safe() -> None:
    """② 声明为真 → True。"""
    reg = ToolRegistry()
    _register(reg, "fixture_parallel", concurrency_safe=True)

    assert reg.is_concurrency_safe("fixture_parallel") is True


def test_declaration_is_per_tool() -> None:
    """③ 改一个工具的声明不影响其它工具（声明是按名字存的，不是全局开关）。"""
    reg = ToolRegistry()
    _register(reg, "fixture_a", concurrency_safe=True)
    _register(reg, "fixture_b", concurrency_safe=True)
    _register(reg, "fixture_c")

    assert reg.is_concurrency_safe("fixture_a") is True
    assert reg.is_concurrency_safe("fixture_b") is True
    assert reg.is_concurrency_safe("fixture_c") is False

    # 改声明：重新注册 c 为并发安全，a/b 不受影响
    _register(reg, "fixture_c", concurrency_safe=True)
    assert reg.is_concurrency_safe("fixture_c") is True
    assert reg.is_concurrency_safe("fixture_a") is True
    assert reg.is_concurrency_safe("fixture_b") is True

    # 反向改：把 b 收回声明，a/c 不变
    _register(reg, "fixture_b", concurrency_safe=False)
    assert reg.is_concurrency_safe("fixture_b") is False
    assert reg.is_concurrency_safe("fixture_a") is True
    assert reg.is_concurrency_safe("fixture_c") is True

    # 注销只影响自己
    reg.unregister("fixture_a")
    assert reg.is_concurrency_safe("fixture_a") is False
    assert reg.is_concurrency_safe("fixture_c") is True


def test_read_only_tools_declare_their_safety() -> None:
    """④ 只读工具确实声明了（最小清单，全部是"不碰共享可变状态"的工具）。"""
    import app.tools  # noqa: F401 — 触发内置工具注册

    read_only = [
        "read_file",
        "ls",
        "glob_files",
        "grep_files",
        "search_files",
        "outline_file",
        "read_image",
        "git_status",
        "git_log",
        "git_diff",
        "read_tool_result",
        "read_archive",
        "read_subagent_result",
        "list_subagent_runs",
        "job_output",
        "kb_list",
        "kb_search",
        "memory_search",
        "recall",
        "skill_list",
        "skill_discover",
    ]
    missing = [name for name in read_only if not registry.is_concurrency_safe(name)]
    assert not missing, f"这些只读工具未声明 concurrency_safe：{missing}"


def test_side_effecting_tools_are_serial_by_default() -> None:
    """有副作用的工具**不得**声明并发安全（串行是默认，不需要额外声明）。"""
    import app.tools  # noqa: F401 — 触发内置工具注册

    for name in (
        "write_file",
        "edit_file",
        "delete_file",
        "shell_exec",
        "run_code",
        "subagent",
        "job_kill",
        "remember",
        "forget",
        "compact_conversation",
    ):
        assert registry.is_concurrency_safe(name) is False, name


def test_module_level_helper_reads_the_global_registry() -> None:
    """模块级 `is_concurrency_safe` 读的就是全局注册表（runtime 将按此接线）。"""
    import app.tools  # noqa: F401

    assert is_concurrency_safe("read_file") is True
    assert is_concurrency_safe("write_file") is False
    assert is_concurrency_safe("definitely_not_a_tool") is False
