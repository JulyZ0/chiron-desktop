"""代码层静态/运行时安全守卫（叶子模块 — 不得 import 任何 app 内模块）。

本模块被两处复用，是单一事实来源，勿在别处复制守卫逻辑：
- app/tools/run_code.py（进程内代码执行沙箱）
- plugin_runner.py（插件 subprocess 沙箱，按文件路径加载本模块以避开
  app.tools.__init__ 的重量级 import 链）

说明：这是 A 层（代码层）约束——提高绕过门槛；完整隔离仍需部署侧 B 层
（容器/低权限/subprocess，见 plugin_runner.py）。
"""

from __future__ import annotations

import ast
import builtins as _b
import types as _types
from collections.abc import Callable
from typing import Any, NoReturn

#: 守卫能力版本。`plugin_runner.py` 按文件路径加载本模块时校验它 —— 旧副本（缺少
#: 属性链/再导出拦截）必须 **fail-closed** 拒绝执行插件，而不是静默用弱守卫跑。
GUARD_API_VERSION = 2

# ── 静态安全检查：禁止沙箱代码直接访问宿主 ─────────────────────────────
DANGEROUS_MODULES = {
    "os",
    "subprocess",
    "sys",
    "ctypes",
    "socket",
    "importlib",
    "pathlib",
    "shutil",
    "tempfile",
    "glob",
    "atexit",
    "signal",
    "multiprocessing",
    "pickle",
    "marshal",
    "dbm",
    "shelve",  # 防止序列化攻击
    "asyncio.tasks",
    # asyncio 是**白名单模块**（模型代码常要 `await asyncio.sleep()`），但它夹带一个
    # 能起进程的子模块：`asyncio.subprocess.create_subprocess_exec/shell` ⇒ **任意命令执行**，
    # 绕开工具白名单与 exec 审计。此前只拦 `asyncio.tasks`，而这段比较只看**根段**（见
    # `_module_blocked`），那条点分条目其实从未生效 —— 这个洞是补 `_module_blocked` 时一并修掉的。
    "asyncio.subprocess",
    "http.client",
    "urllib",
    "requests",  # 防止网络请求
    "telnetlib",
    "ftplib",
    "smtplib",
    "imaplib",
    "poplib",  # 防止协议攻击
    # 再导出（re-export）类逃逸的落地点：白名单模块通过 `mod.os` / `mod.sys` 之类的
    # 属性把宿主模块递出来。这些名字本身也不该被沙箱代码 import。
    "builtins",
    "gc",
    "inspect",
    "types",
    "_thread",
    "fcntl",
    "pty",
    "resource",
    "mmap",
    "runpy",
}

#: 属性链里出现即视为"取到了危险模块"的**段名**（再导出拦截用）。
#:
#: `asyncio.base_events.os.system(...)` 之所以能逃逸：`asyncio` 是白名单模块，
#: 但 `asyncio.base_events` 自己 `import os`，于是点分属性链的**中间段**就是 `os`。
#: 静态层只看 `Name.attr(...)` 两段（`asyncio.base_events` 的 value 已是 Attribute，
#: 不是 Name ⇒ 整条链被跳过）。运行时层把真模块直接交给沙箱代码 ⇒ 同样拿到 `os`。
DANGEROUS_MODULE_ROOTS = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "importlib",
        "ctypes",
        "shutil",
        "pathlib",
        "tempfile",
        "multiprocessing",
        "pickle",
        "marshal",
        "signal",
        "atexit",
    }
)

#: 属性链**末段**命中即拦的"能力名"（换个模块再导出也照样拦）。
BLOCKED_REEXPORT_NAMES = frozenset(
    {
        "system",
        "popen",
        "spawn",
        "spawnl",
        "spawnv",
        "spawnve",
        "execv",
        "execve",
        "execl",
        "fork",
        "forkpty",
        "posix_spawn",
        "__import__",
        "import_module",
        "load_module",
        "open_code",
    }
)

