"""预算越界必须在**会话历史**里留下结论，而不只是发一个流事件。

**这里证明什么**

`budget_exceeded` 恰好发生在最没有订阅者的两种场景里：自动续跑
（`app/main.py` 的 `_auto_resume_runner` 明确 `pass` 掉所有事件）与用户已经关掉客户端。
原先两条越界路径都只 `yield` 一个 `error` 事件然后 `return` —— 事件是流上的东西，
没人读就等于没发生。用户回来看到的会话停在半句上，分不清是额度用完、还是程序崩了。

这与"无人值守也要有结论"是同一条要求：收场方式可以是失败，但**失败的原因必须落在
用户一定读得到的通道上**。

**为什么不把 per-run 越界改成"可续 pause"**

听起来更"持久"，实际是空转：per-run 上限由 `_budget_from_env()` 在每段 `run()` 里从
环境变量重建（没有在线提额通道），而续跑会**继承已用量**（`resume.py` 的 `usage` →
`ResumeState.input_tokens`）。挂起后一旦续跑，轮首立刻再次越界，于是"挂起 → 放弃"循环，
比直接失败更糟。线级预算才有提额通道（`LineLedger.raise_cap_and_resume` 写 `cap_tokens`），
所以两条路径的措辞也必须不同 —— 见最后一条用例。
"""

from __future__ import annotations

from typing import Any

from app.agent import history_writer
from app.gateway.provider import ChatResponse, ToolCall

#: 一次调用就用掉这么多（够大，能在第二轮轮首越界）
_CALL_INPUT = 30
_CALL_OUTPUT = 10


