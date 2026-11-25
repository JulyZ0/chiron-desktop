"""LSP 语言服务器的**命令配置**：只从环境变量读，不猜、不内置第三方命令。

## 为什么不做"自动探测"

写这类工具最容易走的两条歪路：

1. **内置一张表**（`python → ["pylsp"]`）。听着贴心，实际是"凭空的依赖"：
   这台机器没装 pylsp 就变成 `WinError 2`；装了 pyright 的用户永远用不上它；
   而仓库的依赖清单里并没有任何一个语言服务器（也不该有 —— 那是几 GB 的
   按语言可选工具链）。
2. **猜一个"差不多能跑"的命令**。比第 1 条更糟：猜中的命令会正常响应，只是
   回答的是**别的语言**（把 `clangd` 用于 C++ 猜成 C、把 `vscode-json-languageserver`
   用于 JSON5），于是输出了一个语法合法、语义全错的定义位置。

所以这里的口径是：**没有配置就明确报错**，并在错误里写清"该设哪个变量、
值长什么样"。这与仓库其它第三方进程的口径一致（MCP server 也必须显式配置，
见 `app/mcp/client.py` 的 "user-configured commands require the allowlist"）。

## 变量命名

``CHIRON_LSP_<LANG>``，`<LANG>` 是语言键的大写形式（`-` / `.` 折成 `_`）：

* ``CHIRON_LSP_PYTHON=pylsp --stdio``
* ``CHIRON_LSP_TYPESCRIPT="C:\\node\\bin\\typescript-language-server.cmd" --stdio``
* ``CHIRON_LSP_C_CPP=clangd``

值按 shell 词法拆分（不做任何变量展开），因此带空格的路径必须加引号 ——
Windows 上 `shlex` 的 posix 模式会把反斜杠当转义符吃掉（`C:\\a\\b` → `C:ab`），
所以 Windows 走非 posix 模式。

## 超时与升级

* ``CHIRON_LSP_TIMEOUT``：单条查询超时（秒，默认 20）。超时不是"等久一点"，
  而是**杀进程**（见 `session.py`）—— 一个不回应查询的语言服务器已经坏了。
* ``CHIRON_LSP_KILL_GRACE``：SIGTERM 到 SIGKILL 的宽限（秒，默认 2）。
* ``CHIRON_LSP_<LANG>_ENV``：额外透传给语言服务器进程的环境变量，
  `KEY=VALUE;KEY2=VALUE2` 形态。语言服务器是"沙箱清理后的环境"里跑的，
  需要 `JAVA_HOME` / `GOPATH` 之类时必须显式声明。
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

#: 环境变量前缀：`CHIRON_LSP_<LANG>` 给出该语言的启动命令
ENV_PREFIX = "CHIRON_LSP_"

#: 单条查询的默认超时（秒）
DEFAULT_REQUEST_TIMEOUT = 20.0

#: SIGTERM → SIGKILL 的默认宽限（秒）
DEFAULT_KILL_GRACE = 2.0

#: 额外环境变量后缀：`CHIRON_LSP_<LANG>_ENV`
ENV_SUFFIX = "_ENV"

#: 合法的语言键（小写字母/数字 + `_ . + -`）。刻意不做"允许任意字符串"：
#: 语言键会进环境变量名与错误信息，含空格/引号的键只会制造不可复现的配置问题。
_LANGUAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_.+-]*$")

#: 扩展名 → 语言键。只用于**推断语言键**（不是推断命令）：
#: 推断错了顶多报"该语言没配置"，不会静默跑错服务器。
LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".lua": "lua",
    ".swift": "swift",
    ".c": "c_cpp",
    ".h": "c_cpp",
    ".cc": "c_cpp",
    ".cpp": "c_cpp",
    ".hpp": "c_cpp",
    ".json": "json",
    ".jsonc": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".css": "css",
    ".html": "html",
}


class LspConfigError(RuntimeError):
    """配置缺失/不合法。消息面向**用户**（会原样出现在工具错误里）。"""


@dataclass(frozen=True)
class LspServerConfig:
    """一个语言服务器的启动配置（不可变：会话之间共享，改它就是新配置）。"""

    language: str
    command: tuple[str, ...]
    extra_env: Mapping[str, str] = field(default_factory=dict)
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    kill_grace: float = DEFAULT_KILL_GRACE


def normalize_language(language: str) -> str:
    """归一并校验语言键（大小写、分隔符）。"""
    key = (language or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not key:
        raise LspConfigError("language 为空：请传入语言键（如 python / typescript）或指定可识别的文件扩展名")
    if not _LANGUAGE_RE.match(key):
        raise LspConfigError(
            f"language 不合法：{language!r}。只允许小写字母/数字与 `_ . + -`，"
            "例如 python、typescript、c_cpp"
        )
    return key


def env_var_name(language: str) -> str:
    """该语言的启动命令变量名：`CHIRON_LSP_<LANG>`。"""
    return f"{ENV_PREFIX}{normalize_language(language).upper()}"


def language_for_path(path: str | Path) -> str:
    """按扩展名推断语言键；不认识就报错（不猜）。"""
    suffix = Path(path).suffix.lower()
    language = LANGUAGE_BY_SUFFIX.get(suffix)
    if language is None:
        raise LspConfigError(
            f"无法从扩展名推断语言：{Path(path).name!r}。请显式传 `language` "
            f"（已识别：{', '.join(sorted(set(LANGUAGE_BY_SUFFIX.values())))}）"
        )
    return language


def split_command(raw: str) -> tuple[str, ...]:
    """把 `CHIRON_LSP_<LANG>` 的值拆成 argv。

    * Windows 走非 posix 模式：posix 模式会把 `C:\\node\\x.cmd` 的反斜杠当转义符
      吃掉，变成 `C:nodex.cmd`（"命令找不到"，而配置看起来完全正确）。
    * 非 posix 模式保留引号，因此这里再逐项剥一层成对引号。
    """
    text = (raw or "").strip()
    if not text:
        raise LspConfigError("语言服务器命令为空")
    if os.name == "nt":
        tokens = [token for token in shlex.split(text, posix=False) if token]
        parts = [_strip_quotes(token) for token in tokens]
    else:
        parts = shlex.split(text, posix=True)
    parts = [part for part in parts if part]
    if not parts:
        raise LspConfigError(f"语言服务器命令解析后为空：{raw!r}")
    return tuple(parts)


def _strip_quotes(token: str) -> str:
    """去掉 posix=False 模式下被保留的成对引号。"""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def parse_env_pairs(raw: str, *, var_name: str = "") -> dict[str, str]:
    """解析 `KEY=VALUE;KEY2=VALUE2`（值里可以含 `=`，分号不分隔引号内的内容）。"""
    pairs: dict[str, str] = {}
    for chunk in (raw or "").split(";"):
        item = chunk.strip()
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise LspConfigError(f"{var_name or '额外环境变量'} 格式应为 KEY=VALUE，实得 {item!r}")
        pairs[key.strip()] = value
    return pairs


def _read_float(environ: Mapping[str, str], name: str, default: float, *, minimum: float) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise LspConfigError(f"{name} 必须是数字（秒），实得 {raw!r}") from None
    if value < minimum:
        raise LspConfigError(f"{name} 必须 >= {minimum:g}，实得 {value:g}")
    return value


def request_timeout(environ: Mapping[str, str] | None = None) -> float:
    """`CHIRON_LSP_TIMEOUT`（默认 20 秒）。"""
    env = os.environ if environ is None else environ
    return _read_float(env, f"{ENV_PREFIX}TIMEOUT", DEFAULT_REQUEST_TIMEOUT, minimum=0.1)


def kill_grace(environ: Mapping[str, str] | None = None) -> float:
    """`CHIRON_LSP_KILL_GRACE`（默认 2 秒）。"""
    env = os.environ if environ is None else environ
    return _read_float(env, f"{ENV_PREFIX}KILL_GRACE", DEFAULT_KILL_GRACE, minimum=0.01)


def load_server_config(
    language: str,
    environ: Mapping[str, str] | None = None,
) -> LspServerConfig:
    """按语言读配置；**没配置就报错**，消息里给出变量名与示例值。"""
    env = os.environ if environ is None else environ
    key = normalize_language(language)
    var = env_var_name(key)
    raw = (env.get(var) or "").strip()
    if not raw:
        raise LspConfigError(
            f"未配置语言服务器（language={key!r}）：请设置环境变量 {var}=<启动命令>，"
            f"例如 {var}=pylsp --stdio。本工具不内置、也不猜测任何第三方语言服务器的命令。"
        )
    command = split_command(raw)
    extra_env = parse_env_pairs(env.get(f"{var}{ENV_SUFFIX}", ""), var_name=f"{var}{ENV_SUFFIX}")
    return LspServerConfig(
        language=key,
        command=command,
        extra_env=extra_env,
        request_timeout=request_timeout(env),
        kill_grace=kill_grace(env),
    )
