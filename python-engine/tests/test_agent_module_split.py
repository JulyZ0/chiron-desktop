"""「跨副本等待」与「system 段装配」抽取后的**防回流**防线（P2-1）。

两块实现搬到了 `app.agent.remote_wait` / `app.agent.prompt_parts`，`runtime` 以原名
再导出。这里不做行为验证（行为由 `test_remote_wake_poll.py` 与 `test_prompt_sections.py`
覆盖），只钉住一件事：**runtime 里是转发，不是又一次复制**。

为什么要单独钉：这类"搬出去再抄回来"的分叉最难发现 —— 两边都"看起来对"，差异只在
某次修补时改了其中一份。用对象同一性判定，抄回来就立刻红。
"""

from __future__ import annotations

from app.agent import prompt_parts, remote_wait, runtime


def test_system_sections_assembly_is_forwarded():
    # runtime 只是换了个更好用的名字（`_build_system_sections`），函数体只有一份。
    assert runtime._build_system_sections is prompt_parts.build_system_sections


def test_remote_wait_helpers_are_forwarded():
    assert runtime._approval_key is remote_wait.approval_key
    assert runtime._approval_wake_channel is remote_wait.approval_wake_channel
    assert runtime._approval_scope is remote_wait.approval_scope
    assert runtime.REMOTE_WAIT_POLL_INTERVAL_SECONDS == remote_wait.REMOTE_WAIT_POLL_INTERVAL_SECONDS


def test_remote_wait_methods_delegate_to_module():
    # 方法保留是为了既有调用点与 `AgentRuntime.__new__` 式单测；实现应在模块里。
    import inspect

    src = inspect.getsource(runtime.AgentRuntime._take_key_with_wake)
    assert "remote_wait.take_key_with_wake" in src
    # 原地实现的特征（GET+DEL 与 pubsub 订阅）不该再出现在 runtime 的方法体里
    assert "pubsub" not in src
