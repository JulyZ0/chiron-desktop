"""代码守卫的**再导出逃逸**回归（H23）：AST 只认两段属性 + 运行时交真模块 = 沙箱逃逸。

背景（PRD 9.7 / H23）：`asyncio` 在安全 import 白名单里，而它夹带 `asyncio.base_events`，
后者自己 `import os`。于是

    import asyncio
    asyncio.base_events.os.system("id")

两层的旧实现都放行：

* 静态层只识别 `Name.attr(...)`（两段）—— 这里的 `func.value` 是 **Attribute** 而不是
  Name，整条链被静默跳过；
* 运行时层把**真模块对象**交给沙箱代码，`base_events.os` 照样点得开。

本文件钉住修复后的口径：白名单模块**不得暴露** `os/sys/subprocess/socket/importlib`
等危险对象；AST 与运行时 capability 校验各自都要拦住逃逸探针；插件 runner 用的是同一份
守卫（版本不达标即 fail-closed），因此插件通道同步生效。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.tools.code_guard import (
    GUARD_API_VERSION,
    capability_reason,
    check_static,
    safe_builtins,
)

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
_RUNNER = _ENGINE_ROOT / "plugin_runner.py"


# ── 静态（AST）层 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        # 攻击探针：白名单模块再导出 os
        "import asyncio\nasyncio.base_events.os.system('id')",
        "import asyncio\nasyncio.base_events.os",
        "import asyncio\nm = asyncio.base_events\nos_mod = m.os",
        # 同一类洞的其它形态
        "import json\njson.decoder.subprocess.run('id', shell=True)",
        "import datetime\ndatetime.sys",
        "import re\nre.socket",
        "import collections\ncollections.importlib",
    ],
)
def test_static_blocks_reexported_dangerous_module(code: str) -> None:
    assert check_static(code) is not None, f"{code!r} 必须被静态守卫拒绝（再导出逃逸）"


@pytest.mark.parametrize("code", ["import asyncio", "import json", "import math", "import datetime", "import re"])
def test_static_keeps_whitelisted_modules(code: str) -> None:
    assert check_static(code) is None, f"{code!r} 是模型常用能力，不能误伤"


@pytest.mark.parametrize(
    "code",
    [
        "import asyncio\nasyncio.sleep(0)",
        "import json\njson.dumps({'a': 1})",
        "import math\nmath.sqrt(4)",
        "import re\nre.compile('x').match('x')",
    ],
)
def test_static_allows_benign_attribute_chains(code: str) -> None:
    """链式访问本身不是罪证：只要链上取到的都是白名单能力就放行。"""
    assert check_static(code) is None


# ── 运行时 capability 层 ───────────────────────────────────────────────────


def _run(code: str) -> dict:
    namespace: dict = {"__builtins__": safe_builtins()}
    exec(compile(code, "<probe>", "exec"), namespace)  # noqa: S102 — 被测对象就是沙箱执行路径
    return namespace


@pytest.mark.parametrize(
    "code",
    [
        # 同一个探针，走运行时而不是 AST（两条独立通道各自必须能拦）
        "import asyncio\nasyncio.base_events.os.system('id')",
        "import asyncio\nasyncio.base_events.os",
        "import asyncio\nm = asyncio.base_events\nos_mod = m.os",
        "import asyncio\nasyncio.events.os",
    ],
)
def test_runtime_guard_blocks_reexported_dangerous_object(code: str) -> None:
    with pytest.raises(RuntimeError, match="blocked by runtime guard"):
        _run(code)


def test_runtime_guard_keeps_whitelisted_capabilities() -> None:
    """代理层不能把白名单模块的正常能力一起封掉（否则沙箱无法工作）。"""
    ns = _run(
        "import asyncio\nimport json\nimport math\n"
        "payload = json.dumps({'x': math.sqrt(9)})\n"
        "sleep = asyncio.sleep\n"
    )
    assert json.loads(ns["payload"]) == {"x": 3.0}
    assert callable(ns["sleep"])


def test_capability_reason_classifies_objects() -> None:
    """运行时校验是"看对象事实"，不是"看代码怎么写"：直接喂对象也要判对。"""
    import os

    assert capability_reason("system", os.system) is not None  # 危险模块里的可调用物
    assert capability_reason("os", os) is not None  # 危险模块本身
    assert capability_reason("system", "not-a-callable") is not None  # 换个模块再导出也拦

    import asyncio

    assert capability_reason("sleep", asyncio.sleep) is None  # 白名单能力照常放行
    assert capability_reason("gather", asyncio.gather) is None


# ── 插件 runner 同步生效（同一份守卫，按路径加载）──────────────────────────


def _run_plugin_runner(code: str) -> dict:
    payload = json.dumps({"plugin_name": "probe", "code": code, "input": {}})
    proc = subprocess.run(  # noqa: S603 — 被测对象就是 runner 子进程入口
        [sys.executable, str(_RUNNER)],
        input=payload.encode("utf-8"),
        capture_output=True,
        cwd=str(_ENGINE_ROOT),
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return json.loads(proc.stdout.decode("utf-8"))


def test_plugin_runner_blocks_reexport_escape() -> None:
    """插件 runner 走的是同一份守卫 ⇒ 逃逸探针在插件通道上同样被拒。"""
    result = _run_plugin_runner(
        "import asyncio\n\n"
        "def main(input):\n"
        "    return asyncio.base_events.os.system('echo pwned')\n"
    )
    assert result["success"] is False
    assert "blocked" in result["error"].lower()


def test_plugin_runner_reports_guard_version() -> None:
    """runner 侧版本闸：守卫 API 版本不达标必须 fail-closed，而不是照跑。"""
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("plugin_runner_probe", _RUNNER)
    assert spec is not None and spec.loader is not None
    runner = module_from_spec(spec)
    spec.loader.exec_module(runner)

    guard = runner._load_code_guard()
    assert getattr(guard, "GUARD_API_VERSION", 0) >= runner._GUARD_MIN_VERSION
    assert GUARD_API_VERSION >= runner._GUARD_MIN_VERSION

    with pytest.raises(RuntimeError, match="code_guard API"):
        runner._assert_guard_capable(object())  # 没有版本号 ⇒ fail-closed
