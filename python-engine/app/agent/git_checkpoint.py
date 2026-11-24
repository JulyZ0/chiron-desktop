"""git_checkpoint — 代码检查点原语（P0.5：snapshot / diff / restore）。

**这不是模型可调用的工具**：检查点引擎只由回合钩子与回滚 API 驱动。
模型若能自行回滚，就能"掩盖"自己的错误。

本模块是**纯函数层**：不连数据库、不读全局上下文、不碰 HTTP、不依赖沙箱单例。
所有效果都只发生在传入的 ``root`` 上，因此可以在临时目录里完整单测。
上层（P1 的 ``project_checkpoints`` 表、P2 的回滚 API）负责把结果落库与对外暴露。

设计要点（每一条都在下面的实现里直接体现）：

* 快照：独立 index + pathspec 排除 + 前缀裁剪（monorepo 子目录）
* 回滚：三分类差异 + 默认不删
* 单文件/单回合：同一个原语
* 密钥：``add`` 的同一次调用里排除，而不是 ``add`` 后 ``rm --cached``
* 盲区：空目录、gitlink、symlink、大小写改名

几条**用实测换来的**硬约束（都是踩过的坑，不要"简化"掉）：

1. ``GIT_INDEX_FILE`` 必须放在工作区之外。放在里面会被 ``add`` 收进快照，
   于是 ``write-tree`` 每次都不一样，"内容相同则 tree 相同"这个去重前提直接失效。
2. 读回文件用 ``git cat-file --filters``，**不是** ``git show``，也**不是**
   ``git checkout``。``show`` 会把 CRLF 静默改成 LF；``checkout`` 会污染用户暂存区。
3. 解析任何 git 路径输出都用 ``-z``。默认 ``core.quotepath=true`` 会把中文路径
   转成 ``"\\344\\270\\255.py"``，按行解析直接拿到错误文件名。
4. 恢复 ``added`` 类文件前必须 ``mkdir -p`` 父目录，否则报
   ``No such file or directory`` —— 而且**退出码可能仍是 0**，不能只看返回值。
5. 动手前必须用 :func:`resolve_layout` 定出"项目根在仓库里的前缀"（``--show-prefix``）。
   git 会向上查找父仓库：项目根若是某个大仓库的子目录，``read-tree HEAD`` /
   ``write-tree`` 默认是**仓库全局**口径 —— 会把父仓库其它文件一起写进快照，
   而上报的路径口径却是子目录。monorepo 模式下必须先摘掉独立索引里**前缀之外**
   的条目（实测：光靠 pathspec 排除管不到 ``read-tree`` 已经带进来的条目），
   再 ``write-tree``；写完后还要用 ``--full-name`` 复核一遍，见 :func:`snapshot`。

D4（未跟踪文件）：默认**不进 git 对象库**，而进本模块自带的按 sha256 分片的
内容寻址存储（``UntrackedStore``）。理由：agent 新建的文件数量不可控，塞进用户的
``.git/objects`` 会污染仓库体积、拖慢用户的 ``git gc``；而分片 CAS 天然去重、
可按快照引用计数清理，也便于后续放进独立表（路径 → 摘要）。
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Literal, ParamSpec, TypeVar, cast

# ---------------------------------------------------------------------------
# 排除与保护
# ---------------------------------------------------------------------------

#: 密钥类。剔除后**必须计数并告知用户**：
#: 用户有权知道自己的密钥没有被保护进检查点。
SECRET_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keystore",
    "*.jks",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".ssh/**",
    ".aws/**",
    ".gnupg/**",
    ".npmrc",
    ".netrc",
    "credentials*",
    "*.kdbx",
)

#: 噪声类。剔除后**静默**：提了只会让非技术用户困惑。
#: 这类路径同时进**保护列表**，确保它们不会出现在回滚的 ``removed`` 清单里 ——
#: 否则会诱导用户删掉自己的 ``node_modules``。
NOISE_GLOBS: tuple[str, ...] = (
    ".git/**",
    "node_modules/**",
    "__pycache__/**",
    "*.pyc",
    "*.pyo",
    ".venv/**",
    "venv/**",
    "dist/**",
    "build/**",
    "target/**",
    "out/**",
    ".idea/**",
    ".vscode/**",
    ".pytest_cache/**",
    ".mypy_cache/**",
    ".ruff_cache/**",
    ".next/**",
    ".nuxt/**",
    ".turbo/**",
)

#: 回滚时**永不删除**的路径。剔除类 + 用户 VCS 元数据 —— 删了 ``.git`` 是灾难级。
PROTECTED_GLOBS: tuple[str, ...] = SECRET_GLOBS + NOISE_GLOBS + (".git", ".gitmodules")


def _is_excluded(rel: str, globs: Sequence[str]) -> bool:
    """判断工作区相对路径是否命中排除/保护列表。

    三套匹配缺一不可（``fnmatch`` 的一行正则做不到全部）：

    * 整段匹配：``node_modules/**`` 命中根层目录；
    * 目录型 glob 的任意层级匹配：``a/node_modules/x`` 也要命中；
    * 纯文件名 glob 的 basename 匹配：``sub/.env`` 要命中 ``.env``。

    **不要**用 ``rel.lstrip("./")`` 去前缀：``str.lstrip`` 按**字符集**剥离，
    会把 ``.env`` 变成 ``env`` —— 于是密钥排除在 Python 侧整个失效。
    """
    rel = rel.replace(os.sep, "/")
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    base = rel.rsplit("/", 1)[-1]
    for pattern in globs:
        if fnmatch.fnmatch(rel, pattern):
            return True
        if pattern.endswith("/**"):
            stem = pattern[:-3]
            if fnmatch.fnmatch(rel, f"{stem}/**") or f"/{stem}/" in f"/{rel}/":
                return True
        elif "/" not in pattern and fnmatch.fnmatch(base, pattern):
            return True
    return False


def _exclude_spec(globs: Sequence[str]) -> list[str]:
    """把 glob 列表翻成 ``git add`` 的 pathspec 排除项。

    **必须是同一一次 ``git add`` 调用里的排除**。实测对照：
    ``add -A`` 后 ``git rm --cached .env`` 的树同样干净，但密钥 blob 已经写进
    ``.git/objects``，``git fsck --lost-found`` 能把明文捞出来（``LEAKED_IN_OBJECTS``）；
    而 pathspec 排除是 ``SAFE_NOT_WRITTEN`` —— blob 根本没生成。
    """
    spec: list[str] = []
    for pattern in globs:
        spec.append(f":(exclude,glob){pattern}")
        # git 的 glob 里 ``**/x`` 不匹配根层的 ``x``，所以两种都加。
        if not pattern.startswith("**/"):
            spec.append(f":(exclude,glob)**/{pattern}")
    return spec


# ---------------------------------------------------------------------------
# 资源上限（D7）
# ---------------------------------------------------------------------------


#: D7 下限：单次检查点预算**不得低于** 4 GB（用户明确要求）。
#:
#: 这条下限高于"实测增量"一个数量级以上（实测每次检查点约 34 KB），
#: 所以它不是为了省钱，而是为了**永不因为预算而丢失检查点**：
#: 用户花钱买的磁盘，不该由我们替他省。
MIN_CHECKPOINT_BYTES: int = 4 * 1024 * 1024 * 1024


_BYTE_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}


