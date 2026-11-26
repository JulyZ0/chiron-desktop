"""Provider 会话亲和（A4）：旁路调用要有稳定路由标识，且新增直调点必须显式表态。

背景：OpenCode 一类 provider 要求 `x-opencode-session`，缺失即 400。provider 层已有
"非空兜底"（显式会话 → 请求 trace → 进程常量），所以**正确性**不再依赖每个入口自觉；
但**亲和性**（同一件事走同一路由、prompt 缓存命中）仍然取决于入口是否给了稳定标识。

本文件做两件事：

1. `llm_session_scope()` 的行为：不覆盖已有会话、按 scope+key 派生、退出复位；
2. **静态清单**：所有直接调 `gateway.chat*` / `provider.chat` 的文件必须落在
   三类之一 —— 自己设置会话（self-managed）、旁路作用域（scoped）、或明确继承
   （inherit，附理由）。新增一个直调点却什么都不做 ⇒ 本用例失败。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.providers.session_context import (
    get_llm_session_id,
    llm_session_scope,
    reset_llm_session_id,
    set_llm_session_id,
    stable_key,
)

APP_DIR = Path(__file__).resolve().parents[1] / "app"

#: 直接调用 gateway/provider chat 的文件 → 它们**如何**获得会话标识。
#: scoped：必须出现 `llm_session_scope(`；self：必须出现 `set_llm_session_id(`；
#: inherit：由调用方/运行时设置，附理由（新增继承项必须能说清是谁设的）。
_SCOPED = {
    "app/memory/summariser.py",
    "app/memory/distill.py",
    "app/media/analyzer.py",
    "app/skill/generate.py",
    "app/context/manager.py",
    "app/gateway/router.py",
}
_SELF_MANAGED = {
    "app/api/desktop_infer.py",
    "app/api/openai_compat.py",
    "app/api/agents.py",
    "app/agent/bounded_llm.py",
    "app/core/task_router.py",
    "app/api/unified_executor.py",
}
#: 运行在某个 run 的 task 里（或 provider 内部定义）—— 会话由运行时/调用方设置。
_INHERIT = {
    "app/agent/runtime.py": "run() 开头设置 task.session_id，工具与压缩都继承它",
    "app/agent/subagent_runner.py": "子 run 由 runtime 设置 child.session_id 后执行",
    "app/agent/multi_agent.py": "调用方（agents API / unified_executor）负责设置",
    "app/context/branch_condense.py": "分叉浓缩在 runtime 回合内执行",
    "app/core/capabilities.py": "能力执行器在工具/会话上下文内",
    "app/tools/media.py": "工具调用，继承当前 run 的会话",
    "app/tools/pm.py": "工具调用，继承当前 run 的会话",
    "app/tools/skill.py": "工具调用，继承当前 run 的会话",
    "app/providers/llm/gateway.py": "gateway 包装层，不改变调用方上下文",
}

_CALL_RE = re.compile(r"\.chat(?:_stream)?\(|provider\.chat\(")


def _direct_call_files() -> set[str]:
    found: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if _CALL_RE.search(text):
            found.add(path.relative_to(APP_DIR.parent).as_posix())
    return found


def test_every_direct_chat_caller_is_known():
    """新增直调点必须显式选择"设置会话 / 作用域 / 说明继承"三者之一。"""
    known = _SCOPED | _SELF_MANAGED | set(_INHERIT)
    unknown = sorted(_direct_call_files() - known)
    assert not unknown, (
        "以下文件直接调用了 gateway/provider chat，但没有声明会话来源："
        f"{unknown}。请在 _SCOPED（加 llm_session_scope）、_SELF_MANAGED（设 set_llm_session_id）"
        "或 _INHERIT（写清由谁设置）中登记。"
    )


def test_scoped_files_use_the_helper():
    for rel in sorted(_SCOPED):
        text = (APP_DIR.parent / rel).read_text(encoding="utf-8")
        assert "llm_session_scope(" in text, f"{rel} 应使用 llm_session_scope 固定路由标识"


def test_self_managed_files_set_the_session():
    for rel in sorted(_SELF_MANAGED):
        text = (APP_DIR.parent / rel).read_text(encoding="utf-8")
        assert "set_llm_session_id(" in text, f"{rel} 应显式设置会话标识"


def test_inherit_entries_explain_who_sets_the_session():
    for rel, reason in _INHERIT.items():
        assert reason.strip(), f"{rel} 的继承理由不能为空"
        assert (APP_DIR.parent / rel).exists(), f"_INHERIT 登记了不存在的文件：{rel}"


@pytest.fixture
def clean_session():
    token = set_llm_session_id("")
    try:
        yield
    finally:
        reset_llm_session_id(token)


def test_scope_preserves_explicit_session(clean_session):
    token = set_llm_session_id("sess-main")
    try:
        with llm_session_scope("memory-summary", key="k1"):
            assert get_llm_session_id() == "sess-main"
        assert get_llm_session_id() == "sess-main"
    finally:
        reset_llm_session_id(token)


def test_scope_derives_stable_id_and_resets(clean_session):
    with llm_session_scope("memory-summary", key=stable_key("same input")):
        first = get_llm_session_id()
        assert first.startswith("memory-summary:")
        assert len(first) <= 128
    assert get_llm_session_id() == ""

    # 同一输入 → 同一标识；不同输入 → 不同标识
    with llm_session_scope("memory-summary", key=stable_key("same input")):
        assert get_llm_session_id() == first
    other = ""
    with llm_session_scope("memory-summary", key=stable_key("other input")):
        other = get_llm_session_id()
    assert other and other != first


def test_scope_is_nestable_without_leaking(clean_session):
    with llm_session_scope("outer", key="a"):
        outer = get_llm_session_id()
        with llm_session_scope("inner", key="b"):
            # 已有会话 ⇒ 内层不覆盖（沿用外层的标识）
            assert get_llm_session_id() == outer
        assert get_llm_session_id() == outer
    assert get_llm_session_id() == ""
