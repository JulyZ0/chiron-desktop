"""G-P3：工具表变更时插一条 system 消息告知模型（回归测试）。

**这里钉住什么**

模型对"我有哪些工具"的认知来自**它看到的那份工具表**，而那份表跨轮会变
（`tool_search` 激活新工具、用户换插件、模式覆盖改白名单）。不告知的代价是双向的：

* **少**：它按记忆调一个已经不在表里的工具 —— 一次往返 + 一条失败结果，还得再花
  一次往返纠正；
* **多**：新激活的工具它不知道，于是继续用更笨的办法绕。

下面几条一旦回退，这两个故障就原样复现。措辞不在断言范围内（文案会改），
断言的是**行为约束**：什么时候必须出现、什么时候必须完全不出现。

对位 pi 的 `declareToolChanges`（`agent-loop.ts:342`，调用点 :119 与 :220）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.runtime import AgentRuntime, AgentTask, _apply_system_prefix
from app.agent.tool_change_notice import render_tool_change_notice

SYSTEM = "You are Chiron. Reply briefly in Chinese."


# ── 渲染层 ──────────────────────────────────────────────


def test_added_tools_are_named():
    out = render_tool_change_notice(
        previous={"read_file"}, current={"read_file", "git_status"}
    )

    assert "git_status" in out
    assert "Now available" in out
    assert "No longer available" not in out


def test_removed_tools_are_named():
    """撤掉的工具必须说 —— 否则模型会继续按记忆调它（每次都失败、每次都要重试）。"""
    out = render_tool_change_notice(
        previous={"read_file", "git_status"}, current={"read_file"}
    )

    assert "git_status" in out
    assert "No longer available" in out
    assert "Now available" not in out


def test_both_directions_in_one_notice():
    out = render_tool_change_notice(
        previous={"read_file", "old_tool"}, current={"read_file", "new_tool"}
    )

    assert "new_tool" in out
    assert "old_tool" in out


def test_unchanged_set_renders_empty():
    """工具表没变 ⇒ 空串。绝大多数轮次走这条路，system 段必须逐字不变。"""
    assert render_tool_change_notice(previous={"a", "b"}, current={"b", "a"}) == ""


def test_it_points_at_the_request_as_truth():
    """声明不能只是"变了"——还要告诉模型拿什么当准（本请求的工具表 / tool_search）。"""
    out = render_tool_change_notice(previous={"a"}, current={"a", "b"})

    assert "tool_search" in out
    assert "truth" in out.lower()


def test_long_additions_are_truncated_but_counted():
    """一次激活二十个工具时不能把声明写成半屏目录 —— 超出只报数量。"""
    added = {f"tool_{i}" for i in range(30)}
    out = render_tool_change_notice(previous=set(), current=added)

    assert "+18 more" in out, "超出部分必须报数量，否则模型以为就这些"
    assert "tool_29" not in out


# ── 跨轮状态（runtime 方法）──────────────────────────────


def _runtime() -> AgentRuntime:
    # gateway 在本组用例里用不到：只测纯逻辑，不驱动 run()。
    return AgentRuntime(gateway=Any)  # type: ignore[arg-type]


def test_first_turn_makes_no_claim():
    """首轮没有基准：声明"变了什么"只能靠编，所以必须是空串。"""
    rt = _runtime()

    assert rt._tool_change_notice({"read_file", "shell_exec"}) == ""


def test_memory_is_primed_even_when_the_first_notice_is_empty():
    """首轮虽不声明，但**必须记住**这次的表 —— 否则第二轮永远比不了，等于不生效。"""
    rt = _runtime()

    assert rt._tool_change_notice({"read_file"}) == ""
    # 第二轮与首轮相同 ⇒ 仍然空（证明真的存下来了，而不是每轮都当首轮）
    assert rt._tool_change_notice({"read_file"}) == ""
    # 第三轮多了一个 ⇒ 这才该说
    assert "git_status" in rt._tool_change_notice({"read_file", "git_status"})


def test_activation_between_turns_is_declared_once():
    """`tool_search` 激活新工具（典型跨轮变化）：说一次，之后不再重复。"""
    rt = _runtime()
    rt._tool_change_notice({"read_file"})

    first = rt._tool_change_notice({"read_file", "git_status"})
    assert "git_status" in first

    assert rt._tool_change_notice({"read_file", "git_status"}) == "", (
        "变过之后就是新基准了，不能每轮重报 —— 那会把声明变成常驻噪音"
    )


def test_shrinkage_is_declared():
    rt = _runtime()
    rt._tool_change_notice({"read_file", "git_status"})

    assert "git_status" in rt._tool_change_notice({"read_file"})


# ── system 段落点 ────────────────────────────────────────


def _task() -> AgentTask:
    return AgentTask(
        id="t1",
        tenant_id="t",
        user_id="u",
        session_id="s1",
        content="继续",
        system_prompt=SYSTEM,
        max_turns=3,
    )


def test_notice_lands_as_its_own_system_message():
    """独立成 system 消息，不拼进 `system_prompt` —— 拼进去会让稳定的前缀缓存整段失效。"""
    notice = render_tool_change_notice(previous={"a"}, current={"a", "b"})
    messages = _apply_system_prefix(
        [{"role": "user", "content": "go"}], _task(), tool_change_notice=notice
    )

    system_msgs = [m for m in messages if m["role"] == "system"]
    assert len(system_msgs) == 2
    assert str(system_msgs[0]["content"]) == SYSTEM, "稳定前缀必须逐字不变"
    assert str(system_msgs[1]["content"]) == notice


def test_notice_sits_last_in_the_system_block():
    """位置在 system 段末尾：它是"本轮工具表"的即时说明，离请求越近越不易被冲淡。"""
    task = _task()
    task.memory_context = "MEM"
    notice = render_tool_change_notice(previous={"a"}, current={"a", "b"})
    messages = _apply_system_prefix(
        [{"role": "user", "content": "go"}], task, tool_change_notice=notice
    )

    # 稳定前缀 + 记忆 + 声明 三条 system，声明排在最后
    assert [m["role"] for m in messages] == ["system", "system", "system", "user"]
    assert str(messages[1]["content"]) == "MEM"
    assert str(messages[2]["content"]) == notice


def test_empty_notice_keeps_the_system_block_byte_identical():
    """没变化（默认情况）⇒ system 段与引入本机制之前逐字相同。"""
    without = _apply_system_prefix([{"role": "user", "content": "go"}], _task())
    with_empty = _apply_system_prefix(
        [{"role": "user", "content": "go"}], _task(), tool_change_notice=""
    )

    assert without == with_empty


def test_prefix_cache_breakpoint_survives():
    """断点仍在**稳定前缀**那条上，不能跑到每轮都变的声明上。"""
    notice = render_tool_change_notice(previous={"a"}, current={"b"})
    messages = _apply_system_prefix(
        [{"role": "user", "content": "go"}], _task(), tool_change_notice=notice
    )

    assert messages[0].get("cache_breakpoint") is True
    assert not messages[1].get("cache_breakpoint")


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