def _as_bytes(value: object) -> int | None:
    """把设置值解析成字节数；无法解释就返回 ``None``（由调用方回退默认）。

    设置来自 JSON 时是整数，来自配置文件/表单时可能是 ``"4gb"``、``"8192 MB"``。
    两种都认 —— 否则用户填了「4GB」却被静默忽略，是最难解释的一类失效。

    ``bool`` 是 ``int`` 的子类，必须**先**排除，否则 ``True`` 会被当成 1 字节。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip().lower().replace(" ", "").replace("_", "")
    if not text:
        return None
    number, unit = text, ""
    # 按后缀长度倒序，否则 "kb" 会被 "b" 抢先吃掉。
    for suffix in sorted(_BYTE_UNITS, key=len, reverse=True):
        if suffix and text.endswith(suffix):
            number, unit = text[: -len(suffix)], suffix
            break
    try:
        amount = float(number)
    except ValueError:
        return None
    if amount < 0 or amount != amount:  # NaN
        return None
    return int(amount * _BYTE_UNITS[unit])


def _as_bool(value: object) -> bool | None:
    """从设置值里取布尔；无法解释就返回 ``None``。

    设置可能来自 JSON（``true``）、环境变量或表单（``"1"``/``"true"``），
    两种都要认。**非法值一律回退到调用方的默认**，而 D5 的默认是"禁止"——
    解析失败时落在安全侧。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    return None


@dataclass(frozen=True)
class Limits:
    """单文件与单次检查点的体积上限。

    默认值来自实测（``$TMPDIR/d7c``，1000 个文件的仓库）：

    * 首快照（含全部内容）1.4 MB；
    * 之后每次"改 5 个文件"的检查点增量约 **34 KB**；
    * 20 次检查点累计增量 692 KB。

    即"每次检查点约为仓库体积的 2.4%"。按会话 50 个检查点估算约 1.7 MB，
    全局保留 200 个约 7 MB —— 对用户 ``.git`` 是可接受的量级，
    因此默认上限取**远大于**实测的值，只用于挡住异常情况（巨型二进制、
    误提交的模型权重），而不是日常约束。

    **可配置**（D7）：用户可通过 ``resolve()`` 传入自己的设置覆盖；
    但单次预算存在硬下限 ``MIN_CHECKPOINT_BYTES``（4 GB），低于它的配置会被抬到 4 GB。
    """

    #: 单文件上限。超过则跳过并计数（不静默截断 —— 半截文件比不存更糟）。
    max_file_bytes: int = 64 * 1024 * 1024
    #: 单次检查点总量上限。**不低于** ``MIN_CHECKPOINT_BYTES``。
    max_checkpoint_bytes: int = MIN_CHECKPOINT_BYTES

    @classmethod
    def resolve(
        cls,
        settings: Mapping[str, object] | None = None,
        *,
        repo_objects_bytes: int = 0,
    ) -> Limits:
        """把用户设置解析成 ``Limits``；缺失或非法项回退到默认。

        配置键：``max_file_bytes``、``max_checkpoint_bytes``。
        ``max_checkpoint_bytes`` 无论用户写多少，都不会低于 4 GB ——
        这是 D7 的硬下限，不是建议值。
        """
        settings = settings or {}
        adaptive = adaptive_limits(
            repo_objects_bytes, override=_as_bytes(settings.get("max_checkpoint_bytes"))
        )
        file_limit = _as_bytes(settings.get("max_file_bytes"))
        if file_limit is None or file_limit <= 0:
            return adaptive
        return Limits(
            max_file_bytes=file_limit,
            max_checkpoint_bytes=adaptive.max_checkpoint_bytes,
        )


def adaptive_limits(repo_objects_bytes: int = 0, *, override: int | None = None) -> Limits:
    """按仓库现有对象体积放大预算（D7 的"自适应"）。

    规则：取 ``仓库对象体积 × 10``，但**不低于 4 GB**（``MIN_CHECKPOINT_BYTES``）。
    用户显式配置时用 ``override`` 替换自适应部分，同样受 4 GB 下限约束。

    为什么不设上界：上界只会让"用户明确要求的大预算"被静默砍掉。
    真正该防的是单文件失控（``max_file_bytes``）与磁盘不足，而不是总量。
    """
    if override is not None and override > 0:
        budget = max(MIN_CHECKPOINT_BYTES, override)
    else:
        budget = max(MIN_CHECKPOINT_BYTES, int(repo_objects_bytes) * 10)
    return Limits(max_checkpoint_bytes=budget)


# ---------------------------------------------------------------------------
# git 调用
# ---------------------------------------------------------------------------


class GitError(RuntimeError):
    """git 子命令失败（非零退出）。"""


#: 显式指定 git 可执行文件的设置项（值为绝对路径）。
#:
#: 打包运行时（Electron 主进程拉起的引擎）PATH 往往只有系统默认几项，用户自己装
#: 在别处的 git 会找不到 —— 症状是"时间线一直空白"，而原因埋在子进程异常里
#: （``[WinError 2]`` 对用户没有任何行动价值）。因此允许多一条显式路径。
GIT_BIN_ENV = "CHIRON_GIT_BIN"


def git_bin() -> str:
    """git 可执行文件名/路径：``CHIRON_GIT_BIN`` 优先，否则用 PATH 里的 ``git``。"""
    return (os.environ.get(GIT_BIN_ENV) or "").strip() or "git"


_GIT_INDEX_AND_IDENTITY_ENV = frozenset(
    {
        "GIT_INDEX_FILE",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    }
)


@dataclass(frozen=True)
class _GitGuard:
    top: Path
    overrides: tuple[tuple[str, str], ...]


_ACTIVE_GIT_GUARD: ContextVar[_GitGuard | None] = ContextVar("chiron_git_guard", default=None)
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _git_env(index_file: Path | None = None) -> dict[str, str]:
    """Build the shared minimal Git environment plus an isolated index and fixed identity."""
    from app.tools.git_tools import _git_environment

    env = _git_environment()
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    env.update(
        {
            "GIT_AUTHOR_NAME": "Chiron",
            "GIT_AUTHOR_EMAIL": "chiron@local",
            "GIT_COMMITTER_NAME": "Chiron",
            "GIT_COMMITTER_EMAIL": "chiron@local",
        }
    )
    return env


def _git_argv(
    root: Path,
    *args: str,
    overrides: tuple[tuple[str, str], ...] = (),
) -> list[str]:
    from app.tools.git_tools import _GIT_SAFE_CONFIG

    cmd = [git_bin(), "--no-pager", "-C", str(root)]
    # Keep internal/checkpoint calls aligned with the device gateway while the
    # shared tool table is reused for all other settings.
    safe_config = (*_GIT_SAFE_CONFIG, ("core.alternateRefsCommand", ""))
    for key, value in (*safe_config, *overrides):
        cmd.extend(("-c", f"{key}={value}"))
    cmd.extend(args)
    return cmd


def _run_git_unchecked(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    timeout: int = 120,
    stdin: bytes | None = None,
    overrides: tuple[tuple[str, str], ...] = (),
) -> subprocess.CompletedProcess[bytes]:
    exe = git_bin()
    cmd = _git_argv(root, *args, overrides=overrides)
    child_env = _git_env()
    if env is not None:
        for key, value in env.items():
            if key in _GIT_INDEX_AND_IDENTITY_ENV:
                child_env[key] = str(value)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            env=child_env,
            timeout=timeout,
            creationflags=creationflags,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError(
            f"未找到 git（git executable not found）: {exe!r}；"
            f"检查点已跳过。请安装 git，或设置环境变量 {GIT_BIN_ENV} "
            f"指向 git 可执行文件的绝对路径。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(
            f"git {' '.join(args)} timed out after {timeout}s "
            f"(repository too large or locked by another process)"
        ) from exc


