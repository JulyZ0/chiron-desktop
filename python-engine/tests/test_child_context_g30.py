"""G30：子 Agent 首轮上下文组装器（四段按固定序号注入）。

**修前会怎样**：子 Agent 的首条消息里只有任务文本（外加可选的继承/续跑上下文）。
它读不到三件事：

1. **工作目录**是哪一个绝对路径 —— 于是它按自己的猜测拼相对路径；
2. **文件策略**是只读还是可写 —— 只读会话里它会反复 write_file 被拒（每一次都是一整次
   往返，用户看到的是"子 Agent 莫名失败"）；可写会话里它又常自我推断"没被授权"，明明
   能改文件却只写一份报告；
3. **审批模式**与**自己的权限面在启动时就固定了** —— 审批被自动拒绝时它会重试同一个
   操作，而不是把限制写进结论让派发方处理。

本文件先钉住这四段的**存在、顺序、内容**，再钉住核心判据：**同一份派发在只读会话与
可写会话里拼出的文本必须不同**，而且两段环境文本走的是"运行/环境段"（首条用户消息），
**不是** system prompt —— 父子 system prompt 必须逐字一致（DSH 就是走 runtime-context）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent import runtime as runtime_module
from app.agent.subagent_runner import SubAgentRunner
from app.subagent import affinity, runtime_cache
from app.subagent import child_context as cc

# ── 测试替身：把"落库 / 缓存 / 归属"这些外部依赖换成空实现 ──────────────────


class _NullStore:
    """runner 的最小 store 替身（有这些方法就够，不落库）。"""

    async def start_run(self, *args: Any, **kwargs: Any) -> str:
        return ""

    async def add_step(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def flush_steps(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def finish_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mark_lifecycle(self, *args: Any, **kwargs: Any) -> None:
        return None


class _NoLease:
    """归属租约替身：本用例只关心首条消息，不碰 Redis。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _no_infra(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_cache() -> None:
        return None

    monkeypatch.setattr(runtime_cache, "get_runtime_cache", _no_cache)
    monkeypatch.setattr(affinity, "OwnerLease", _NoLease)


def _capture_first_message(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_write: bool,
    unattended: bool = False,
    expert_prompt: str = "你是评审专家",
) -> tuple[SubAgentRunner, dict[str, Any]]:
    """用假 `AgentRuntime.run` 驱动 runner，只把子任务的 `AgentTask` 抓下来。"""
    captured: dict[str, Any] = {}

    async def _run(self: Any, task: Any):  # noqa: ANN001
        captured["task"] = task
        yield runtime_module.AgentEvent(type="text", content="ok")

    monkeypatch.setattr(runtime_module.AgentRuntime, "run", _run)
    runner = SubAgentRunner(
        gateway=object(),
        store=_NullStore(),
        parent_session_id="s1",
        tenant_id=1,
        user_id="u1",
        allow_write=allow_write,
        unattended=unattended,
    )
    return runner, captured


# ── 1. 四段存在、按固定序号、带标记 ─────────────────────────────────────────


def _context(**overrides: Any) -> str:
    kwargs: dict[str, Any] = {
        "working_directory": "D:/ws",
        "sandbox": cc.SandboxState(
            mode=cc.SANDBOX_WORKSPACE_WRITE,
            writable_paths=("D:/ws",),
            isolation_level="restricted",
        ),
        "approval": cc.ApprovalState(prompts_enabled=True, tools_mode="ask"),
        "identity": cc.ChildIdentity(
            run_id="rs_1",
            parent_agent_id="s1",
            current_depth=1,
            max_depth=1,
            read_only=False,
        ),
    }
    kwargs.update(overrides)
    return cc.build_child_context(**kwargs)


def test_四段按固定序号拼进runtime_context标记块() -> None:
    text = _context()
    assert text.startswith(f"<{cc.CONTEXT_TAG}>")
    assert text.rstrip().endswith(f"</{cc.CONTEXT_TAG}>")

    order = [text.index(cc.SLOT_HEADINGS[key]) for _number, key in cc.SLOT_ORDER]
    assert order == sorted(order), f"槽位必须按固定序号排列：{cc.SLOT_ORDER}"

    # 序号本身也得是升序且互不相同（拼装顺序由它决定，不能靠调用顺序）
    numbers = [number for number, _key in cc.SLOT_ORDER]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)
    assert numbers == [100, 110, 115, 120, 125], "与 DSH dsh-system-prompt 的顺序常量对齐"