DANGEROUS_CALLS = {
    "open",
    "exec",
    "eval",
    "compile",
    "__import__",
    "input",
    "breakpoint",
}
# 注意：getattr/setattr 等反射函数不在静态层封杀 —— 静态层无法识别动态构造的
# 混淆调用，这类攻击由运行时守卫（_safe_builtins stub）兜底拦截（纵深防御）。

DANGEROUS_ATTR_PATHS = (
    "os.system",
    "os.popen",
    "os.startfile",
    "os.remove",
    "os.rename",
    "os.chdir",
    "os.mkdir",
    "os.makedirs",
    "sys.exit",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "socket.socket",
    "shutil.rmtree",
    "shutil.copy",
    "shutil.move",
    "pathlib.Path",
)

# 对象内省/逃逸链属性（S 修复）：堵死 __class__.__mro__[].__subclasses__()
# 一类访问宿主模块的经典沙箱逃逸。这些是对象属性访问,运行时 __builtins__
# stub 拦不到(不经 getattr),故在静态层直接封禁属性名。沙箱代码无合法需要。
BLOCKED_SUBCLASS_ATTRS = frozenset(
    {
        "__class__",
        "__subclasses__",
        "__base__",
        "__bases__",
        "__mro__",
        "__dict__",
        "__globals__",
        "__closure__",
        "__code__",
        "__getattribute__",
        "__build_class__",
    }
)


def _dotted_name(node: ast.AST) -> str | None:
    """把 `a.b.c` 形式的属性链还原成点分名；含下标/调用/复杂表达式时返回 None。

    存在理由：旧实现只认 `Name.attr`（两段），于是**任意长链**都被静默跳过 ——
    `asyncio.base_events.os.system(...)` 的 `func.value` 是 Attribute 而不是 Name，
    静态检查看不见它。
    """
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _attribute_chain_reason(node: ast.Attribute) -> str | None:
    """属性链级判定：链上哪些段说明"取到了危险能力"。

    首段是沙箱代码自己的变量/模块别名（无法静态判定来源），因此只看**其后**的段；
    但首段本身若就是被禁模块的别名，import 检查已经拦下了。
    """
    dotted = _dotted_name(node)
    if not dotted:
        return None
    parts = dotted.split(".")
    for segment in parts[1:]:
        if segment in DANGEROUS_MODULE_ROOTS:
            return (
                f"attribute path '{dotted}' reaches blocked module '{segment}' "
                "(re-export escape)"
            )
    if len(parts) > 1 and parts[-1] in BLOCKED_REEXPORT_NAMES:
        return f"attribute path '{dotted}' exposes blocked capability '{parts[-1]}'"
    if dotted in DANGEROUS_ATTR_PATHS:
        return f"call '{dotted}()' is not allowed — use tools.* instead"
    return None


def _module_blocked(name: str) -> bool:
    """按**完整点分名逐级前缀**判定模块是否被禁。

    为什么不能只看根段：`DANGEROUS_MODULES` 里有 `asyncio.tasks` / `http.client` 这类
    **点分条目**，而"取根段比较"（`name.split('.')[0] in DANGEROUS_MODULES`）永远匹配不到它们
    —— 于是 `import asyncio.subprocess` 这种写法**全部放行**（实测可拿到
    `create_subprocess_exec`，等于绕开沙箱起进程）。逐级前缀比较让这些条目真正生效，
    同时保留 `import os` / `import os.path` 都被拦的效果。
    """
    if not name:
        return False
    parts = name.split(".")
    return any(".".join(parts[: i + 1]) in DANGEROUS_MODULES for i in range(len(parts)))


