"""isolation — 可分级、可撤销的 Windows 原生执行隔离。

## 为什么不是 Docker

本仓库是 **Electron 桌面端 + 内嵌 PG/Redis**（见 `desktop-local/app/launcher.py` 的
架构说明）。要求终端用户安装 Docker Desktop 才能用产品是不可接受的，因此"受限容器"
在本项目里**不可能**是默认路径。隔离必须用 Windows 自带机制实现，本包就是那一层。

## 四个档位（强度递增）

| 档位 | 挡住什么 | 代价 |
|---|---|---|
| `none` | 什么都不挡（仅保留 `sandbox.py` 的命令白名单/逃逸正则） | 无 |
| `job` | 内存/CPU 打爆、进程数失控、退出后子孙进程残留 | 无 |
| `low_il` | 以上 + **写**用户目录/注册表等中完整性对象 | 无（实测零提权可用） |
| `appcontainer` | 以上 + 文件系统按包 SID 白名单、可断网 | 需 `icacls` 授权路径 |

实测依据（本机，非管理员账户）：
- Job Object：256MB 上限成功拦下 600MB 分配；
- Low IL：`SetTokenInformation` 成功，子进程写 `%USERPROFILE%` 被拒；
- AppContainer：profile 可零提权创建，但**必须**先授权目录，否则子进程
  `STATUS_DLL_NOT_FOUND (0xC0000135)`。

## 关键约束：不许静默降级

`resolve_level()` 在"请求的档位不可用"时**不会**悄悄退回 `none` —— 它会返回实际
生效的档位并在 `IsolationDecision.reasons` 里说明原因，同时由调用方决定是告警还是
拒绝执行。这与 README 承诺的"不偷偷回退"是一致的：降级必须**可见**。

要显式允许降级，设 `SANDBOX_ISOLATION_ALLOW_DOWNGRADE=true`（单机开发用）。
"""

from __future__ import annotations

import enum
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any

from app.isolation import win, win_token

logger = logging.getLogger(__name__)

LEVEL_ENV = "SANDBOX_ISOLATION_LEVEL"
ALLOW_DOWNGRADE_ENV = "SANDBOX_ISOLATION_ALLOW_DOWNGRADE"


class Level(enum.StrEnum):
    """隔离档位。顺序即强度顺序（用于比较与"至少要到某档"的判断）。"""

    NONE = "none"
    JOB = "job"
    LOW_IL = "low_il"
    RESTRICTED = "restricted"
    APPCONTAINER = "appcontainer"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)


_ORDER = [Level.NONE, Level.JOB, Level.LOW_IL, Level.RESTRICTED, Level.APPCONTAINER]

#: 默认档位：`restricted`（受限令牌 + 合成 SID + 一条可撤销 ACE）。
#:
#: 为什么默认它而不是 `low_il`：`low_il` 要先给目录授当前用户完全控制才能打 Low
#: 标签，对用户资产是**实质且不可逆**的改动（`set_path_integrity` 打回去也换不回
#: 继承来的原始 ACE）。`restricted` 只在根目录加一条 ACE，撤销即删除 —— 同一档
#: 强度，代价却是可逆的，所以没有理由让默认值停在更脏的那个。
#:
#: `low_il` 保留为**可选**档位（老配置、或 restricted 机制在本机不可用时回落）。
#: 非 Windows 上自然退化为 `none`（POSIX 继续走 setrlimit）。
DEFAULT_LEVEL = Level.RESTRICTED

#: **硬底线**：Windows 上实际生效的档位不得低于此值。
#:
#: 与 `DEFAULT_LEVEL` 的区别很关键：默认值可以被配置改掉，底线**不能**。
#: `SANDBOX_ISOLATION_ALLOW_DOWNGRADE=true` 的语义是"接受稍弱但仍存在的隔离"，
#: 不是"关掉隔离"。把这两件事分开，是因为一个布尔开关不该有能力抹掉 README 里
#: 「执行隔离是默认值 / 沙箱不可用时拒绝启动」这条承诺。
#:
#: 为什么底线选 `low_il` 而不是 `job`：实测（零提权）`job` 只能拦内存爆炸，
#: 挡不住写用户目录；`low_il` 是**当前这台机器上唯一既零提权又真能挡写**的档位。
#: 定在 `job` 等于允许"看起来有隔离"的不隔离。
#:
#: 非 Windows 不受此限：Linux 侧保持既有 local 实现（POSIX 走 setrlimit）。
FLOOR_LEVEL = Level.LOW_IL