def test_工作目录段给出绝对路径() -> None:
    text = _context(working_directory="D:/ws/project")
    assert cc.HEADING_WORKING_DIRECTORY in text
    assert '"D:/ws/project"' in text, "路径要用引号包起来，模型才不会把它当普通单词"


# ── 2. 沙箱等级：三选一，且只读那段带"不要仅凭策略就拒绝"的指引 ─────────────


def test_只读会话与可写会话的文本不同() -> None:
    """**核心判据**：同一份派发，只读会话与可写会话拼出的文本必须不同。"""
    read_only = _context(
        sandbox=cc.SandboxState(mode=cc.SANDBOX_READ_ONLY),
        identity=cc.ChildIdentity(current_depth=1, max_depth=1, read_only=True),
    )
    writable = _context()
    assert read_only != writable
    assert cc.SANDBOX_READ_ONLY in read_only
    assert cc.SANDBOX_WORKSPACE_WRITE in writable
    # 只读那句必须带"别因为策略就自己拒活"的三点语义
    assert "不要仅凭" in read_only
    assert "正常调用" in read_only
    assert "拒绝" in read_only
    # 可写那句必须带可写路径
    assert '"D:/ws"' in writable


def test_完全访问是第三档独立文案() -> None:
    full = _context(sandbox=cc.SandboxState(mode=cc.SANDBOX_DANGER_FULL_ACCESS))
    assert cc.SANDBOX_DANGER_FULL_ACCESS in full
    assert "不限制文件修改" in full