def check_static(code: str) -> str | None:
    """AST 静态检查沙箱代码：命中危险模块导入/危险调用返回原因，否则 None。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # 语法错误由执行阶段结构化返回

    for node in ast.walk(tree):
        # import os / import pathlib / from X import Y
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _module_blocked(alias.name):
                    return f"module '{alias.name}' is not allowed — use tools.* instead"
        if isinstance(node, ast.ImportFrom):
            if _module_blocked(node.module or ""):
                return f"module '{node.module}' is not allowed — use tools.* instead"
            # `from asyncio import subprocess`：module 段是合法的 asyncio，危险在被导入的名字上
            for alias in node.names:
                combined = f"{node.module}.{alias.name}" if node.module else alias.name
                if _module_blocked(combined):
                    return f"name '{combined}' is not allowed — use tools.* instead"

        # open(...) / exec(...) / eval(...) / __import__(...) 等危险调用
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in DANGEROUS_CALLS:
                return f"call '{fn.id}()' is not allowed — use tools.* instead"
            # os.system / subprocess.run / sys.exit / socket.* 属性调用
            if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
                attr_path = f"{fn.value.id}.{fn.attr}"
                if attr_path in DANGEROUS_ATTR_PATHS:
                    return f"call '{attr_path}()' is not allowed — use tools.* instead"

        # 封禁对象内省/逃逸链属性(.__class__.__mro__[].__subclasses__() 等)
        if isinstance(node, ast.Attribute):
            if node.attr in BLOCKED_SUBCLASS_ATTRS:
                return f"attribute '{node.attr}' is not allowed (object introspection)"
            # 再导出逃逸：任意长度的属性链都要还原判定（`asyncio.base_events.os.system`）
            chain_reason = _attribute_chain_reason(node)
            if chain_reason:
                return chain_reason

    # 注：混淆访问(字符串拼接/'__builtins__' 索引等)不被静态层识别,由运行时
    # _safe_builtins stub 兜底;而 __class__/__subclasses__ 等对象属性访问不经
    # __builtins__ 拦截,故在静态层封禁(见上)。两层纵深防御。
    return None


# ── 运行时守卫（堵静态守卫绕过，如 __builtins__['ope'+'n'] 动态构造） ──
# 方案：替换 exec 命名空间的 builtins —— open/exec/eval 等 builtin 全被 stub（raise），
# __import__ 白名单化（仅安全标准库）。沙箱代码无法拿到 os/subprocess 模块，
# 动态构造 builtin 调用也被 stub 拦截。（settrace/monitoring 对 asyncio 协程与
# builtin 调用均不可靠，实测弃用。）
# S4 强化：移除 io/copy/operator/enum — 这些模块提供元编程链可触达 os/socket，
# 导致沙箱逃逸。模型代码不需要它们（文件 IO 由 tools 注入 namespace 提供）。
# asyncio 保留：模型代码常需 `await asyncio.sleep()`/`asyncio.gather()`；但它的危险子模块
# （`asyncio.subprocess`，能起进程）**必须显式禁用** —— 见 `DANGEROUS_MODULES` 里的条目与
# `_module_blocked`（静态 + 运行时两层都拦；早先只在注释里声称"由 DANGEROUS_ATTR_PATHS 拦截"，
# 而那里并没有对应条目，属**注释声称了不存在的防护**）。
SAFE_IMPORTS = frozenset(
    {
        "json",
        "math",
        "random",
        "datetime",
        "re",
        "collections",
        "itertools",
        "typing",
        "time",
        "functools",
        "decimal",
        "string",
        "asyncio",
    }
)
BLOCKED_BUILTINS = frozenset({"open", "exec", "eval", "compile", "input", "breakpoint"})
# 反射攻击函数
BLOCKED_REFLECTIVE = frozenset(
    {"getattr", "setattr", "delattr", "dir", "vars", "locals", "globals"}
)


def _make_blocked_builtin(name: str) -> Callable[..., Any]:
    def _blocked(*_args: Any, **_kwargs: Any) -> NoReturn:  # noqa: ANN002, ANN003 — 桩函数
        raise RuntimeError(f"blocked by runtime guard: {name}()")

    return _blocked


def capability_reason(name: str, value: Any) -> str | None:
    """**运行时能力校验**：判断"从白名单模块属性上取到的这个对象"是否越界。

    AST 层能拦住的只有**写死**的属性链；动态形态（`getattr` 被 stub 之外的花样、
    `vars()`/`__dict__` 已被拦，但 `import` 出来的对象本身仍可被逐层点开）需要运行时
    按**对象事实**再判一次 —— 这正是"AST + 运行时 capability 双重校验"的第二半：
    不看代码怎么写的，只看**实际拿到的对象**是不是危险模块 / 危险模块里的可调用物。

    Returns:
        拒绝原因；``None`` 表示该属性可以交出去。
    """
    if name in BLOCKED_REEXPORT_NAMES:
        return f"attribute '{name}' exposes a blocked capability"
    if isinstance(value, _types.ModuleType):
        module_name = getattr(value, "__name__", "") or ""
        root = module_name.split(".")[0]
        if root not in SAFE_IMPORTS or _module_blocked(module_name):
            return f"module '{module_name}' is not allowed (re-export of blocked module)"
        return None
    owner = getattr(value, "__module__", None)
    if isinstance(owner, str) and owner:
        # 只看**根名**：`asyncio.sleep` 的 __module__ 是 `asyncio.tasks`（点分条目本身
        # 被禁的是"import 这个模块"，不是"用它的函数"），按整名比较会把白名单能力一起封掉。
        if owner.split(".")[0] in DANGEROUS_MODULE_ROOTS:
            return f"object from blocked module '{owner}' is not allowed"
    return None


class _GuardedModule:
    """白名单模块的**受控代理**：每次属性访问都过一次 :func:`capability_reason`。

    为什么必须代理而不是直接交真模块：`asyncio.base_events.os` 里的 `os` 是**真模块
    对象**，交出去就等于把 `os.system` 一并交出去（子模块 / `__dict__` 都够不着守卫）。
    代理层让"取到危险对象"这个动作本身在运行时失败，且与 AST 层同一套判据（同一份
    白名单），所以两层不会各自漂移。
    """

    __slots__ = ("_guarded_target",)

    def __init__(self, module: _types.ModuleType) -> None:
        object.__setattr__(self, "_guarded_target", module)

    def __getattr__(self, name: str) -> Any:
        target = object.__getattribute__(self, "_guarded_target")
        value = getattr(target, name)  # 不存在时照常抛 AttributeError
        reason = capability_reason(name, value)
        if reason:
            raise RuntimeError(
                f"blocked by runtime guard: {target.__name__}.{name} — {reason}"
            )
        if isinstance(value, _types.ModuleType):
            return _GuardedModule(value)
        return value

    def __setattr__(self, name: str, value: Any) -> NoReturn:
        raise RuntimeError(f"blocked by runtime guard: cannot set attribute {name!r}")

    def __repr__(self) -> str:
        target = object.__getattribute__(self, "_guarded_target")
        return f"<guarded module {target.__name__!r}>"


def safe_builtins() -> dict[str, Any]:
    """构造受控 builtins：安全库白名单导入 + 危险 builtin 全部 stub。"""
    orig = vars(_b).copy()
    real_import = orig["__import__"]

    def _guarded_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:  # noqa: A002
        root = (name or "").split(".")[0]
        # 两层都要判：根在白名单里、**且**没有命中禁用的点分条目 —— 否则
        # `import asyncio.subprocess` 会因为根是合法的 asyncio 而一路放行（能起进程）。
        if root in SAFE_IMPORTS and not _module_blocked(name):
            module = real_import(name, globals, locals, fromlist, level)
            # 交出去的是**受控代理**而不是真模块：白名单模块自己 `import os` 之后，
            # `asyncio.base_events.os` 仍能取到真 `os`（再导出逃逸）。代理层用与 AST
            # 同一套判据在**取用那一刻**拒绝（见 capability_reason）。
            return _GuardedModule(module) if isinstance(module, _types.ModuleType) else module
        raise RuntimeError(f"blocked by runtime guard: import '{name}'")

    # 阻塞危险内置函数
    for name in BLOCKED_BUILTINS:
        orig[name] = _make_blocked_builtin(name)

    # 阻塞反射攻击函数
    for name in BLOCKED_REFLECTIVE:
        orig[name] = _make_blocked_builtin(name)

    # 替换 __import__ 为安全版本
    orig["__import__"] = _guarded_import

    # 移除潜在的危险属性访问
    orig.pop("__builtins__", None)  # 防止通过 __builtins__ 访问内置函数

    return orig
