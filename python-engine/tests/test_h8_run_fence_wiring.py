"""H8：run 围栏必须**真的被激活** —— `agent_submit` 的模块级名字解析回归。

为什么单独钉这一条（PRD 9.2 / H8）：

`app/main.py` 的 `agent_submit()` 在 SSE 生成器里调用 `activate_run_fence()` /
`deactivate_run_fence()`，但这两个名字此前**只在 `_pump_runtime()` 内部局部导入** ——
于是 `agent_submit` 作用域里根本不存在它们。后果不是"少一层校验"，而是**只要带了
session_id（几乎总是）就在建连后的第一行抛 NameError，整轮 run 直接被掐断**；只要
这个分支没被走到，围栏就等于从未启用，stale worker 的写操作也就无从被拒。

这条测试断言的是**名字解析**这一层事实：`app.main` 的模块命名空间里必须直接有这两个
函数，且它们和 `run_registry` 里的是同一对象（防止将来出现一个行为不同的同名副本）。
"""

from __future__ import annotations

import inspect

import app.main as main_mod
from app import run_registry


def test_main_exposes_run_fence_activators() -> None:
    assert hasattr(main_mod, "activate_run_fence"), (
        "app.main 必须在模块级导入 activate_run_fence —— 否则 agent_submit 里调用它会 NameError"
    )
    assert hasattr(main_mod, "deactivate_run_fence")
    assert main_mod.activate_run_fence is run_registry.activate_run_fence
    assert main_mod.deactivate_run_fence is run_registry.deactivate_run_fence


def test_agent_submit_body_references_fence_functions() -> None:
    """`agent_submit` 的源码里必须真的调用围栏（而不是被静默删掉）。"""
    source = inspect.getsource(main_mod.agent_submit)
    assert "activate_run_fence(" in source
    assert "deactivate_run_fence(" in source


def test_activate_roundtrip_binds_context_var() -> None:
    """围栏激活后 `current_run_fence()` 必须能看见它（这是"贯穿"的最小证据）。"""
    fence = run_registry.bind_run_fence("token-h8-regression", 3)
    assert fence is not None
    token = main_mod.activate_run_fence(fence)
    try:
        current = run_registry.current_run_fence()
        assert current is not None
        assert (current.run_token, current.generation) == ("token-h8-regression", 3)
    finally:
        main_mod.deactivate_run_fence(token)
    assert run_registry.current_run_fence() is None
