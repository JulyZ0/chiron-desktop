"""G23：两个 LSP 工具（`lsp_definition` / `lsp_diagnostics`）的用例。

钉住四件事：

1. **未配置语言服务器 → 明确报错**（不猜、不内置任何第三方命令）；
2. **路径越界 → 拒绝**（走 `app/tools/sandbox.safe_join`，与其它文件工具同一道边界）；
3. **正常路径 → 结构化结果**（假服务器回固定结果，断言"1 基 ↔ 0 基"换算、
   工作区相对路径、severity 名称）；
4. **没有声明并发安全**（语言服务器是有状态进程，声明了会让调度器并行打进来）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.tools.lsp import tools as lsp_tools
from app.tools.lsp.config import LspConfigError
from app.tools.lsp.session import LspSessionManager
from app.tools.registry import registry
from tests.source_scan import code_of
from tests.test_lsp_fake_server_g23 import (
    direct_spawn,
    fake_command,
    write_fake_server,
)

SAMPLE = "def 目标():\n    return 1\n\n值 = 目标()\n"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把工作区钉到临时目录（否则 safe_join 会指向真实沙箱根，越界判定不可控）。"""
    from app.tools import sandbox as sandbox_mod

    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path / "sbx"))
    monkeypatch.setattr(sandbox_mod, "project_root", lambda: None)
    return sandbox_mod.workspace_dir()


@pytest.fixture
def sample(workspace: Path) -> Path:
    path = workspace / "sample.py"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


@pytest.fixture
async def manager():
    """每个用例一个干净的管理器（真进程、直连 spawn），结束后确保进程都关掉。"""
    instance = LspSessionManager(spawn=direct_spawn)
    lsp_tools.set_manager(instance)
    try:
        yield instance
    finally:
        await instance.unload_all()
        lsp_tools.set_manager(None)


@pytest.fixture
def script(tmp_path: Path) -> Path:
    return write_fake_server(tmp_path)


# ---------------------------------------------------------------------------
# 未配置
# ---------------------------------------------------------------------------


async def test_未配置语言服务器返回可读错误(sample: Path, manager: LspSessionManager) -> None:
    """没有 `CHIRON_LSP_PYTHON` 时必须直接报错，并告诉用户**该设哪个变量**。

    修前会怎样：任何"猜一个常见命令（pylsp / pyright）"的实现都会在没装的机器上
    变成难查的 WinError 2；而猜错命令（比如把 clangd 当成 C++ 服务器）更糟 ——
    它是有响应的，只是结果全错。
    """
    result = await lsp_tools.lsp_definition(path="sample.py", line=1, character=1)
    assert result.get("code") == "lsp_not_configured"
    assert "CHIRON_LSP_PYTHON" in result["error"]
    assert manager.keys() == [], "未配置时不该起任何进程"


async def test_未知扩展名且未指定语言时报错(workspace: Path, manager: LspSessionManager) -> None:
    weird = workspace / "数据.weird"
    weird.write_text("x\n", encoding="utf-8")
    result = await lsp_tools.lsp_definition(path="数据.weird", line=1, character=1)
    assert result.get("code") == "lsp_language_unknown"
    assert "language" in result["error"]