def parse_level(value: str | None, *, default: Level = DEFAULT_LEVEL) -> Level:
    """把配置值解析成 `Level`。**拼错就报错**，不猜 —— 见 `deps.verify_sandbox_backend`。"""
    if not value or not value.strip():
        return default
    raw = value.strip().lower()
    try:
        return Level(raw)
    except ValueError:
        allowed = ", ".join(lv.value for lv in _ORDER)
        raise ValueError(f"invalid isolation level {value!r}; allowed: {allowed}") from None


def configured_level() -> Level:
    """读配置。优先级：**运行时覆盖 > env > settings > 默认**。

    运行时覆盖放最前，是为了让 ``PUT /v1/system/isolation`` 能立即生效 ——
    它只在进程内改，不写 .env，所以重启即恢复原值（这正是"随时可撤销"的实现方式：
    撤销不需要磁盘状态一致，只需要清掉内存变量）。
    """
    if _runtime_override is not None:
        return _runtime_override
    raw = os.getenv(LEVEL_ENV)
    if raw:
        try:
            return parse_level(raw)
        except ValueError as exc:
            # 拼错必须显式失败：否则一个字母之差就等于关掉隔离
            raise RuntimeError(str(exc)) from exc
    try:
        from app.config import settings

        raw = getattr(settings, "sandbox_isolation_level", "") or ""
        return parse_level(raw)
    except Exception:  # noqa: BLE001 — 配置不可读时用默认
        return DEFAULT_LEVEL


#: 运行时覆盖（进程内，不落盘）。`None` = 未覆盖，按配置走。
#:
#: 为什么不写回 .env：写盘会让"改一下试试"变成持久状态，而撤销要同时回滚文件和
#: 内存。放在内存里，重启即回到配置文件的值 —— 撤销是免费且必然成功的。
_runtime_override: Level | None = None


def runtime_override() -> Level | None:
    """当前运行时覆盖值（None = 没有覆盖）。供前端显示"这是临时改动"。"""
    return _runtime_override


def set_runtime_level(level: Level) -> Level | None:
    """设置运行时档位，返回**被替换掉的旧覆盖值**（供撤销）。

    低于 ``FLOOR_LEVEL`` 的请求在 Windows 上会被抬到底线 —— 这里不抬，
    因为 ``resolve_level`` 已经统一处理；本函数只负责"记住用户想要什么"，
    让 ``status()`` 能显示"请求 none、实际 low_il"这段差异。
    """
    global _runtime_override
    previous = _runtime_override
    _runtime_override = level
    logger.info("isolation level overridden at runtime: %s -> %s", previous, level)
    return previous


def clear_runtime_level() -> Level | None:
    """撤销运行时覆盖，返回撤销前的值（None 表示本来就没有覆盖）。"""
    global _runtime_override
    previous = _runtime_override
    _runtime_override = None
    if previous is not None:
        logger.info("isolation level runtime override cleared (was %s)", previous)
    return previous