class _ChattyGateway:
    """第一轮回工具调用（把循环推进到第二轮），之后正常收尾。

    必须推进到第二轮：预算检查在**每轮开头**做，首轮开头 `_tokens_spent()` 还是 0，
    永远不可能越界 —— 直接 `stop` 的 gateway 会让这个测试假装通过而其实什么都没测。
    """

    def __init__(self) -> None:
        self.calls = 0

    async def chat_stream(self, **kwargs: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            yield ChatResponse(
                content="",
                finish_reason="tool_calls",
                # 不存在的工具名：结果报错，但循环照常进入下一轮 —— 这里要数的不是工具成败
                tool_calls=[ToolCall(id="c1", name="__absent_tool__", arguments="{}")],
                input_tokens=_CALL_INPUT,
                output_tokens=_CALL_OUTPUT,
                cached_tokens=0,
            )
            return
        yield ChatResponse(
            content="好",
            finish_reason="stop",
            input_tokens=_CALL_INPUT,
            output_tokens=_CALL_OUTPUT,
            cached_tokens=0,
        )


class _LineSpy:
    """线账：让 `check_session` 报越界，per-run 各轴全部不限。"""

    def __init__(self, axis: str = "tokens") -> None:
        self._axis = axis

    async def check(self, *_a: Any, **_k: Any) -> str:
        return self._axis

    async def check_session(self, *_a: Any, **_k: Any) -> str:
        return self._axis

    async def reserve(self, *_a: Any, **_k: Any) -> str:
        return ""

    async def settle(self, *_a: Any, **_k: Any) -> None:
        return None

    async def pause_for_budget(self, *_a: Any, **_k: Any) -> bool:
        return True


async def _drive(
    monkeypatch: Any,
    *,
    run_tokens: int,
    ledger_axis: str = "",
    max_steps: int = 0,
) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """跑真 `runtime.run`，把"落历史"与"写终态"换成记录。

    `run_tokens=0` 表示 per-run token 轴不限；`max_steps=0` 表示 steps 轴不限。
    """
    import app.subagent.line_ledger as ledger_mod
    from app.agent.runtime import AgentRuntime, AgentTask

    monkeypatch.setenv("AGENT_RUN_MAX_TOKENS", str(run_tokens))
    monkeypatch.setenv("AGENT_MAX_STEPS", str(max_steps))
    monkeypatch.setenv("AGENT_RUN_MAX_TURNS", "0")
    monkeypatch.setenv("AGENT_MAX_SECONDS", "0")

    monkeypatch.setattr(ledger_mod, "get_line_ledger", lambda: _LineSpy(ledger_axis))

    persisted: list[dict[str, Any]] = []

    async def _persist_messages(
        _session_id: str,
        _tenant_id: Any,
        messages: list[dict[str, Any]],
        *,
        turn_id: str = "",
        roles: set[str] | None = None,
    ) -> int:
        # 只收终态说明：正常回合内的 tool 消息也走这里，用 roles 区分。
        if roles == {"assistant"}:
            persisted.extend(messages)
        return len(messages)

    monkeypatch.setattr(history_writer, "persist_messages", _persist_messages)

    runtime = AgentRuntime(gateway=_ChattyGateway())

    async def _load(_task: Any) -> Any:
        return None

    async def _save(*_a: Any, **_k: Any) -> None:
        return None

    async def _noop(*_a: Any, **_k: Any) -> None:
        return None

    marked: list[str] = []

    async def _mark(_task: Any, status: str) -> None:
        marked.append(status)

    monkeypatch.setattr(runtime, "_load_resume_state", _load)
    monkeypatch.setattr(runtime, "_save_checkpoint", _save)
    monkeypatch.setattr(runtime, "_mark_run_status", _mark)
    # 提额票的挂出会连库：这里只关心"结论有没有说清"，票据路径另有测试。
    monkeypatch.setattr(runtime, "_pause_session_line", _noop)

    task = AgentTask(
        id="run_notice",
        tenant_id="t1",
        user_id="u1",
        session_id="s-notice",
        content="跑",
        max_turns=3,
    )
    events = [event async for event in runtime.run(task)]
    return events, persisted, marked


def _error_of(events: list[Any]) -> str:
    for event in events:
        if event.type == "error":
            return str(event.error or "")
    return ""


async def test_per_run_budget_overflow_leaves_a_conclusion_in_history(monkeypatch) -> None:
    """越界只发事件 ⇒ 无人值守时用户回来只看到半句话。这里钉住"一定写进历史"。"""
    events, persisted, _marked = await _drive(monkeypatch, run_tokens=10)

    assert _error_of(events) == "budget_exceeded:tokens"
    assert persisted, "预算越界必须把结论写进会话历史，否则无人值守时用户无从知道为什么停了"

    text = "\n".join(str(m.get("content") or "") for m in persisted)
    assert "预算越界" in text
    assert "tokens" in text, "必须点出是哪个轴越界，否则用户不知道该要哪一档额度"


async def test_per_run_notice_does_not_promise_an_auto_resume(monkeypatch) -> None:
    """措辞必须诚实：per-run 额度是单段的，续跑会继承用量、接上来也立刻越界。

    说成"稍后会自动继续"是**错误承诺** —— 用户会去等一个永远不会来的续跑。
    """
    _events, persisted, _marked = await _drive(monkeypatch, run_tokens=10)

    text = "\n".join(str(m.get("content") or "") for m in persisted)
    assert "发下一条消息" in text, "要告诉用户怎样才能继续"
    assert "立刻越界" in text, "必须说明自动续跑不会接上，否则等于承诺了做不到的事"


async def test_line_overflow_notice_mentions_the_ticket(monkeypatch) -> None:
    """线级越界挂出了提额票 ⇒ 措辞要说"提额后会重新放行"，而不是"发条消息"。

    两条路径的收场方式不同，措辞混同会让用户走错路：去重发消息（白花钱）或
    去等自动续跑（永远不来）。
    """
    events, persisted, _marked = await _drive(monkeypatch, run_tokens=0, ledger_axis="tokens")

    assert _error_of(events) == "budget_exceeded:tokens"
    assert persisted, "线级越界同样必须把结论写进会话历史"

    text = "\n".join(str(m.get("content") or "") for m in persisted)
    assert "提额待办" in text
    assert "重新放行" in text
    # 不能复用 per-run 那句错误承诺
    assert "发下一条消息" not in text


async def test_steps_axis_overflow_writes_both_notice_and_terminal_status(monkeypatch) -> None:
    """steps 轴的缺口比另两处更重：它既不留结论，**也不写终态**。

    这一处 `return` 跳过了循环之后那句 `_mark_run_status(completed)`，而 `finally` 只在
    异常/取消时补写终态（正常 `return` 时 `_exc` 是 None）⇒ `agent_runs` 停在 `running`，
    被 reconciler 当僵尸收掉，用户回来连"为什么停了"和"它停没停"都不知道。
    """
    events, persisted, marked = await _drive(monkeypatch, run_tokens=0, max_steps=1)

    assert _error_of(events) == "budget_exceeded:steps"
    assert "failed" in marked, "steps 越界必须写终态，否则 run 会永远停在 running 被当僵尸回收"

    text = "\n".join(str(m.get("content") or "") for m in persisted)
    assert "steps" in text
    assert "发下一条消息" in text, "steps 与 tokens 同属单段额度，续跑能接上"


async def test_notice_is_skipped_when_there_is_no_conclusion_to_write(monkeypatch) -> None:
    """空说明 / 无会话 id 不写：免得往历史里插一条空 assistant 消息。"""
    from app.agent.runtime import AgentRuntime

    runtime = AgentRuntime(gateway=_ChattyGateway())
    persisted: list[dict[str, Any]] = []

    async def _persist(*_a: Any, **_k: Any) -> int:
        persisted.append({"called": True})
        return 1

    monkeypatch.setattr(history_writer, "persist_messages", _persist)

    class _Task:
        session_id = "s1"
        tenant_id = "t1"
        turn_id = "turn-1"

    await runtime._persist_terminal_notice(_Task(), [], "   ")
    assert not persisted

    _Task.session_id = ""
    await runtime._persist_terminal_notice(_Task(), [], "有话要说")
    assert not persisted