# ---------------------------------------------------------------------------
# 路径边界
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../../../../README.md",
        "..\\..\\..\\README.md",
    ],
)
async def test_路径越界被拒绝(
    path: str, sample: Path, manager: LspSessionManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相对路径逃逸 → 拒绝，且**不起进程**（边界检查必须在最前面）。"""
    monkeypatch.setenv("CHIRON_LSP_PYTHON", fake_command(sample.parent / "server.py"))
    result = await lsp_tools.lsp_definition(path=path, line=1, character=1)
    assert result.get("code") == "path_escapes_sandbox"
    assert manager.keys() == []


async def test_工作区外的绝对路径被拒绝(
    tmp_path: Path, sample: Path, manager: LspSessionManager
) -> None:
    outside = tmp_path / "外面.py"
    outside.write_text("x = 1\n", encoding="utf-8")
    result = await lsp_tools.lsp_diagnostics(path=str(outside))
    assert result.get("code") == "path_escapes_sandbox"


async def test_文件不存在时给出可读错误(
    workspace: Path, manager: LspSessionManager, monkeypatch: pytest.MonkeyPatch, script: Path
) -> None:
    monkeypatch.setenv("CHIRON_LSP_PYTHON", fake_command(script))
    result = await lsp_tools.lsp_definition(path="没有这个文件.py", line=1, character=1)
    assert result.get("code") == "lsp_file_unreadable"


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


async def test_跳转定义返回结构化结果(
    sample: Path, manager: LspSessionManager, monkeypatch: pytest.MonkeyPatch, script: Path
) -> None:
    """入参 1 基、LSP 内部 0 基、输出又是 1 基 —— 三段换算必须对得上。

    假服务器把定义放在"入参行 + 1 行"，所以 line=2 应得到 line=3（1 基）：
    若哪段漏了 +/-1，这里会稳定偏一行，而"看起来仍然是个合法位置"。
    """
    monkeypatch.setenv("CHIRON_LSP_PYTHON", fake_command(script))
    result = await lsp_tools.lsp_definition(path="sample.py", line=2, character=3)
    assert result.get("count") == 1
    location = result["locations"][0]
    assert location["path"] == "sample.py", "输出应当是工作区相对路径，不是宿主绝对路径"
    assert location["line"] == 3
    assert location["character"] == 1
    assert location["end_line"] == 3
    assert location["uri"].startswith("file://")


async def test_跳转定义无结果时返回空列表(
    sample: Path, manager: LspSessionManager, monkeypatch: pytest.MonkeyPatch, script: Path
) -> None:
    """`result: null`（真实服务器对"找不到定义"的常见回答）→ count=0，不是报错。

    修前会怎样：把 null 当成"协议错误"的实现会让"这里没有定义"变成一条红色
    工具失败，模型据此以为工具坏了、开始改用 grep 硬找。
    """
    monkeypatch.setenv("FAKE_LSP_MODE", "null")
    monkeypatch.setenv("CHIRON_LSP_PYTHON", fake_command(script))
    result = await lsp_tools.lsp_definition(path="sample.py", line=1, character=1)
    assert result.get("count") == 0
    assert result["locations"] == []
    assert "error" not in result


async def test_诊断返回结构化结果_推送模型(
    sample: Path, manager: LspSessionManager, monkeypatch: pytest.MonkeyPatch, script: Path
) -> None:
    """默认（push）模型：didOpen 之后服务器推 `publishDiagnostics`。"""
    monkeypatch.setenv("CHIRON_LSP_PYTHON", fake_command(script))
    result = await lsp_tools.lsp_diagnostics(path="sample.py")
    assert result.get("count") == 1
    assert result["source"] == "push"
    diagnostic = result["diagnostics"][0]
    assert diagnostic["severity"] == "error", f"severity 应归一成名字，实得 {diagnostic['severity']!r}"
    assert diagnostic["line"] == 2
    assert diagnostic["path"] == "sample.py"
    assert "假诊断" in diagnostic["message"]
    assert diagnostic["code"] == "F001"
    assert diagnostic["source"] == "fake-lsp"


async def test_诊断返回结构化结果_拉取模型(
    sample: Path, manager: LspSessionManager, monkeypatch: pytest.MonkeyPatch, script: Path
) -> None:
    """服务器声明 `diagnosticProvider` 时走 `textDocument/diagnostic`（拉取）。

    为什么两种都要支持：3.17 之后新服务器（pyright、rust-analyzer）用拉取模型，
    老服务器（pylsp 的一部分插件）只推 —— 只做一种就等于一半的语言用不了。
    """
    monkeypatch.setenv("FAKE_LSP_MODE", "pull")
    monkeypatch.setenv("CHIRON_LSP_PYTHON", fake_command(script))
    result = await lsp_tools.lsp_diagnostics(path="sample.py")
    assert result.get("count") == 1
    assert result["source"] == "pull"
    assert result["diagnostics"][0]["severity"] == "error"


# ---------------------------------------------------------------------------
# 注册与并发安全
# ---------------------------------------------------------------------------


def test_两个工具都已注册() -> None:
    """`app/tools/__init__.py` 里那一行 import 必须真的把工具挂进注册表。"""
    assert registry.get("lsp_definition") is not None
    assert registry.get("lsp_diagnostics") is not None


def test_没有声明并发安全() -> None:
    """工具**不声明**并发安全：语言服务器是串行、有状态的进程。

    这里用源码守卫而不是行为断言：注册表里根本没有这个字段（DSH 侧才有），
    所以"没写"就是正确状态 —— 一旦有人照抄别处的 `is_concurrency_safe=True`，
    调度器就会并发打进来，而这里的会话锁会让第二个请求静默排队到超时。
    """
    code = code_of("app/tools/lsp/tools.py")
    assert "is_concurrency_safe" not in code
    assert "concurrency_safe" not in code


def test_配置错误类型可读() -> None:
    """`LspConfigError` 的消息要能直接展示给用户（含变量名与示例）。"""
    error = LspConfigError("未配置语言服务器（language='python'）：请设置 CHIRON_LSP_PYTHON")
    assert "CHIRON_LSP_PYTHON" in str(error)


def test_假服务器脚本可编译(script: Path) -> None:
    """假服务器本身是测试资产：语法坏了会让三个用例文件同时红，这里先兜住。"""
    source = script.read_text(encoding="utf-8")
    compile(source, str(script), "exec")
    assert sys.executable
