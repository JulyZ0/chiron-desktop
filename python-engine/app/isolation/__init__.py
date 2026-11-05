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

from app.isolation import win

logger = logging.getLogger(__name__)

LEVEL_ENV = "SANDBOX_ISOLATION_LEVEL"
ALLOW_DOWNGRADE_ENV = "SANDBOX_ISOLATION_ALLOW_DOWNGRADE"


class Level(enum.StrEnum):
    """隔离档位。顺序即强度顺序（用于比较与"至少要到某档"的判断）。"""

    NONE = "none"
    JOB = "job"
    LOW_IL = "low_il"
    APPCONTAINER = "appcontainer"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)


_ORDER = [Level.NONE, Level.JOB, Level.LOW_IL, Level.APPCONTAINER]

#: 默认档位。`low_il` 在 Windows 上零提权可用且能挡写，是性价比最高的一档；
#: 非 Windows 上自然退化为 `none`（POSIX 继续走 setrlimit）。
DEFAULT_LEVEL = Level.LOW_IL

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

    # Low IL 生效的**必需前置**：工作区必须打 Low 完整性标签。
    #
    # 实测（非管理员账户）：不打标签时 Low IL 子进程连 workspace 都写不了
    # （WRITE/MKDIR 全 DENIED）—— run_code / persistent_shell 会全面失效；
    # 打标签后工作区可写，而**越界写上级目录仍被拒**，引擎自身（Medium）也照常可写。
    if want.rank >= Level.LOW_IL.rank:
        ws = appcontainer_workspace()
        labeled, lerr = _ensure_workspace_low_label(ws)
        if not labeled:
            reasons.append(f"工作区无法打 Low 完整性标签: {lerr}")
            return IsolationDecision(want, Level.JOB, reasons, below_floor=True)

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

    return IsolationDecision(want, want, reasons)


_job_ok: bool | None = None
_low_il_ok: bool | None = None
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

def apply_in_child(level: Level) -> None:
    """在**子进程**入口调用，把自身令牌降到 Low 完整性。

    为什么必须在子进程里做：`SetTokenInformation` 改的是当前进程令牌，
    没有撤销 API（这是设计而非缺陷 —— 它保证降级不可逆）。父进程若调用，
    引擎自身就被降级了。

    失败时不抛异常而是 `os._exit(9)`：一个"以为自己被隔离"的子进程比没有
    隔离更危险。
    """
    if level.rank < Level.LOW_IL.rank or not win.IS_WINDOWS:
        return
    if not win.set_integrity_level(win.INTEGRITY_LOW):
        sys.stderr.write(
            "[isolation] FATAL: 无法把子进程降到 Low 完整性级别；"
            f"拒绝在未隔离状态下执行代码（level={level.value}）\n"
        )
        sys.stderr.flush()
        os._exit(9)


# ── 父进程侧：统一 spawn ────────────────────────────────────────────

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


def wrap_async_process(proc: Any, decision: IsolationDecision) -> win.JobObject | None:
    """把刚 spawn 出来的进程挂进 Job Object。返回 job 供调用方持有到进程结束。

    必须由调用方持有引用：job 句柄一关，`KILL_ON_JOB_CLOSE` 立刻杀光进程树 ——
    这是**特性**（超时/异常时保证不留孤儿），但提前 GC 会导致子进程莫名被杀。
    """
    if decision.effective.rank < Level.JOB.rank or not win.IS_WINDOWS:
        return None
    job = win.JobObject()
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
    "DEFAULT_LEVEL",
    "FLOOR_LEVEL",
    "LEVEL_ENV",
    "IsolationDecision",
    "Level",
    "apply_in_child",
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