def test_沙箱状态从真实工作区读取(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """可写路径必须来自 `app/tools/sandbox.workspace_dir()`（只读它，不改它）。"""
    from app.tools import sandbox as sandbox_mod

    monkeypatch.setattr(sandbox_mod, "workspace_dir", lambda: tmp_path)
    writable = cc.read_sandbox_state(read_only=False)
    read_only = cc.read_sandbox_state(read_only=True)

    assert writable.mode == cc.SANDBOX_WORKSPACE_WRITE
    assert str(tmp_path) in writable.writable_paths
    assert read_only.mode == cc.SANDBOX_READ_ONLY


# ── 3. 审批：二选一，且状态来自既有的单一来源 ───────────────────────────────


def test_审批开启与关闭是两套措辞() -> None:
    on = _context(approval=cc.ApprovalState(prompts_enabled=True, tools_mode="ask"))
    off = _context(approval=cc.ApprovalState(prompts_enabled=False, tools_mode="yolo"))
    assert on != off
    assert "fail" in on.lower() or "fail-closed" in on
    assert "没有可用的应答者" in on or "没有可用应答者" in on
    assert "不要请求" in off or "不要申请" in off
    assert "自动拒绝" in off


def test_审批状态从真实tools_mode与无人值守读取() -> None:
    # yolo = 不弹审批；ask/auto = 有审批；非法值按既有单一来源归一成 auto
    assert cc.read_approval_state(tools_mode="yolo", unattended=False).prompts_enabled is False
    assert cc.read_approval_state(tools_mode="ask", unattended=False).prompts_enabled is True
    assert cc.read_approval_state(tools_mode="auto", unattended=False).prompts_enabled is True
    assert cc.read_approval_state(tools_mode="乱写", unattended=False).tools_mode == "auto"
    # 无人值守（自动续跑）：没人能应答 ⇒ 与关闭同义
    assert cc.read_approval_state(tools_mode="ask", unattended=True).prompts_enabled is False


# ── 4. 子身份 + 范围 ────────────────────────────────────────────────────────


def test_子身份段四个语义点都在() -> None:
    text = _context()
    assert cc.HEADING_IDENTITY in text
    assert "启动时" in text  # 权限范围在启动时固定
    assert "无法" in text  # 会话内无法扩大
    assert "不要重试" in text  # 不重试被拒的操作
    assert "让派发你的 Agent 处理" in text or "说明这处限制" in text


# ── 5. 深度用尽：注入"不要调用，直接做" ─────────────────────────────────────


def test_深度用尽时注入不要调用该工具的指令() -> None:
    exhausted = _context(
        identity=cc.ChildIdentity(current_depth=1, max_depth=1, remaining=0)
    )
    assert "剩余 0" in exhausted
    assert "不要调用" in exhausted
    assert "直接" in exhausted

    roomy = _context(
        identity=cc.ChildIdentity(current_depth=1, max_depth=2, remaining=1)
    )
    assert "不要调用" not in roomy, "还有余量时不该劝退委派"


def test_剩余层数与runner的block_delegate同口径() -> None:
    # max_depth=0 ⇒ 禁止再委派（runner._resolve_tools 的 block_delegate 就是这个口径）
    assert cc.remaining_levels(current_depth=1, max_depth=0) == 0
    assert cc.remaining_levels(current_depth=1, max_depth=1) == 0
    assert cc.remaining_levels(current_depth=1, max_depth=2) == 1
    assert cc.remaining_levels(current_depth=2, max_depth=1) == 0


# ── 6. 注入通道：进首条用户消息，system prompt 逐字不动 ─────────────────────


@pytest.mark.asyncio
async def test_子首条消息带环境段且系统提示词保持原样(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, captured = _capture_first_message(monkeypatch, allow_write=False)
    await runner.run("把结论写出来", run_id="rs_g30a", expert_prompt="你是评审专家")

    task = captured["task"]
    assert task.system_prompt == "你是评审专家", (
        "环境段**不许**进 system prompt：父子 system prompt 必须逐字一致"
    )
    assert task.content.startswith(f"<{cc.CONTEXT_TAG}>")
    assert cc.HEADING_WORKING_DIRECTORY in task.content
    assert cc.HEADING_SANDBOX_POLICY in task.content
    assert cc.HEADING_APPROVAL_POLICY in task.content
    assert cc.HEADING_IDENTITY in task.content
    assert "把结论写出来" in task.content


@pytest.mark.asyncio
async def test_只读派发与可写派发的首条消息不同(monkeypatch: pytest.MonkeyPatch) -> None:
    """同一句任务，只读会话与可写会话送到子 Agent 的首条消息必须不同。"""
    ro_runner, ro_captured = _capture_first_message(monkeypatch, allow_write=False)
    await ro_runner.run("跑一下测试", run_id="rs_g30ro")
    rw_runner, rw_captured = _capture_first_message(monkeypatch, allow_write=True)
    await rw_runner.run("跑一下测试", run_id="rs_g30rw")

    ro_text = ro_captured["task"].content
    rw_text = rw_captured["task"].content
    assert ro_text != rw_text
    assert cc.SANDBOX_READ_ONLY in ro_text
    assert cc.SANDBOX_WORKSPACE_WRITE in rw_text


@pytest.mark.asyncio
async def test_无人值守派发的审批段与可应答派发不同(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watched, watched_captured = _capture_first_message(
        monkeypatch, allow_write=False, unattended=False
    )
    await watched.run("做点事", run_id="rs_g30w")
    blind, blind_captured = _capture_first_message(
        monkeypatch, allow_write=False, unattended=True
    )
    await blind.run("做点事", run_id="rs_g30b")

    assert watched_captured["task"].content != blind_captured["task"].content
    assert "自动拒绝" in blind_captured["task"].content