@contextmanager
def _git_guard(root: Path) -> Iterator[None]:
    """Share one fail-closed configuration probe across a checkpoint operation."""
    active = _ACTIVE_GIT_GUARD.get()
    if active is not None and _same_dir(active.top, root):
        yield
        return

    from app.tools.git_tools import _config_overrides

    probe = _run_git_unchecked(root, "config", "--includes", "--null", "--list", timeout=30)
    if probe.returncode != 0:
        detail = probe.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"git repository configuration probe failed: {detail or f'exit {probe.returncode}'}")
    try:
        overrides = _config_overrides(probe.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise GitError(f"git repository configuration probe failed: {exc}") from exc

    top_probe = _run_git_unchecked(root, "rev-parse", "--show-toplevel", timeout=30, overrides=overrides)
    top = Path(top_probe.stdout.decode("utf-8", errors="replace").strip()) if top_probe.returncode == 0 else root
    if not top.is_absolute():
        top = root
    token = _ACTIVE_GIT_GUARD.set(_GitGuard(top=top, overrides=overrides))
    try:
        yield
    finally:
        _ACTIVE_GIT_GUARD.reset(token)


def _guarded_git(func: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(func)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        raw_root = args[0] if args else kwargs["root"]
        with _git_guard(Path(cast("Path", raw_root))):
            return func(*args, **kwargs)

    return wrapped


def _run_git(
    root: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    timeout: int = 120,
    stdin: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run Git with raw byte I/O after the shared repository configuration guard."""
    active = _ACTIVE_GIT_GUARD.get()
    if active is None or not _same_dir(active.top, root):
        with _git_guard(root):
            return _run_git(root, *args, env=env, timeout=timeout, stdin=stdin)
    return _run_git_unchecked(
        root,
        *args,
        env=env,
        timeout=timeout,
        stdin=stdin,
        overrides=active.overrides,
    )


def _must(proc: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip()
        raise GitError(f"{what} failed (exit={proc.returncode}): {detail}")
    return proc.stdout


def _split_z(raw: bytes) -> list[str]:
    """按 NUL 切分 git 的输出。**唯一**可靠的解析方式。"""
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _same_dir(a: Path, b: Path) -> bool:
    """两个路径是否指同一个目录（跟随符号链接，Windows 大小写不敏感）。

    ``rev-parse --show-toplevel`` 输出的是 git 自己的路径风格（Windows 上是
    ``C:/Users/...`` 正斜杠），与 ``Path`` 的 ``\\`` 写法不能直接比较字符串；
    而 macOS 的 ``/var`` → ``/private/var``、Windows 的大小写差异都会让
    "同一个目录"被判成不同。因此先 ``realpath`` 再 ``normcase``。
    """
    try:
        left = os.path.normcase(os.path.realpath(a))
        right = os.path.normcase(os.path.realpath(b))
    except OSError:
        return False
    return left == right


@dataclass(frozen=True)
class RepoLayout:
    """项目根与它所在 git 仓库的关系：``root`` 是仓库顶层，还是它下面某个前缀。

    ``prefix`` 是**POSIX 风格、带结尾斜杠**的仓库内前缀（``packages/foo/``）；
    项目根就是仓库顶层时为空串。空串与非空串的差别贯穿整个模块：

    * 快照：非空时独立索引必须先裁到前缀内（:func:`_restrict_index_to_prefix`）；
    * 路径口径：``Snapshot.tree_paths`` / ``DiffPlan`` / ``restore`` 一律是
      **项目根口径**（``src/a.py``），而不是仓库根口径（``packages/foo/src/a.py``），
      因此所有 git 命令都带 ``--relative`` / 相对 cwd 的路径；
    * commit / ref 仍然写进**父仓库**的对象库与 ref 命名空间 —— 这是既定设计：
      用户的项目本来就在那个仓库里，把检查点放到别处会让 ``git gc`` 认不出它们。

    为什么不再"拒绝子目录项目"：monorepo 是常见布局，把项目根指向子目录是用户
    的正当选择。拒绝只会让这类项目完全没有还原点；正确做法是**限定快照范围**。
    """

    #: 项目根（调用方传入的 ``root``，未解析符号链接）。
    root: Path
    #: 仓库工作区顶层（``rev-parse --show-toplevel`` 的输出）。
    top: Path
    #: 项目根相对仓库顶层的前缀，POSIX 分隔、带结尾斜杠；顶层项目为空串。
    prefix: str = ""

    @property
    def nested(self) -> bool:
        """项目根是不是仓库里的子目录。"""
        return bool(self.prefix)

    def contains(self, repo_path: str) -> bool:
        """仓库根口径的路径是否落在项目根子树内（索引条目都是文件，不必判目录）。"""
        if not self.prefix:
            return True
        return repo_path.replace(os.sep, "/").startswith(self.prefix)

    def relative(self, repo_path: str) -> str:
        """仓库根口径 → 项目根口径；前缀之外的原样返回（调用方负责丢弃）。"""
        if not self.prefix:
            return repo_path
        text = repo_path.replace(os.sep, "/")
        return text[len(self.prefix):] if text.startswith(self.prefix) else text


@_guarded_git
def resolve_layout(root: Path) -> RepoLayout:
    """解析 ``root`` 所在的仓库布局；不是仓库时抛可行动的 ``GitError``。

    这是原来的 ``verify_repo_root``：**不再拒绝**"项目根嵌在父仓库里"，
    而是把前缀解析出来交给快照/回滚使用。仍会失败（并给出行动项）的只有两种：

    * 根本不在任何仓库里 —— 提示先 ``git init``；
    * 能进仓库、却拿不到可用前缀（``--show-prefix`` 失败或为空而顶层又不同）——
      此时无法确定快照边界，宁可明确失败也不生成"看起来成功、实际是父仓库"的快照。
    """
    root = Path(root)
    proc = _run_git(root, "rev-parse", "--show-toplevel")
    detail = proc.stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise GitError(
            f"not a git repository: {root}"
            + (f" ({detail})" if detail else "")
            + "；检查点需要版本控制，请先在该目录执行 git init。"
        )
    top_raw = proc.stdout.decode("utf-8", errors="replace").strip()
    if not top_raw:
        # 正常仓库不会走到这里；保底也拒绝，避免"空值当成匹配"。
        raise GitError(f"git rev-parse --show-toplevel returned nothing for {root}")
    top = Path(top_raw)
    if _same_dir(top, root):
        return RepoLayout(root=root, top=top, prefix="")

    # 子目录（monorepo）：前缀决定快照边界，拿不到就只能失败。
    proc = _run_git(root, "rev-parse", "--show-prefix")
    prefix = proc.stdout.decode("utf-8", errors="replace").strip().replace("\\", "/")
    if proc.returncode != 0 or not prefix:
        raise GitError(
            f"cannot determine the project prefix inside the git repository: {root} "
            f"(repository top-level: {top_raw})；检查点无法确定快照边界，"
            "请确认项目目录位于仓库工作区内（不要是符号链接跳出去的目录），"
            "或在项目根单独 git init。"
        )
    if not prefix.endswith("/"):
        prefix += "/"
    return RepoLayout(root=root, top=top, prefix=prefix)


def verify_repo_root(root: Path) -> RepoLayout:
    """旧名保留：校验 ``root`` 在一个 git 仓库里，并返回它的 :class:`RepoLayout`。

    语义已放宽 —— 子目录项目**不再**被拒绝（见 :class:`RepoLayout` 的说明）。
    调用方拿返回值就能知道要不要走前缀裁剪；只关心"是不是仓库"的调用方可以忽略它。
    """
    return resolve_layout(root)


def _repo_objects_bytes(root: Path) -> int:
    """仓库对象库体积（用于 D7 的自适应预算）。读不到就当 0。"""
    out = _run_git(root, "count-objects", "-v")
    if out.returncode != 0:
        return 0
    for line in out.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("size:"):
            try:
                return int(line.split(":", 1)[1].strip()) * 1024
            except ValueError:
                return 0
    return 0


# ---------------------------------------------------------------------------
# 未跟踪文件的哈希分片存储（D4）
# ---------------------------------------------------------------------------


class UntrackedStore:
    """按内容寻址的未跟踪文件存储：``<root>/<sha256[:2]>/<sha256[2:]>``。

    为什么两字符分片：单目录放十万个文件会让 ``ls``/杀毒/备份都变慢；
    两字符 = 最多 256 个子目录，路径深度只有两层。

    **分布实测**（400 条真实 sha256 的前缀统计）：201 个不同前缀，空桶 55，
    平均 1.56 个/桶，最大的桶 6 个 —— 与随机分布的期望一致。也就是说单目录
    文件数约为总量的 1/256，十万个未跟踪文件时最深目录约 400 个，可以接受。
    （这是**前缀分布**的实测，不是磁盘占用；体积取决于内容本身，与分片无关。）

    内容寻址带来三个免费的性质：同一文件在多个检查点里**只存一份**；
    写盘前天然去重；摘要本身就是完整性校验（读回时比对，损坏立刻发现）。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, digest: str) -> Path:
        return self.root / digest[:2] / digest[2:]

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if target.exists():
            return digest  # 已有同内容 → 不重写（去重的实际落点）
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再 replace：中途崩溃不会留下半截 blob 被后续快照误用。
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return digest

    def get(self, digest: str) -> bytes:
        data = self._path(digest).read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            # 内容损坏：宁可报错也不要静默恢复出一份错文件。
            raise GitError(f"untracked object corrupt: expected {digest}, got {actual}")
        return data

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

#: 未跟踪文件的存放策略。
#: * ``store``（D4 默认）：不进 git 对象库，进上面的分片 CAS；
#: * ``tree``：并入 git tree（``add -A``），实现简单但会污染用户 ``.git/objects``。
UntrackedMode = Literal["store", "tree"]


@dataclass(frozen=True)
class Snapshot:
    """一次快照的结果。

    ``tree`` 只覆盖**已跟踪**文件（``store`` 模式下）；未跟踪文件在 ``untracked``
    里，``path -> sha256``。两者合起来才是完整的工作区状态。
    """

    tree: str
    untracked: Mapping[str, str] = field(default_factory=dict)
    #: git 树里实际存在的路径（相对 ``root``，POSIX 分隔）。
    tree_paths: frozenset[str] = frozenset()
    #: tree 里 filemode 不是 ``100644``/``100755`` 的条目（gitlink / tree）。
    non_regular: frozenset[str] = frozenset()
    skipped_secrets: int = 0
    skipped_oversized: int = 0
    mode: UntrackedMode = "store"

    def paths(self) -> frozenset[str]:
        """完整路径集（树内 + CAS 内）。"""
        return frozenset(self.tree_paths) | frozenset(self.untracked)


def _tree_entries(
    root: Path, tree: str, *, prefix: str = ""
) -> tuple[frozenset[str], frozenset[str]]:
    """列出树内的常规文件路径，以及非普通文件（gitlink 等）的路径。

    ``ls-tree -r -l -z`` 的格式是 ``<mode> <type> <sha> <size>\\t<path>\\0``
    （已实测）。``-z`` 不能省：否则中文路径是八进制转义的。

    ``prefix`` 非空（monorepo 子目录）时加 ``--full-name`` 并**显式**剥前缀：
    默认的 ``ls-tree`` 输出是相对 cwd 的（在子目录里调用时前缀已被 git 去掉、
    且前缀之外的条目被过滤掉）—— 那正是"看起来 0 个文件"的成因。
    显式剥前缀让口径与 :func:`diff`（``--relative``）和 :func:`restore`
    （``root / path``）保持同一个：**项目根口径**。
    """
    args = ["ls-tree", "-r", "-l", "-z"]
    if prefix:
        args.append("--full-name")
    proc = _run_git(root, *args, tree)
    if proc.returncode != 0:
        return frozenset(), frozenset()
    regular: set[str] = set()
    special: set[str] = set()
    for item in proc.stdout.split(b"\0"):
        if not item:
            continue
        meta, _, raw_path = item.partition(b"\t")
        parts = meta.split()
        if len(parts) < 3:
            continue
        mode = parts[0].decode("ascii", errors="replace")
        path = os.fsdecode(raw_path).replace(os.sep, "/")
        if prefix:
            if not path.startswith(prefix):
                # 前缀之外的条目不该出现在快照里；snapshot() 会用 --full-name
                # 复核并报错，这里只是不让它混进"项目根口径"的清单。
                continue
            path = path[len(prefix):]
        # 160000 = gitlink（嵌套仓库 / submodule），其**内部内容不在树里**。
        if mode in ("100644", "100755"):
            regular.add(path)
        else:
            special.add(path)
    return frozenset(regular), frozenset(special)


def _full_tree_paths(root: Path, tree: str) -> list[str]:
    """树的**仓库根口径**路径清单（``--full-name``）。

    只用于"快照有没有越过项目根"的复核：默认输出是相对 cwd 的，在子目录里
    前缀之外的条目会被 git 过滤掉 —— 用它复核等于自欺。
    """
    proc = _run_git(root, "ls-tree", "-r", "-z", "--name-only", "--full-name", tree)
    if proc.returncode != 0:
        return []
    return [p.replace(os.sep, "/") for p in _split_z(proc.stdout)]


def _restrict_index_to_prefix(layout: RepoLayout, env: Mapping[str, str]) -> None:
    """把独立索引里**项目根前缀之外**的条目全部摘掉（monorepo 子目录）。

    为什么必须在 ``write-tree`` 之前、且必须**显式**摘：

    * ``read-tree HEAD`` 是**仓库全局**口径 —— 索引里立刻有父仓库的所有条目；
    * ``add -u -- .`` 只更新 cwd 子树，**不会**删除别的条目；
    * 于是 ``write-tree`` 会把父仓库其它文件一起写进快照。用户以为保护了
      ``packages/foo``，实际把整个 monorepo 存了一份（还混进别人的密钥）。

    ``update-index --force-remove --stdin`` 的路径解析实测结论：**相对 cwd**。
    因此这条命令必须在**仓库顶层**执行（``-C top``）。在子目录里执行时，
    传进去的 ``other/o.py`` 会被解释成 ``packages/foo/other/o.py``，静默不匹配
    —— 退出码仍是 0，父仓库条目原封不动留在索引里。

    只删**索引条目**，不碰工作区文件、也不读 blob 内容（与密钥排除同一个理由：
    ``add`` 之后再删，明文早已落盘）。
    """
    proc = _run_git(layout.top, "ls-files", "-z", "--cached", "--full-name", env=env)
    if proc.returncode != 0:
        # 索引刚建/空仓库：没有可摘的东西；后面的 --full-name 复核会兜底。
        return
    outside = [p for p in _split_z(proc.stdout) if not layout.contains(p)]
    if not outside:
        return
    payload = b"".join(os.fsencode(p) + b"\0" for p in outside)
    proc = _run_git(
        layout.top,
        "update-index",
        "--force-remove",
        "-z",
        "--stdin",
        env=env,
        stdin=payload,
    )
    if proc.returncode != 0:
        raise GitError(
            "update-index (restrict to project prefix) failed: "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )


def _oversized_paths(root: Path, limit: int) -> set[str]:
    """找出超过单文件上限的路径，以便在 ``add`` **之前**就排除掉。

    必须在 add 之前：``add`` 之后再剔除，blob 已经落盘了 —— 与密钥那条是同一个道理。
    """
    if limit <= 0:
        return set()
    candidates: set[str] = set()
    for args in (("ls-files", "-z"), ("ls-files", "-o", "--exclude-standard", "-z")):
        proc = _run_git(root, *args)
        if proc.returncode == 0:
            candidates.update(_split_z(proc.stdout))
    oversized: set[str] = set()
    for rel in candidates:
        try:
            if (root / rel).is_file() and (root / rel).stat().st_size > limit:
                oversized.add(rel)
        except OSError:
            continue
    return oversized


@_guarded_git
def snapshot(
    root: Path,
    *,
    index_file: Path,
    store_root: Path | None = None,
    limits: Limits | None = None,
    settings: Mapping[str, object] | None = None,
    untracked_mode: UntrackedMode = "store",
) -> Snapshot:
    """把当前工作区写成 tree 对象，返回树 sha（以及未跟踪文件的 CAS 清单）。

    ``index_file`` **必须**在工作区之外（见模块头部第 1 条）。调用方通常传
    ``<appdata>/chiron/snapidx/<workspace_id>``；持久化的 index 是热态 179 ms 的来源。

    ``limits`` 显式传入时优先；否则按 ``settings``（D7 用户配置）+ 仓库对象体积
    解析 —— 单次预算硬下限 4 GB，用户设置只能往上调。

    第一步**必须**是 :func:`resolve_layout`：git 会向上找父仓库，不解析前缀就会
    把父仓库当工作区快照（见 :class:`RepoLayout`）。

    **monorepo 子目录**（``root`` 是仓库里的子目录）走同一条路径，只是多两步：
    ``read-tree HEAD`` 之后摘掉前缀之外的索引条目（:func:`_restrict_index_to_prefix`），
    ``write-tree`` 之后再复核树里没有前缀之外的路径。于是 ``tree`` 只含项目根子树，
    而路径口径仍是仓库根（tree 是仓库根的子树，``diff`` 用 ``--relative`` 转成
    项目根口径，``restore`` 用 ``cat-file <tree>:./<path>`` 反过来取）。
    """
    root = Path(root)
    layout = resolve_layout(root)
    prefix = layout.prefix
    index_file = Path(index_file)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    env = _git_env(index_file)
    limits = limits or Limits.resolve(settings, repo_objects_bytes=_repo_objects_bytes(root))

    # 1) 从 HEAD 起步，让未变更文件直接继承父树 —— write-tree 只写差异 blob。
    if _run_git(root, "read-tree", "HEAD", env=env).returncode != 0:
        _run_git(root, "read-tree", "--empty", env=env)
    # 1.5) monorepo：read-tree 是**仓库全局**口径，必须裁到项目根子树。
    if prefix:
        _restrict_index_to_prefix(layout, env)

    oversized = _oversized_paths(root, limits.max_file_bytes)
    # 树快照**只**排除密钥，不排除噪声。
    #
    #   理由：树里只会出现"用户已跟踪"的文件（`add -u` 与 `read-tree` 都只碰已跟踪
    #   路径）。已跟踪 == 用户认为它需要版本控制。若还按 `node_modules/**`、
    #   `dist/**` 去排除，会把**名字恰好是 dist/build/out/target 的源码目录**静默
    #   剔掉 —— 那是最难排查的一类数据丢失。
    #   噪声排除只留给未跟踪文件收集（`_collect_untracked`）：那里确实绝大多数是生成物。
    spec = _exclude_spec(list(SECRET_GLOBS))
    spec += [f":(exclude,literal){p}" for p in sorted(oversized)]

    # 2) 先把索引里**已存在**的排除项摘掉。
    #
    #    pathspec 排除只作用于"这次要 add 的路径"，管不到 `read-tree HEAD` 已经
    #    带进索引的条目。实测（$TMPDIR/headenv）：用户把 `.env` 提交过一次之后，
    #    `read-tree HEAD` + `add -u -- . ':(exclude,glob).env'` 的树里**仍然有 .env**。
    #    所以必须显式 `update-index --force-remove`。
    #
    #    这条命令只删**索引条目**，不碰工作区文件，也不读 blob 内容 ——
    #    因此不会像 `add` 那样把明文写进对象库。
    staged_excluded = [
        p
        for p in _index_paths(root, env)
        if _is_excluded(p, SECRET_GLOBS) or p in oversized
    ]
    if staged_excluded:
        payload = b"".join(os.fsencode(p) + b"\0" for p in staged_excluded)
        proc = _run_git(root, "update-index", "--force-remove", "-z", "--stdin", env=env, stdin=payload)
        if proc.returncode != 0:
            raise GitError(
                "update-index failed: "
                + proc.stderr.decode("utf-8", errors="replace").strip()
            )

    # 3) 写入独立索引。
    #    ``-u``：只更新已跟踪文件（store 模式；未跟踪交给 CAS）
    #    ``-A``：含未跟踪文件（tree 模式）
    #
    #    空仓库（还没有 HEAD，也没有已跟踪文件）时 ``add`` 一个候选都没有，
    #    git 会因 `:(exclude,...)` 全不匹配而**报错退出**（实测
    #    `did not match any file(s) known to git`）。这不是失败，跳过即可 ——
    #    靠匹配 stderr 字符串来判断太脆，直接看候选集。
    add_flag = "-A" if untracked_mode == "tree" else "-u"
    has_candidates = bool(_index_paths(root, env))
    if untracked_mode == "tree" and not has_candidates:
        has_candidates = bool(_untracked_candidates(root))
    if has_candidates:
        proc = _run_git(root, "add", add_flag, "--", ".", *spec, env=env)
        if proc.returncode != 0:
            if b"did not match any file" not in proc.stderr:
                raise GitError(
                    "git add failed: " + proc.stderr.decode("utf-8", errors="replace").strip()
                )

    # 3.5) 补上"用户已暂存、但 HEAD 里还没有"的新文件。
    #
    #   这类文件**三处机制都不管**，实测（$TMPDIR/staged）确认会被静默丢掉：
    #     * `read-tree HEAD` —— HEAD 里没有它，所以索引里也没有；
    #     * `add -u`        —— 只更新"已跟踪"文件，它在 HEAD 里不叫已跟踪；
    #     * `ls-files -o`   —— 它已进索引，所以不算未跟踪。
    #   结果是「用户 git add 了 new.py，检查点里却没有 new.py」。这是最坏的一类
    #   丢失：用户已经明确表达过"这个文件要留下"。
    #
    #   从**用户自己的 index** 取路径（这里故意不传独立 env），再喂给独立索引。
    staged_new: list[str] = []
    if untracked_mode != "tree":
        staged_new = [
            p
            for p in _staged_added_paths(root)
            if not _is_excluded(p, SECRET_GLOBS) and p not in oversized and (root / p).is_file()
        ]
        if staged_new:
            proc = _run_git(root, "add", "--", *staged_new, env=env)
            if proc.returncode != 0:
                raise GitError(
                    "git add (staged new) failed: "
                    + proc.stderr.decode("utf-8", errors="replace").strip()
                )

    tree = _must(_run_git(root, "write-tree", env=env), "write-tree").decode("ascii").strip()

    # 4) 复核：树里**不得**有项目根前缀之外的路径。
    #
    #    这一条是 monorepo 模式的安全边界。没有它，一次前缀裁剪失败（例如上面
    #    ``update-index`` 静默不匹配）就会让快照变成"整个父仓库"，而上报的
    #    ``file_count`` 还是子目录那点数字 —— 用户永远不会发现自己的 repo 被复制
    #    进了检查点。宁可这次检查点失败（fail-open 只是少一个还原点）。
    if prefix:
        stray = [p for p in _full_tree_paths(root, tree) if not p.startswith(prefix)]
        if stray:
            preview = ", ".join(stray[:5]) + (" ..." if len(stray) > 5 else "")
            raise GitError(
                f"checkpoint snapshot leaked paths outside project root {root} "
                f"(prefix {prefix!r}): {preview}；已放弃本次快照，"
                "不会把父仓库的内容写进检查点。"
            )

    untracked: dict[str, str] = {}
    skipped = 0
    if untracked_mode == "store":
        untracked, skipped = _collect_untracked(root, store_root, index_file, limits, oversized)

    tree_paths, non_regular = _tree_entries(root, tree, prefix=prefix)
    secrets = sum(1 for p in _untracked_candidates(root) if _is_excluded(p, SECRET_GLOBS))

    return Snapshot(
        tree=tree,
        untracked=untracked,
        tree_paths=tree_paths,
        non_regular=non_regular,
        skipped_secrets=secrets,
        skipped_oversized=len(oversized) + skipped,
        mode=untracked_mode,
    )


def _index_paths(root: Path, env: Mapping[str, str]) -> list[str]:
    """当前**独立 index** 里已暂存的路径（POSIX 分隔）。

    用于在 ``add`` 之前把"已经从 HEAD 继承进来"的排除项摘掉 ——
    pathspec 排除管不到索引里已存在的条目（见 ``snapshot()`` 第 2 步）。
    """
    proc = _run_git(root, "ls-files", "-z", "--cached", env=env)
    if proc.returncode != 0:
        return []
    return [p.replace(os.sep, "/") for p in _split_z(proc.stdout)]


def _staged_added_paths(root: Path) -> list[str]:
    """**用户自己的 index** 里已暂存、但 HEAD 里还没有的路径。

    这是 ``snapshot()`` 第 3.5 步的输入。不加 ``GIT_INDEX_FILE``（``_git_env()``
    已经把它清掉了），所以读到的是仓库默认 index —— 也就是用户真正的暂存区。

    初始提交之前没有 HEAD，``diff --cached HEAD`` 会失败；此时把这些文件视为
    "新增"更准确，但为了不把整个初始暂存区当成检查点内容，这里选择返回空：
    没有 HEAD 的仓库本来就没有可回滚的历史，P1 会在建检查点前先做 ``git init`` 校验。
    """
    if _run_git(root, "rev-parse", "--verify", "--quiet", "HEAD").returncode != 0:
        return []
    # ``--relative`` 不能省：不带它时 ``diff --cached`` 输出的是**仓库根**口径
    # （``packages/foo/new.txt``），而其余一切路径都是项目根口径（``new.txt``）。
    # 混用会让恢复时去 ``cat-file <tree>:packages/foo/new.txt`` 取到错误对象。
    proc = _run_git(
        root, "diff", "--cached", "--name-only", "-z", "--relative", "--diff-filter=A", "HEAD"
    )
    if proc.returncode != 0:
        return []
    return [p.replace(os.sep, "/") for p in _split_z(proc.stdout)]


def _untracked_candidates(root: Path) -> list[str]:
    """git 眼中的未跟踪文件（遵守用户的 ``.gitignore``）。"""
    proc = _run_git(root, "ls-files", "-o", "--exclude-standard", "-z")
    if proc.returncode != 0:
        return []
    return _split_z(proc.stdout)


def _collect_untracked(
    root: Path,
    store_root: Path | None,
    index_file: Path,
    limits: Limits,
    oversized: Iterable[str],
) -> tuple[dict[str, str], int]:
    """把未跟踪文件写进 CAS，返回 ``(path -> sha256, 跳过数)``。

    剔除规则与树模式一致：密钥与噪声都不进，超限的也跳过（并计数）。
    """
    if store_root is None:
        # 默认与 index 同目录（都在工作区之外），不再额外要求配置。
        store_root = Path(index_file).parent / "untracked"
    store = UntrackedStore(Path(store_root))
    skip = {p.replace(os.sep, "/") for p in oversized}
    manifest: dict[str, str] = {}
    skipped = 0
    total = 0
    for rel in _untracked_candidates(root):
        norm = rel.replace(os.sep, "/")
        if norm in skip or _is_excluded(norm, SECRET_GLOBS + NOISE_GLOBS):
            continue
        path = root / rel
        try:
            if not path.is_file():
                continue  # 目录、符号链接的悬空目标等都不进
            size = path.stat().st_size
        except OSError:
            continue
        if size > limits.max_file_bytes or total + size > limits.max_checkpoint_bytes:
            skipped += 1
            continue
        try:
            data = path.read_bytes()
        except OSError:
            skipped += 1
            continue
        manifest[norm] = store.put(data)
        total += size
    return manifest, skipped


# ---------------------------------------------------------------------------
# 差异（三分类）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffPlan:
    """回滚前的改动分类。

    ``removed`` **默认不会被执行** —— 它只是清单，等用户勾选。
    """

    restored: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    #: 本会出现在 ``removed`` 但因保护列表而**被剔除**的路径。
    #: 单独列出便于排查，但绝不呈现为"可删除项"。
    protected_removed: tuple[str, ...] = ()
    #: 目标里是 gitlink / 非常规文件，内容无法恢复。
    unsupported: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not (self.restored or self.added or self.removed)


def _tree_diff(root: Path, base: str, target: str) -> list[tuple[str, str]]:
    """``git diff-tree`` 的 ``(status, path)`` 列表。

    ``-z`` 下每项是 ``<status>\\0<path>\\0``（已实测）。``R``/``C`` 会多带一个路径，
    这里按状态长度跳过额外项 —— 不加 ``-M`` 时不会出现，但解析要稳。
    """
    # ``--relative`` 是这里的关键：两份树都是**仓库根口径**（monorepo 子目录下
    # 树里的路径是 ``packages/foo/src/a.txt``），而 ``_tree_entries`` /
    # ``ls-files`` 都是**项目根口径**（``src/a.txt``）。``--relative`` 在
    # ``-C root`` 的子目录里过滤并剥掉前缀，正好把树路径翻成项目根口径 ——
    # 实测 ``M\\0a.py``。不加它，``restore()`` 会拿仓库根路径去拼
    # ``root / path``，写进 ``packages/foo/packages/foo/src/a.txt``，
    # 而 ``written`` 返回空、``ok=True`` —— 用户看到"回滚成功"却没改动任何文件（实测）。
    proc = _run_git(
        root, "diff-tree", "-r", "-z", "--no-commit-id", "--relative", "--name-status", base, target
    )
    if proc.returncode != 0:
        raise GitError(
            "diff-tree failed: " + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    fields = _split_z(proc.stdout)
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        status = fields[i]
        if len(status) >= 2 and status[0] in "RC":
            # R100\0old\0new\0 —— 取新路径
            if i + 2 < len(fields):
                out.append((status[0], fields[i + 2]))
            i += 3
        else:
            if i + 1 < len(fields):
                out.append((status[:1], fields[i + 1]))
            i += 2
    return out


@_guarded_git
def diff(
    root: Path,
    *,
    current: Snapshot,
    target: Snapshot,
    protected: Sequence[str] | None = None,
) -> DiffPlan:
    """算出"回到 ``target``"需要做哪些事。

    语义：

    * ``restored``：目标有、当前内容不同（或当前没有但目标有 —— 见 ``added``）；
    * ``added``：目标有、当前完全没有；
    * ``removed``：当前有、目标没有 —— **需要用户勾选才执行**。

    注意 ``diff`` 比较的是两份**快照**，不是"快照 vs 磁盘"。
    调用方要先对当前状态 ``snapshot()`` 一次，结果才反映磁盘。
    """
    guard = tuple(protected) if protected is not None else PROTECTED_GLOBS

    restored: list[str] = []
    added: list[str] = []
    removed: list[str] = []
    protected_hit: list[str] = []

    for status, path in _tree_diff(root, current.tree, target.tree):
        if status == "A":
            added.append(path)
        elif status == "D":
            if _is_excluded(path, guard):
                protected_hit.append(path)
            else:
                removed.append(path)
        else:  # M / T（类型变化）
            restored.append(path)

    # 未跟踪文件按内容摘要比较。
    cur_untracked = {p.replace(os.sep, "/"): h for p, h in current.untracked.items()}
    tgt_untracked = {p.replace(os.sep, "/"): h for p, h in target.untracked.items()}
    for path, digest in tgt_untracked.items():
        if path not in cur_untracked:
            added.append(path)
        elif cur_untracked[path] != digest:
            restored.append(path)
    for path in cur_untracked:
        if path not in tgt_untracked:
            if _is_excluded(path, guard):
                protected_hit.append(path)
            else:
                removed.append(path)

    unsupported = sorted(p for p in target.non_regular if p not in target.tree_paths)

    def _uniq(items: Iterable[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item not in seen:
                seen.add(item)
                out.append(item)
        return tuple(out)

    return DiffPlan(
        restored=_uniq(restored),
        added=_uniq(added),
        removed=_uniq(removed),
        protected_removed=_uniq(protected_hit),
        unsupported=tuple(unsupported),
    )


# ---------------------------------------------------------------------------
# 恢复（两阶段落地）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RestoreResult:
    """回滚结果。

    ``failed`` 非空即"部分成功" —— 这是真实可达状态，不是理论推演：
    实测 ``chmod 444`` 的文件会让 ``cp`` 报 ``Permission denied``，
    而其余文件已经落地（实测 land1 样本）。
    此时**已落的文件不回退**（反转风险更大），并把清单交给用户。
    """

    written: tuple[str, ...] = ()
    added: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    #: 因为删文件而变空、随后被清掉的父目录（相对仓库根）。
    #: 单独一栏是因为它不是"删了用户的文件"，而是收拾残局。
    pruned_dirs: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    #: 因为 ``allow_delete=False`` 而**没有删**的 ``removed`` 类路径。
    skipped_removed: tuple[str, ...] = ()
    #: 因为命中保护列表而从未进入"可删除项"的路径（永远不删）。
    protected_removed: tuple[str, ...] = ()
    #: 目标里是 gitlink 等无法恢复的内容。
    unsupported: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed


def _read_blob(root: Path, tree: str, path: str) -> bytes:
    """取树里的内容，并且**过 smudge 过滤器**。

    ``--filters`` 是这里唯一正确的选择（实测）：

    * ``git show <tree>:f`` 取的是仓库内的 blob，已被 ``add`` 规范化成 LF；
      在 ``core.autocrlf=true`` 下写回工作区就是 CRLF → LF 的静默篡改，
      ``git status`` 立刻变脏。
    * ``git cat-file --filters`` 会应用 ``.gitattributes`` / ``core.autocrlf``，
      还原成工作区应有的字节，``git status`` 保持为空。

    ``path`` 是**项目根**相对路径（``_tree_diff`` / ``_tree_entries`` 的口径）。
    ``cat-file`` 默认按**仓库根**解析，所以必须写成 ``<tree>:./<path>`` ——
    ``./`` 让它跟着 ``-C root`` 的工作目录走（实测：monorepo 子目录下
    ``<tree>:src/a.txt`` 报 ``path does not exist``，``<tree>:./src/a.txt`` 正常）。
    手工拼 ``rev-parse --show-prefix`` 也能对，但多一次子进程且要处理分隔符。
    """
    proc = _run_git(root, "cat-file", "--filters", f"{tree}:./{path}")
    if proc.returncode != 0:
        raise GitError(
            f"cat-file failed for {path}: "
            + proc.stderr.decode("utf-8", errors="replace").strip()
        )
    return proc.stdout


@dataclass(frozen=True)
class RollbackPolicy:
    """回滚的可配置项（D5）。

    **默认禁止删除**（``allow_delete=False``）。这是刻意的：开启删除后，
    用户丢失的是"agent 新建但还没提交的文件"，而这类文件**不在 git 里**，
    删掉就只能靠检查点的 CAS 找回 —— 代价远高于"多留一个文件"。
    用户在设置里显式打开才生效，且命中 ``PROTECTED_GLOBS`` 的路径永不删。
    """

    allow_delete: bool = False

    @classmethod
    def from_settings(cls, settings: Mapping[str, object] | None = None) -> RollbackPolicy:
        """从用户设置解析。**缺失或非法一律回落到"禁止"**。

        ``settings`` 里认 ``allow_delete`` 的 JSON 布尔、``"true"``/``"1"`` 等写法；
        解析不了的字符串当作没配置 —— 宁可少删，不可误删。
        """
        value = _as_bool((settings or {}).get("allow_delete"))
        return cls(allow_delete=bool(value))


def _prune_empty_dirs(root: Path, paths: Iterable[str]) -> list[str]:
    """删除文件后清掉因此变空的父目录，向上直到 ``root``（不含）。

    不清理的话，回滚会留下一堆空目录：git 不跟踪空目录，所以 ``git status``
    依然干净，用户却看到一堆空壳。这是"回滚了但看起来没回滚"的典型困扰。

    只删**确实为空**的目录，且绝不越过 ``root``；任何异常都静默跳过
    （目录非空、被占用、权限不足都不是回滚失败）。
    """
    root = Path(root).resolve()
    removed: list[str] = []
    for rel in paths:
        parent = (root / rel).parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()  # 只在空目录时成功
            except OSError:
                break
            removed.append(str(parent.relative_to(root)).replace(os.sep, "/"))
            parent = parent.parent
    return removed


@_guarded_git
def restore(
    root: Path,
    *,
    target: Snapshot,
    store_root: Path | None = None,
    paths: Sequence[str] | None = None,
    allow_delete: bool | None = None,
    settings: Mapping[str, object] | None = None,
    protected: Sequence[str] | None = None,
    index_file: Path | None = None,
) -> RestoreResult:
    """把 ``paths``（默认全部）恢复成 ``target`` 的样子。

    **两阶段**：先把所有内容取到临时目录并全部成功，再逐个落地。
    失败被压缩到第二阶段，而且逐文件报告 —— 绝不"改一半还说成功"。

    ``allow_delete`` 默认 ``None`` = 从 ``settings`` 解析，而解析不出来时
    **禁止删除**（D5：可自定义，但默认禁止）。``removed`` 类路径只报告不动作。
    显式传 ``True``/``False`` 时以它为准（内部 API 用）。
    """
    root = Path(root)
    guard = tuple(protected) if protected is not None else PROTECTED_GLOBS

    if allow_delete is None:
        allow_delete = RollbackPolicy.from_settings(settings).allow_delete

    if store_root is None:
        if index_file is None:
            raise ValueError("restore() 需要 store_root 或 index_file 之一来确定 CAS 位置")
        store_root = Path(index_file).parent / "untracked"
    store = UntrackedStore(Path(store_root))

    # 用来观察"当前状态"的临时 index。**绝不复用调用方的 index**：
    # 那是快照热态缓存（179 ms 的来源），被回滚顺手覆盖会在下一次检查点
    # 触发一次无谓的全量重建。这里派生一个同目录的兄弟文件，位置仍满足
    # "index 必须在工作区之外"。
    if index_file is not None:
        probe_index = Path(index_file).with_name(Path(index_file).name + ".restore")
    else:
        probe_index = Path(store_root).parent / "restore.idx"

    current = snapshot(
        root,
        index_file=probe_index,
        store_root=Path(store_root),
        untracked_mode=target.mode,
    )
    plan = diff(root, current=current, target=target, protected=guard)

    wanted = set(paths) if paths is not None else None
    todo = [p for p in (*plan.restored, *plan.added) if wanted is None or p in wanted]

    # ---- 阶段 1：全部内容先取到临时目录 ----
    stage = Path(tempfile.mkdtemp(prefix="chiron-restore-"))
    staged: list[tuple[str, Path]] = []
    failed: list[tuple[str, str]] = []
    try:
        for i, path in enumerate(todo):
            try:
                if path in target.untracked:
                    data = store.get(target.untracked[path])
                else:
                    data = _read_blob(root, target.tree, path)
            except (GitError, OSError) as exc:
                failed.append((path, str(exc)))
                continue
            blob = stage / f"{i}.bin"
            blob.write_bytes(data)
            staged.append((path, blob))

        # ---- 阶段 2：逐个落地 ----
        written: list[str] = []
        added: list[str] = []
        for path, blob in staged:
            dest = root / path
            try:
                existed = dest.exists()
                # added 类必须建父目录，否则 shutil.move 报 "No such file or directory"。
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(blob), str(dest))
                (written if existed else added).append(path)
            except OSError as exc:
                failed.append((path, str(exc)))
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    deleted: list[str] = []
    skipped: list[str] = []
    pruned: list[str] = []
    if allow_delete:
        for path in plan.removed:
            if wanted is not None and path not in wanted:
                continue
            target_path = root / path
            try:
                target_path.unlink()
                deleted.append(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                failed.append((path, str(exc)))
        # 删完再收空目录：先收会出现"目录非空所以留着"的中间态。
        pruned = _prune_empty_dirs(root, deleted)
    else:
        skipped = list(plan.removed)

    return RestoreResult(
        written=tuple(written),
        added=tuple(added),
        deleted=tuple(deleted),
        pruned_dirs=tuple(pruned),
        failed=tuple(failed),
        skipped_removed=tuple(skipped),
        protected_removed=tuple(plan.protected_removed),
        unsupported=plan.unsupported,
    )


# ---------------------------------------------------------------------------
# 提交与 ref（供 P1 使用，P0.5 只保证纯函数可测）
# ---------------------------------------------------------------------------


@_guarded_git
def commit_tree(
    root: Path,
    *,
    tree: str,
    parent: str | None = None,
    message: str = "chiron checkpoint",
    head_sha: str | None = None,
    index_file: Path | None = None,
) -> str:
    """把 tree 包成 commit，返回 commit sha。

    **父提交是上一个检查点，不是 HEAD**。用 HEAD 做父会让检查点链
    与用户的分支历史纠缠：用户 rebase/amend 之后，检查点的祖先关系就代表不了
    "会话的时间顺序"。t当时的 HEAD 记在 trailer 里，仅供审计。
    实测对照：``-p HEAD`` 得到 ``CHAIN_BROKEN``，``-p <prev_cp>`` 得到 ``CHAIN_INTACT``。

    **monorepo 子目录的 commit 也写父仓库的对象库**（既定设计）：树本身就放在那
    个库里，写别处会让 ``git gc`` 立刻把它当垃圾回收掉；ref 命名不变
    （``refs/chiron/checkpoints/<session>/...``），因此子目录项目与仓库顶层项目
    共用同一个 ref 命名空间 —— 会话 id 唯一，不会撞号。
    """
    body = message
    if head_sha:
        body += f"\n\nChiron-Head: {head_sha}"
    args = ["commit-tree", tree, "-m", body]
    if parent:
        args += ["-p", parent]
    proc = _run_git(root, *args, env=_git_env(index_file))
    return _must(proc, "commit-tree").decode("ascii").strip()


def checkpoint_ref_prefix(session_id: str) -> str:
    """某个会话所有检查点 ref 的前缀（``refs/chiron/checkpoints/<session>/``）。"""
    return f"refs/chiron/checkpoints/{session_id}/"


@_guarded_git
def list_checkpoint_refs(root: Path, session_id: str, *, index_file: Path | None = None) -> list[str]:
    """列出该会话**已存在**的检查点 ref 名（完整 refname）。

    **必须走 `for-each-ref`，不能遍历 `.git/refs/...` 目录** —— 这一点是实测换来的：

    * `git gc` 默认带 `pack-refs --all --prune`（松散对象超阈值时自动触发）。打包后
      松散文件消失、ref 只留在 `.git/packed-refs` 里。按目录枚举会读到"一个都没有"，
      于是序号回退到 1，而 `write_ref` 的 CAS（旧值必须为空）必然失败 ——
      **每次检查点创建都静默失败**（fail-open 让对话照常，用户只看到时间线不再增长）。
    * git worktree / submodule 下 `.git` 是**文件**而不是目录，`iterdir()` 直接抛
      `OSError`，同样恒回退到 1。

    `for-each-ref` 同时覆盖松散与打包 ref，也不关心 `.git` 是目录还是文件。
    命令失败时返回空列表（调用方据此从 1 开始）—— 与"没有检查点"同义，且不抛。

    **monorepo 子目录同样要读父仓库的 ref**：ref 命名不变（既定设计），
    子目录项目的检查点 ref 就落在父仓库的 `refs/chiron/**` 下。此前这里返回空，
    导致子目录项目的序号恒为 1、`write_ref` 的 CAS 必然失败 —— 时间线永远不增长。
    不相关的会话因为 ref 名里带 session_id 而天然隔离，不会互相影响序号。
    """
    prefix = checkpoint_ref_prefix(session_id)
    try:
        resolve_layout(root)
    except GitError:
        return []
    proc = _run_git(
        root,
        "for-each-ref",
        "--format=%(refname)",
        prefix,
        env=_git_env(index_file),
    )
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        name = line.strip()
        # `for-each-ref <prefix>` 已经过滤过，这里再校验一次前缀：
        # 任何非本会话的 ref 都不该参与序号计算（否则会跳号或撞号）。
        if name.startswith(prefix):
            out.append(name)
    return out


def next_checkpoint_seq(root: Path, session_id: str, *, index_file: Path | None = None) -> int:
    """该会话下一个可用序号：已有 ref 的最大序号 + 1；一个都没有时从 1 开始。

    只认 ``<seq:06d>-<kind>`` 里那串数字（见 :func:`checkpoint_ref`）。
    """
    best = 0
    for ref in list_checkpoint_refs(root, session_id, index_file=index_file):
        tail = ref[len(checkpoint_ref_prefix(session_id)):]
        head, _, _kind = tail.partition("-")
        if head.isdigit():
            best = max(best, int(head))
    return best + 1


def checkpoint_ref(session_id: str, seq: int, kind: str) -> str:
    """检查点 ref 名。

    用真实 ref（而不是只存 sha）是为了让 ``git gc`` 认它 —— 实测 ``SURVIVED_GC``。
    但它在 ``refs/chiron/**`` 下，``git branch`` 不会列出，用户的分类面板不会多出噪音。
    """
    return f"refs/chiron/checkpoints/{session_id}/{seq:06d}-{kind}"


@_guarded_git
def write_ref(root: Path, ref: str, sha: str, *, index_file: Path | None = None) -> None:
    """CAS 写入 ref：旧值必须是"空"，因此**物理上不可能覆盖**已有检查点。

    自定义命名空间**没有 reflog**（实测 ``core.logAllRefUpdates=true`` 不覆盖它们），
    所以外部改写被发现后只能靠 DB 的 ``commit_sha`` 重建，不能靠 reflog 回溯。
    """
    proc = _run_git(root, "update-ref", ref, sha, "", env=_git_env(index_file))
    _must(proc, f"update-ref {ref}")


@_guarded_git
def read_ref(root: Path, ref: str, *, index_file: Path | None = None) -> str | None:
    """读 ref 指向的 sha；不存在返回 None（不抛异常 —— ref 缺失是**正常**的修复场景）。"""
    proc = _run_git(root, "rev-parse", "--verify", "--quiet", ref, env=_git_env(index_file))
    if proc.returncode != 0:
        return None
    value = proc.stdout.decode("ascii", errors="replace").strip()
    return value or None