def downgrade_allowed() -> bool:
    v = (os.getenv(ALLOW_DOWNGRADE_ENV) or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    try:
        from app.config import settings

        return bool(getattr(settings, "sandbox_isolation_allow_downgrade", False))
    except Exception:  # noqa: BLE001
        return False


@dataclass
class IsolationDecision:
    """实际生效的隔离决策 —— **请求值与被满足值都留下**，便于审计。"""

    requested: Level
    effective: Level
    reasons: list[str] = field(default_factory=list)
    #: 实际档位是否**低于硬底线**（`FLOOR_LEVEL`）。低于底线时**任何配置都不能放行**：
    #: `allow_downgrade` 管的是"接受稍弱"，不是"接受没有"。
    below_floor: bool = False

    @property
    def degraded(self) -> bool:
        return self.effective.rank < self.requested.rank

    def describe(self) -> str:
        if not self.degraded:
            return f"isolation={self.effective.value}"
        detail = "; ".join(self.reasons) or "原因未知"
        return f"isolation={self.effective.value}（请求 {self.requested.value}，降级: {detail}）"


def resolve_level(requested: Level | None = None) -> IsolationDecision:
    """把"请求的档位"落到"本机真能提供的档位"。

    **不静默降级**：降级时 `reasons` 必非空，调用方据此告警或拒绝执行。

    两条不同的约束，别混为一谈：

    * 请求值**低于** `FLOOR_LEVEL`（显式要 `none`/`job`）⇒ Windows 上按底线执行，
      并在 `reasons` 说明。这是"配置改不动底线"。
    * 本机机制缺失导致实际档位**落到** `FLOOR_LEVEL` 以下 ⇒ 标记 `below_floor`，
      `require_or_raise` 会拒绝执行，**不看** `allow_downgrade`。
    """
    want = requested or configured_level()
    reasons: list[str] = []

    if not win.IS_WINDOWS:
        # Linux 侧保持既有 local 实现（POSIX 走 setrlimit），不受硬底线约束。
        return IsolationDecision(want, Level.NONE, ["非 Windows 平台；POSIX 走 setrlimit"])

    # ① 请求值不得低于底线：显式写 `none` 也不能真的关掉隔离。
    if want.rank < FLOOR_LEVEL.rank:
        reasons.append(f"请求档位 {want.value} 低于底线 {FLOOR_LEVEL.value}，按底线执行")
        want = FLOOR_LEVEL

    # Job Object 是所有高于 none 的档位共同的地基
    if want.rank >= Level.JOB.rank and not _probe_job():
        reasons.append("Job Object 不可用")
        return IsolationDecision(want, Level.NONE, reasons, below_floor=True)

    if want.rank >= Level.LOW_IL.rank and not _probe_low_il():
        reasons.append("Low 完整性级别不可设置")
        return IsolationDecision(want, Level.JOB, reasons, below_floor=True)

    # Low 完整性标签是 `low_il` / `appcontainer` 的必需前置，但**不是** `restricted`
    # 的前置：受限令牌靠 restricting SIDs 判写，与完整性级别无关。原来这里用
    # `>= LOW_IL` 会把 `restricted` 也拉进"必须给整目录打 Low 标签"，
    # 等于把代价更小的方案按更脏的那条实现 —— 所以按档位精确判断。
    if want in (Level.LOW_IL, Level.APPCONTAINER):
        ws = appcontainer_workspace()
        labeled, lerr = _ensure_workspace_low_label(ws)
        if not labeled:
            reasons.append(f"工作区无法打 Low 完整性标签: {lerr}")
            return IsolationDecision(want, Level.JOB, reasons, below_floor=True)

    if want is Level.RESTRICTED:
        if not _probe_restricted():
            # 把**具体**原因写进 reasons：`exec_unavailable_reason()` 会说清是
            # "未以管理员身份运行"还是"令牌造不出来"。只写"不可用"等于让用户
            # 去查 Win32 错误码才知道该做什么。
            if win_token.probe():
                reasons.append(
                    win_token.exec_unavailable_reason() or "受限令牌执行层不可用（原因未知）"
                )
            else:
                reasons.append("受限令牌无法创建（缺少 logon SID，或 advapi32 不可用）")
            # 降级到 `low_il` 前**必须**补打 Low 标签：那是 low_il 的必需前置，
            # 漏掉它会让"降级"变成"降级到一个根本写不了工作区的档位"。
            labeled, lerr = _ensure_workspace_low_label(appcontainer_workspace())
            if not labeled:
                reasons.append(f"工作区无法打 Low 完整性标签: {lerr}")
                return IsolationDecision(want, Level.JOB, reasons, below_floor=True)
            return IsolationDecision(want, Level.LOW_IL, reasons)
        ws = appcontainer_workspace()
        granted, gerr = win_token.grant_path(ws)
        if not granted:
            reasons.append(f"工作区无法授权给合成 SID: {gerr}")
            labeled, lerr = _ensure_workspace_low_label(ws)
            if not labeled:
                reasons.append(f"工作区无法打 Low 完整性标签: {lerr}")
                return IsolationDecision(want, Level.JOB, reasons, below_floor=True)
            return IsolationDecision(want, Level.LOW_IL, reasons)
        if not win_token.desktop_isolation_ready():
            # 已知缺口，告警但**不降级**：low_il 要改用户资产，比"缺桌面隔离"更重。
            # 把丑陋但真实的状态说清楚，好过换一个更脏的档位假装更安全。
            reasons.append(
                "受限进程仍运行在默认桌面（官方建议独立桌面以阻断 PostMessage 跨进程攻击）"
            )

    if want is Level.APPCONTAINER:
        ok, sid, err = win.ensure_appcontainer(win.appcontainer_name("default"))
        if not ok:
            reasons.append(f"AppContainer 不可用: {err or '未知'}")
            return IsolationDecision(want, Level.LOW_IL, reasons)
        uri = appcontainer_workspace()
        granted, gerr = win.grant_appcontainer_path(sid, uri)
        if not granted:
            reasons.append(f"AppContainer 路径授权失败: {gerr}")
            return IsolationDecision(want, Level.LOW_IL, reasons)
        # 刚往工作区写了包 SID ACE ⇒ 缓存失效。
        #
        # `_ensure_workspace_low_label()` 会缓存"已验证过"的路径并跳过后续检查；
        # 若不清掉这个键，`appcontainer → low_il` 的切换会跳过残留 ACE 清理，
        # 于是 low_il 带着一条写不动的 ACL 静默上线（这正是实测到的缺陷）。
        _labeled_workspaces.discard(uri)

        # **执行层尚未接入，所以这里必须降级，不能声称 appcontainer 生效。**
        #
        # 建 AppContainer 进程要 attribute-list（`PROC_THREAD_ATTRIBUTE_SECURITY_
        # CAPABILITIES`），`subprocess` 与 `asyncio.create_subprocess_exec` 都表达
        # 不了，必须 `UpdateProcThreadAttribute` + `CreateProcessW` 走独立路径
        # （`spawn_kwargs` 里那个 `_appcontainer` 键至今没有任何调用方消费）。
        #
        # 原来这里直接 `return IsolationDecision(want, want, reasons)`，于是
        # `/v1/system/isolation`、前端面板与审计日志一致声称 appcontainer 生效，
        # 而实际执行的仍是 low_il —— 正是 `child_applicable_level` 文档点名的
        # "配置、状态、日志三处谎报"。
        reasons.append(
            "AppContainer 执行层尚未接入（建进程需要进程属性列表，subprocess 表达不了）；"
            "按 low_il 执行"
        )
        return IsolationDecision(want, Level.LOW_IL, reasons)

    return IsolationDecision(want, want, reasons)


_job_ok: bool | None = None
_low_il_ok: bool | None = None
_restricted_ok: bool | None = None
#: 已打过标签的 workspace 路径（避免每次 resolve 都跑一次 icacls）
_labeled_workspaces: set[str] = set()


def _ensure_workspace_low_label(path: str) -> tuple[bool, str]:
    """确保工作区带 Low 完整性标签（幂等，结果缓存）。

    这是 `low_il` / `appcontainer` 档位能正常工作的前提：完整性检查是
    **no-write-up**，Low IL 子进程写不了 Medium 对象 —— 包括它自己的工作区。
    """
    if not win.IS_WINDOWS:
        return True, ""  # POSIX 无此概念
    if path in _labeled_workspaces:
        return True, ""
    import os as _os

    if not _os.path.isdir(path):
        try:
            _os.makedirs(path, exist_ok=True)
        except OSError as exc:
            return False, f"无法创建工作区 {path}: {exc}"
    # 先清掉**上一个档位**（appcontainer）留下的包 SID ACE。
    #
    # 实测：带这条 ACE 的目录上，Low IL 子进程创建/修改文件一律 PermissionError；
    # `icacls /remove:g *<包SID>` 之后立即恢复可写。也就是说只要切过 appcontainer
    # 再回头用 low_il，工作区就静默变成"写不了"——配置上看不出来。清理放在打标签
    # 之前，是因为它和完整性标签互相独立，但缺了它标签就毫无意义。
    cleaned, sids, cerr = win.remove_appcontainer_aces(path)
    if not cleaned:
        return False, f"无法清理残留的 AppContainer 授权: {cerr}"
    ok, err = win.set_path_integrity(path)
    if not ok:
        # 重试一次：先给当前用户授完全控制再打标签。
        #
        # 根因（本机实测）：工作区从父目录继承的 ACE 对 `Authenticated Users` 只给
        # `M`，而 `M` 不含 `WRITE_OWNER` —— 改完整性标签恰恰需要它。于是读 ACL 成功、
        # 所有者就是当前用户、也没提权，但 `/setintegritylevel` 一律返回 5（拒绝访问），
        # `low_il` 就静默降级成 `job`。授完权再打即可成功。
        #
        # 只在**当前用户自己的工作区**上授权（调用方保证），不动系统目录。
        granted, gerr = win.grant_current_user_full_control(path)
        if granted:
            ok, err = win.set_path_integrity(path)
        else:
            err = f"{err}；授权重试也失败: {gerr}"
    if ok:
        _labeled_workspaces.add(path)
    return ok, err


def _probe_job() -> bool:
    global _job_ok
    if _job_ok is None:
        job = win.JobObject()
        _job_ok = job.open()
        job.close()
    return _job_ok


def _probe_low_il() -> bool:
    """Low IL 只是"令牌能否打开并设置"，不在本进程真降级。"""
    global _low_il_ok
    if _low_il_ok is None:
        report = win.capability_report()
        _low_il_ok = bool(report.get("low_integrity"))
    return _low_il_ok


def _probe_restricted() -> bool:
    """`restricted` 档能否**真正执行**（不只是"令牌造得出来"）。

    两个条件缺一不可：

    1. `win_token.probe()` —— 受限令牌能创建（logon SID 与 advapi32 都在）；
    2. `win_token.exec_ready()` —— 执行层能真的**以该受限令牌拉起进程**
       并取回 stdout/stderr（`win_launch.smoke_test()` 端到端实测）。

    第 2 条是实测而非开关。本机零点提权的普通账户上它恒为假：
    `CreateProcessAsUserW` 需要 `SeIncreaseQuotaPrivilege`（未分配，err=5），
    `CreateProcessWithTokenW` 需要 `SeImpersonatePrivilege`（未分配，err=1314）。
    此时 `resolve_level` 降级到 `low_il`（仍在底线上，不拒绝启动），
    并把具体原因（`win_token.exec_unavailable_reason()`）写进 `reasons`。
    """
    global _restricted_ok
    if _restricted_ok is None:
        _restricted_ok = win_token.probe() and win_token.exec_ready()
    return _restricted_ok


def appcontainer_workspace() -> str:
    """AppContainer 需要授权的那个目录（工作区根）。

    授权范围刻意收窄到**当前用户的工作区**，不是整个 `SANDBOX_ROOT` —— 否则
    A 用户的沙箱进程能读 B 用户的文件。
    """
    try:
        from app.tools.sandbox import workspace_dir

        return str(workspace_dir())
    except Exception:  # noqa: BLE001
        return os.path.join(os.getcwd(), "workspace")


# ── 子进程侧：把自己降级 ────────────────────────────────────────────

def child_applicable_level(level: Level) -> Level:
    """子进程**实际**能达到的档位（不等于请求档位）。

    为什么需要这个函数：`restricted` 靠 **restricting SID 建令牌**，而那必须发生在
    `CreateProcessAsUserW` 建进程的那一刻（父进程侧）。子进程自己启动后再怎么改令牌
    也无法获得 restricting SID —— 所以子进程入口只能做到 Low IL。

    这意味着 `run_code` 的 worker（普通 `Popen` + 子进程自降级）在 `restricted` 档下
    真实强度是 `low_il`。把它说成 `restricted` 就是谎报：配置、`/v1/system/isolation`、
    日志三处都会声称比实际更强。所以把"子进程能到哪一档"收成单一事实源，供
    `apply_in_child()` 与调用方共同引用。
    """
    if level is Level.RESTRICTED:
        return Level.LOW_IL
    return level


def apply_in_child(level: Level) -> None:
    """在**子进程**入口调用，把自身令牌降到 Low 完整性。

    为什么必须在子进程里做：`SetTokenInformation` 改的是当前进程令牌，
    没有撤销 API（这是设计而非缺陷 —— 它保证降级不可逆）。父进程若调用，
    引擎自身就被降级了。

    传入 `RESTRICTED` 时会降到 `LOW_IL`（见 `child_applicable_level` 的说明），
    并在 stderr 留一行说明 —— 不静默。仍然**高于硬底线**：Low IL 挡得住写工作区
    之外，所以这不是"退回无隔离"。

    失败时不抛异常而是 `os._exit(9)`：一个"以为自己被隔离"的子进程比没有
    隔离更危险。
    """
    applied = child_applicable_level(level)
    if level is Level.RESTRICTED and applied is not level:
        sys.stderr.write(
            "[isolation] restricted 需要父进程建受限令牌，子进程只能做到 low_il；"
            "按 low_il 执行（仍拒绝写工作区之外）\n"
        )
        sys.stderr.flush()
    if applied.rank < Level.LOW_IL.rank or not win.IS_WINDOWS:
        return
    if not win.set_integrity_level(win.INTEGRITY_LOW):
        sys.stderr.write(
            "[isolation] FATAL: 无法把子进程降到 Low 完整性级别；"
            f"拒绝在未隔离状态下执行代码（level={level.value}）\n"
        )
        sys.stderr.flush()
        os._exit(9)


# ── 父进程侧：统一 spawn ────────────────────────────────────────────

def child_launch_argv(
    decision: IsolationDecision,
    command: list[str],
) -> list[str]:
    """把「要在某个档位下执行的命令」包成真正能生效的 argv。

    ## 为什么需要这一层

    `low_il` 只能由**子进程自己**降级（完整性级别改的是当前进程令牌，且不可撤销）。
    但 `shell_exec` 原来只是把进程挂进 Job Object —— 那一步对 `job` / `low_il` /
    `appcontainer` 三档**完全一样**（都只判 `rank < Level.JOB`）。结果就是用户切了
    档位、`/v1/system/isolation` 也如实报告了新档位，可实际执行强度一点没变。

    这里把档位变成**命令行的差异**：`low_il` 及更高档位走自降级入口，`job`/`none`
    保持原样。这样"切换档位"与"执行路径改变"是同一件事。

    ## 各档的落点

    * `none` / `job`：原样返回，只挂 Job Object。
    * `low_il`：走 `_low_il_launcher`，子进程先降到 Low 完整性级别再 exec。
    * `restricted` / `appcontainer`：**赢不过它们各自的父侧机制**，见下。

    `restricted` 由 `shell_exec` 的独立路径（`win_launch.launch_restricted`）处理，
    调用方在进入本函数前已分流；`appcontainer` 的执行层尚未接入，
    `resolve_level` 会先如实降级到 `low_il`，因此到这里只会看到 `low_il`。
    """
    if decision.effective.rank < Level.LOW_IL.rank:
        return list(command)
    if not command:
        return list(command)
    # 用**文件路径**而不是 `-m app.isolation._low_il_launcher`：沙箱子进程的
    # `cwd` 是工作区、`sandboxed_env()` 又清空了 `PYTHONPATH`，`-m` 会解析不到
    # `app` 包（表现为"命令报 ImportError 而不是被隔离执行"）。文件路径不受这两者
    # 影响，入口自己负责把引擎根目录放回 `sys.path`。
    launcher = os.path.join(os.path.dirname(__file__), "_low_il_launcher.py")
    return [
        sys.executable,
        launcher,
        f"--level={decision.effective.value}",
        "--",
        *command,
    ]


def spawn_kwargs(decision: IsolationDecision) -> dict[str, Any]:
    """给 `asyncio.create_subprocess_exec` / `subprocess.Popen` 的额外参数。

    * `low_il`：子进程启动后自己降级（见 `apply_in_child`），父侧**没有**额外参数 ——
      Windows 没有 `preexec_fn` 的等价物，所以由子进程入口统一处理。
    * `appcontainer`：需要 attribute-list，无法用 `subprocess` 表达，走独立路径
      （暂未接入；`resolve_level` 会先降级到 `low_il` 并在 reasons 里说明）。
    """
    kwargs: dict[str, Any] = {}
    if decision.effective is Level.APPCONTAINER:
        kwargs["_appcontainer"] = True
    return kwargs


#: 面向「执行一条命令」的 Job 额度：收束进程树与内存，**不设 CPU 上限**。
#:
#: `cpu_seconds=0` 是刻意为之：Job 的 `PerProcessUserTimeLimit` 计的是**CPU 时间**，
#: 与墙钟超时是两回事。把 CPU 上限压到低于墙钟 timeout，构建类命令会在看起来还有
#: 充裕时间时被内核杀掉，且退出码不带任何解释 —— 比不设上限更难排查。
#: 墙钟超时已由调用方的 `asyncio.wait_for` 兜底，这里补它做不到的两件事：
#: 整棵进程树回收、单进程内存失控拦截。
COMMAND_LIMITS = win.ResourceLimits(
    memory_bytes=2 * 1024**3,
    cpu_seconds=0,
    active_processes=64,
)

#: 长驻 shell 的额度。内存放得比一次性命令宽，因为一个 `npm install` 或 dev server
#: 的峰值远超单次脚本；`KILL_ON_JOB_CLOSE` 保证 session 关闭时不留孤儿进程。
SHELL_LIMITS = win.ResourceLimits(
    memory_bytes=4 * 1024**3,
    cpu_seconds=0,
    active_processes=64,
)


def wrap_async_process(
    proc: Any,
    decision: IsolationDecision,
    limits: win.ResourceLimits | None = None,
) -> win.JobObject | None:
    """把刚 spawn 出来的进程挂进 Job Object。返回 job 供调用方持有到进程结束。

    必须由调用方持有引用：job 句柄一关，`KILL_ON_JOB_CLOSE` 立刻杀光进程树 ——
    这是**特性**（超时/异常时保证不留孤儿），但提前 GC 会导致子进程莫名被杀。

    `limits` 留空则用 `ResourceLimits` 的默认值（512MB / 60s CPU / 16 进程），
    那套默认值面向单次脚本执行；长驻 shell 与构建命令需要各自的额度，否则
    `npm install` 会被 512MB 上限直接打死，而"构建莫名失败"比"没有上限"更难排查。
    """
    if decision.effective.rank < Level.JOB.rank or not win.IS_WINDOWS:
        return None
    job = win.JobObject(limits)
    if not job.open():
        return None
    pid = getattr(proc, "pid", None)
    if pid:
        job.assign(int(pid))
    return job


def require_or_raise(decision: IsolationDecision) -> None:
    """fail-closed 帮手：降级未获同意、或落到硬底线以下时抛错。

    与 `local_env_profile.adapt()` 的旧行为正相反 —— 那边是"悄悄改成 local 继续跑"，
    这边是"降级必须经你同意"。

    **两种拒绝，别混为一谈**：

    * `below_floor=True`：本机拿不出底线档位（如 Low IL 不可设置）。这**不看**
      `allow_downgrade` —— 那个开关的语义是"接受稍弱"，不是"接受没有"。降到这里
      等于没有隔离，而 README 承诺的是"沙箱不可用时拒绝启动"。
    * 普通 `degraded` 且未允许：`appcontainer → low_il` 这类，可经配置放行。
    """
    if decision.below_floor:
        raise RuntimeError(
            f"拒绝在没有隔离的档位下执行（硬底线 {FLOOR_LEVEL.value}）：{decision.describe()}。"
            "这一档不受 SANDBOX_ISOLATION_ALLOW_DOWNGRADE 影响 —— 该开关只放宽"
            "「稍弱的隔离」，不放宽「没有隔离」。修好机制后重启引擎。"
        )
    if decision.degraded and not downgrade_allowed():
        raise RuntimeError(
            f"拒绝在未达标的隔离下执行：{decision.describe()}。"
            f"修好机制，或显式设 {ALLOW_DOWNGRADE_ENV}=true 表示接受降级。"
        )


def record_level_change(
    *,
    action: str,
    requested: str,
    effective: str,
    previous: str | None,
    actor: str = "",
    reason: str = "",
) -> None:
    """记一条档位变更流水到 `logs/isolation_audit.jsonl`（**永不抛异常**）。

    与 `app/plugins/audit.py` 同款契约：审计是安全能力，落盘失败只告警，
    绝不让"记不上账"变成"改不了档位"。但如果**记不上**，日志里会留下 warning，
    所以不会静默丢失。

    记什么、不记什么：只记档位与调用者，不记任何路径或凭据 —— 审计文件本身
    不该成为泄露源。`previous` 为 None 表示"之前没有覆盖"（即走配置文件）。
    """
    try:
        from pathlib import Path

        from app.audit_log import utc_timestamp

        log_dir = Path("logs")
        try:
            from app.config import settings

            log_dir = Path(getattr(settings, "log_dir", "") or "logs")
        except Exception:  # noqa: BLE001 — 配置不可读则用默认目录
            pass

        log_dir.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": utc_timestamp(),
            "action": action,
            "requested": requested,
            "effective": effective,
            "previous": previous,
            "actor": actor,
            "reason": reason,
        }
        with (log_dir / "isolation_audit.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计失败不影响档位变更
        logger.warning("isolation audit log write failed", exc_info=True)


def status() -> dict[str, Any]:
    """`python -m app.isolation status` 的输出。"""
    caps = win.capability_report()
    want = configured_level()
    decision = resolve_level(want)
    return {
        "requested": want.value,
        "effective": decision.effective.value,
        "degraded": decision.degraded,
        "floor": FLOOR_LEVEL.value,
        "below_floor": decision.below_floor,
        "reasons": decision.reasons,
        "downgrade_allowed": downgrade_allowed(),
        #: 前端据此决定选项卡是否置灰：低于底线的档位在 Windows 上不可选。
        "selectable": [lv.value for lv in _ORDER if not win.IS_WINDOWS or lv.rank >= FLOOR_LEVEL.rank],
        "capabilities": caps,
        "appcontainer_name": win.appcontainer_name("default"),
        "appcontainer_workspace": appcontainer_workspace(),
    }


def revoke_all() -> dict[str, Any]:
    """撤销本方案创建的一切：删 AppContainer profile、路径授权、恢复目录完整性标签。

    Job Object 与 Low IL 本身都是进程级的，进程退出即消失，无需显式撤销 ——
    唯一会**留在磁盘上**的就是 AppContainer profile 与工作区的完整性标签（ACL），
    所以这两项必须能一键还原。
    """
    out: dict[str, Any] = {"appcontainer_profile": "", "path_grants": [], "integrity_labels": []}

    # ① 工作区完整性标签还原为 Medium（否则目录会一直是 Low，影响后续未隔离使用）
    ws = appcontainer_workspace()
    if win.IS_WINDOWS:
        ok, msg = win.set_path_integrity(ws, win.INTEGRITY_LABEL_MEDIUM)
        out["integrity_labels"].append(
            {"path": ws, "restored_to": "medium", "ok": ok, "message": msg}
        )
        _labeled_workspaces.discard(ws)

    # ② AppContainer 路径授权 + profile
    name = win.appcontainer_name("default")
    ok, sid, _err = win.ensure_appcontainer(name)
    if ok and sid:
        rok, rmsg = win.remove_appcontainer_path(sid, ws)
        out["path_grants"].append({"path": ws, "removed": rok, "message": rmsg})
    dok, derr = win.delete_appcontainer(name)
    out["appcontainer_profile"] = "已删除" if dok else f"删除失败: {derr}"
    return out


__all__ = [
    "ALLOW_DOWNGRADE_ENV",
    "COMMAND_LIMITS",
    "DEFAULT_LEVEL",
    "FLOOR_LEVEL",
    "LEVEL_ENV",
    "SHELL_LIMITS",
    "IsolationDecision",
    "Level",
    "apply_in_child",
    "child_applicable_level",
    "child_launch_argv",
    "clear_runtime_level",
    "configured_level",
    "downgrade_allowed",
    "parse_level",
    "record_level_change",
    "require_or_raise",
    "resolve_level",
    "revoke_all",
    "runtime_override",
    "set_runtime_level",
    "spawn_kwargs",
    "status",
    "wrap_async_process",
]
