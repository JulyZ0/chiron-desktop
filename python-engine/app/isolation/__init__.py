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
| `container` | 以上所有 + 独立 namespaces/cgroup/caps/seccomp，只挂工作区、无网络 | 需 Kiri（按需下载，不内置） |

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
    #: 一次性容器（Kiri 的 Docker Engine API）：独立 namespaces/cgroup、
    #: 只读根文件系统、CAP_DROP=ALL、seccomp 默认档、无网络。
    #: 跨平台可用的"最强"档位；依赖缺失即拒跑（见 `kiri.py`）。
    CONTAINER = "container"

    @property
    def rank(self) -> int:
        return _ORDER.index(self)


_ORDER = [
    Level.NONE,
    Level.JOB,
    Level.LOW_IL,
    Level.RESTRICTED,
    Level.APPCONTAINER,
    Level.CONTAINER,
]

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

#: **安全底线**：低于它的档位需要用户**显式确认**才能生效。
#:
#: ## 它挡的是"悄悄没有隔离"，不是"用户自己关掉隔离"
#:
#: 两层保护必须分开看：
#:
#: * **配置层**（`.env` / `SANDBOX_ISOLATION_LEVEL`）：低于底线一律抬到 `FLOOR_LEVEL`。
#:   这是一条**环境变量改不动**的保护 —— 否则一次拼写误改、一个继承来的旧 `.env`
#:   就能让整套部署失去隔离，而且没有任何人做过这个决定。
#: * **运行时显式切换**（设置页 / `PUT /v1/system/isolation` 带
#:   `acknowledge_below_floor=true`）：**真的生效**。这是用户本人做的决定，
#:   发生在界面上、有二次确认、会写审计流水、界面持续红标。
#:
#: 把这两件事混成一条硬墙的代价是：需求要求的「无隔离 / 作业对象」在界面上
#: 永远不可达，用户只能改代码 —— 而"悄悄改不动"和"明确地允许"是两回事。
#:
#: 与 `DEFAULT_LEVEL` 的区别仍然关键：默认值可以被配置改掉，底线不会。
#: `SANDBOX_ISOLATION_ALLOW_DOWNGRADE` 的语义也没有变宽：它只放宽"稍弱的隔离"
#: （如 `appcontainer → low_il`），从不让**配置**降到"没有隔离"。
#:
#: 为什么底线选 `low_il` 而不是 `job`：实测（零提权）`job` 只能拦内存爆炸，
#: 挡不住写用户目录；`low_il` 是**当前这台机器上唯一既零提权又真能挡写**的档位。
#: 所以它才是"默认建议的强度"，而不是强制上限。
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

#: 运行时覆盖是否由用户**显式确认过**低于安全底线的那一档。
#:
#: 与 `_runtime_override` 同生命周期：`clear_runtime_level()` 一并清掉，换档位时
#: 由 `set_runtime_level()` 重新赋值。它只回答"这个具体档位是用户确认要的吗"，
#: 不承担"当前有没有隔离"的判断（那是 `below_floor`）。
_runtime_acknowledged: bool = False


def runtime_override() -> Level | None:
    """当前运行时覆盖值（None = 没有覆盖）。供前端显示"这是临时改动"。"""
    return _runtime_override


def runtime_acknowledged() -> bool:
    """当前运行时覆盖是否带用户对低档位的显式确认。"""
    return _runtime_acknowledged


def set_runtime_level(level: Level, *, acknowledge_below_floor: bool = False) -> Level | None:
    """设置运行时档位，返回**被替换掉的旧覆盖值**（供撤销）。

    ``acknowledge_below_floor`` 是"我确实要这个低于安全底线的档位"的显式声明。
    不传它、又请求低于底线的档位时，``resolve_level`` 会把档位抬到底线 ——
    这与"配置改不动底线"是同一条规则。

    本函数只负责"记住用户想要什么"与"他有没有确认过"，是否真的生效仍由
    ``resolve_level`` 统一判定（那里还要探测本机机制）。
    """
    global _runtime_override, _runtime_acknowledged
    previous = _runtime_override
    _runtime_override = level
    _runtime_acknowledged = bool(acknowledge_below_floor)
    logger.info(
        "isolation level overridden at runtime: %s -> %s%s",
        previous,
        level,
        "（已确认低于安全底线）" if _runtime_acknowledged else "",
    )
    return previous


def clear_runtime_level() -> Level | None:
    """撤销运行时覆盖，返回撤销前的值（None 表示本来就没有覆盖）。

    确认标志一并清掉：它是"对某个低档位的决定"，档位撤了，决定也就不存在了。
    如果留着，"当前没有覆盖"却仍带着一份确认，会让下一次配置里的低档位被顺带放行。
    """
    global _runtime_override, _runtime_acknowledged
    previous = _runtime_override
    _runtime_override = None
    _runtime_acknowledged = False
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
    #: 生效档位是否**低于安全底线**（`FLOOR_LEVEL`）。
    #:
    #: 这是**事实判断**，用于界面红标与提示，不直接决定是否拒绝执行。
    below_floor: bool = False
    #: 这个档位是否是**用户显式选择**的结果（而非被动降级）。
    #:
    #: 区分两件完全不同的事：
    #: * `below_floor=True, user_selected=True`：用户自己选了低档位（`job`，
    #:   或确认过的 `none`）。放行 —— 否则"关掉隔离"会连带把工具全废掉。
    #: * `below_floor=True, user_selected=False`：本机机制缺失导致**被动**掉到低档位，
    #:   谁都没同意。由 `require_or_raise` 拒绝执行。
    user_selected: bool = False

    @property
    def degraded(self) -> bool:
        return self.effective.rank < self.requested.rank

    def describe(self) -> str:
        if not self.degraded:
            return f"isolation={self.effective.value}"
        detail = "; ".join(self.reasons) or "原因未知"
        return f"isolation={self.effective.value}（请求 {self.requested.value}，降级: {detail}）"


def _probe_safely(probe: Any, label: str) -> bool:
    """跑一个可用性探测，**异常一律当作"不可用"**。

    为什么不能让它抛出去：`resolve_level()` 是所有档位共同的判据入口 ——
    启动门禁、`GET /v1/system/isolation`、`PUT` 切档、每次 `require_or_raise`
    都会经过它。某一个档位的探测（例如受限令牌要端到端起一个进程、还可能要
    ping 提权 helper）抛异常，会把**所有**档位的判定一起打成失败：

    * 用户想切到 `none`（"我不要隔离"）时收到 500，理由却来自他根本没选的
      `restricted` 探测 —— 诉求与报错毫无关系；
    * 引擎启动门禁变成"某个高档位探测崩了 ⇒ 整个引擎起不来"。

    探测失败的语义本来就只有一个：这一档现在不可用。所以在这里收口，
    并把异常记进日志（丢失它会让"为什么这档突然不可用"无从查起）。
    """
    try:
        return bool(probe())
    except Exception as exc:  # noqa: BLE001 — 见 docstring：探测失败 = 该档不可用
        logger.warning("isolation probe %s failed: %s", label, exc)
        return False


def resolve_level(requested: Level | None = None) -> IsolationDecision:
    """把"请求的档位"落到"本机真能提供的档位"。

    **不静默降级**：降级时 `reasons` 必非空，调用方据此告警或拒绝执行。

    三条不同的约束，别混为一谈：

    * 请求值**低于** `FLOOR_LEVEL` 且**没有用户确认**（配置里写了 `none`/`job`）
      ⇒ Windows 上按底线执行，并在 `reasons` 说明。这是"配置改不动底线"。
    * 请求值**低于** `FLOOR_LEVEL` 但**用户显式确认过**（设置页二次确认，
      落到 `_runtime_acknowledged`）⇒ 如实生效，标 `below_floor=True` 供界面红标，
      不拒绝执行。这是"用户自己关掉隔离"。
    * 本机机制缺失导致实际档位**落到** `FLOOR_LEVEL` 以下 ⇒ 标记 `below_floor`，
      `require_or_raise` 会拒绝执行，**不看** `allow_downgrade`。
    """
    want = requested or configured_level()
    reasons: list[str] = []
    #: 这个低档位是否是**用户通过运行时开关显式选的**（而非配置层写死、也非被动跌破）。
    #:
    #: 只有它为真时 `require_or_raise` 才放行低于底线的档位。判据绑在
    #: `_runtime_override` 上而不是"全局有没有确认过"：确认是对某个档位做的决定，
    #: 换档位就失效。否则用户确认过一次 `none`，之后配置里任何低档位都会被顺带放行
    #: —— 那等于确认一次、永久有效。
    user_selected = False

    # ⓪ `container` 挡位：跨平台，但只认 Engine API over Unix socket。
    #
    # 依赖缺失 ⇒ **effective=NONE + below_floor=True**：启动门禁会拒绝启动、
    # 执行侧也会拒跑（`require_or_raise` 不看 allow_downgrade）。这正是 H2 的
    # "沙箱不可用即拒绝"，不是静默降级到无命名空间的本地执行。
    if want is Level.CONTAINER:
        from app.isolation import kiri

        if win.IS_WINDOWS:
            return IsolationDecision(
                want,
                Level.NONE,
                ["Windows 的 Kiri 通过命名管道提供 Docker API，本版未实现该驱动"],
                below_floor=True,
            )
        if kiri.available():
            return IsolationDecision(want, Level.CONTAINER, [])
        return IsolationDecision(
            want, Level.NONE, [kiri.unavailable_reason()], below_floor=True
        )

    if not win.IS_WINDOWS:
        # Linux 侧保持既有 local 实现（POSIX 走 setrlimit），不受硬底线约束。
        return IsolationDecision(want, Level.NONE, ["非 Windows 平台；POSIX 走 setrlimit"])

    # ① 低于安全底线：**运行时显式选择**才生效，配置层一律抬到底线。
    #
    # 三种来源必须分开，否则会出现"谁都不同意却真的没隔离"：
    #
    # * 配置里写了 `none`/`job`（`_runtime_override is None`）⇒ 抬到底线。
    #   环境变量改不动底线 —— 否则一个拼写误改就能让整套部署失去隔离。
    # * 用户在设置页选了 `job` ⇒ **直接生效**（`user_selected=True`）。
    #   `job` 仍有 Job Object 隔离，只是挡不住写用户目录；不值得为它加一道
    #   二次确认，否则"切一下看看"成本过高。
    # * 用户在设置页选了 `none` ⇒ 需要**这个具体档位**的显式确认
    #   （`acknowledge_below_floor=true` 落到 `_runtime_acknowledged`），
    #   没确认就抬到底线并说明。这是"用户自己关掉隔离"，不是配置能做的事。
    if want.rank < FLOOR_LEVEL.rank:
        if _runtime_override is want and (
            want.rank >= Level.JOB.rank or _runtime_acknowledged
        ):
            reasons.append(
                f"低于安全底线 {FLOOR_LEVEL.value} 的档位 {want.value} 已由用户显式选择"
                + ("（并已确认）" if _runtime_acknowledged else "")
            )
            user_selected = True
        elif _runtime_override is want:
            reasons.append(
                f"请求档位 {want.value} 低于安全底线 {FLOOR_LEVEL.value}，"
                "但未做显式确认，按底线执行"
            )
            want = FLOOR_LEVEL
        else:
            reasons.append(f"请求档位 {want.value} 低于安全底线 {FLOOR_LEVEL.value}，按底线执行")
            want = FLOOR_LEVEL

    # Job Object 是所有高于 none 的档位共同的地基
    if want.rank >= Level.JOB.rank and not _probe_safely(_probe_job, "job"):
        reasons.append("Job Object 不可用")
        return IsolationDecision(want, Level.NONE, reasons, below_floor=True)

    if want.rank >= Level.LOW_IL.rank and not _probe_safely(_probe_low_il, "low_il"):
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
        if not _probe_safely(_probe_restricted, "restricted"):
            # 把**具体**原因写进 reasons：`exec_unavailable_reason()` 会说清是
            # "未以管理员身份运行"还是"令牌造不出来"。只写"不可用"等于让用户
            # 去查 Win32 错误码才知道该做什么。
            if _probe_safely(win_token.probe, "restricted-token"):
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

        # 执行层是**实测**判定的，不是开关：`appcontainer_exec_ready()` 会真的在
        # 包 SID 下起一个进程并读回输出（`win_launch.appcontainer_smoke_test`）。
        #
        # 此前这里无条件降级，理由是"建 AppContainer 进程需要进程属性列表，
        # `subprocess` 表达不了" —— 那个缺口现由 `win_launch.launch_appcontainer`
        # / `open_appcontainer_session` 补上（`STARTUPINFOEXW` + `CreateProcessW`）。
        #
        # 与 `restricted` 的关键差异：AppContainer **不需要另一个用户令牌**，
        # 因此不要求 `SeIncreaseQuotaPrivilege` —— 普通（非提权）账户也能用。
        # 本机实测：非提权下 `restricted` 恒 `err=5`（`CreateProcessAsUserW` 起不来），
        # 而 AppContainer 冒烟测试通过。所以它才是这台机器上真正可达的高档位。
        if not _probe_safely(_probe_appcontainer, "appcontainer"):
            reasons.append(
                _appcontainer_probe_error() or "AppContainer 执行层不可用（原因未知）"
            )
            labeled, lerr = _ensure_workspace_low_label(uri)
            if not labeled:
                reasons.append(f"工作区无法打 Low 完整性标签: {lerr}")
                return IsolationDecision(want, Level.JOB, reasons, below_floor=True)
            return IsolationDecision(want, Level.LOW_IL, reasons)

        return IsolationDecision(want, want, reasons)

    # 走到这里说明请求的档位如实生效了（没有发生降级）。
    #
    # `below_floor` 是**事实**（界面红标），`user_selected` 是**许可**
    # （`require_or_raise` 是否放行）。用户显式选的 `none`/`job` 在这里第一次
    # 同时带上两者；配置层写死的低档位在 ① 已被抬到底线，走不到这里。
    return IsolationDecision(
        want,
        want,
        reasons,
        below_floor=want.rank < FLOOR_LEVEL.rank,
        user_selected=user_selected,
    )


_job_ok: bool | None = None
_low_il_ok: bool | None = None
_restricted_ok: bool | None = None
#: `appcontainer` 执行层是否可用（端到端冒烟结果，不是配置项）。
_appcontainer_ok: bool | None = None
#: 已打过标签的 workspace 路径（避免每次 resolve 都跑一次 icacls）
_labeled_workspaces: set[str] = set()


def reset_probes() -> None:
    """清掉**档位可用性**的探测缓存，让下一次 `status()` 重新实测。

    为什么需要：本模块的 `_restricted_ok` 与 `win_launch` 的 `_smoke_ok` 是
    **两份独立缓存**。用户点"启用受限令牌助手"之后，helper 起来了、
    `win_launch` 那边也清过了，但这里的 `_restricted_ok` 仍是 `False` ——
    于是 `selectable` 里依旧没有 `restricted`，界面上表现成
    "UAC 授权成功了，档位却还是灰的"。

    同样的道理适用于撤销授权（helper 停了要立刻变回不可选）。所以两个方向
    都在改动后调一次，而不是只清一边。
    """
    global _job_ok, _low_il_ok, _restricted_ok, _appcontainer_ok
    _job_ok = None
    _low_il_ok = None
    _restricted_ok = None
    _appcontainer_ok = None
    try:
        from app.isolation import kiri

        kiri.reset_probe()
    except Exception as exc:  # noqa: BLE001 — 运行时探测复位失败不影响其它档位
        logger.warning("kiri probe reset failed: %s", exc)
    try:
        from app.isolation import win_launch

        win_launch.reset_probe()
    except Exception as exc:  # noqa: BLE001 — 复位失败不该让状态查询整个失败
        logger.warning("isolation probe cache reset failed: %s", exc)


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


def _probe_appcontainer() -> bool:
    """`appcontainer` 档能否**真正执行** —— 端到端实测，不是开关。

    判据是 `win_launch.appcontainer_smoke_test()`：真的在包 SID 下起一个进程
    并把 stdout/stderr 读回来。只验证"profile 建得出来"远远不够 ——
    attribute list、环境块、管道继承、路径授权任一处错了，进程都起不来。

    关键事实：这条路**不需要提权**。`CreateProcessW` + `STARTUPINFOEXW` 不涉及
    `SeIncreaseQuotaPrivilege`，所以普通账户可用；而 `restricted` 走的
    `CreateProcessAsUserW` 需要该特权，非提权下恒 `err=5`。本机实测：
    appcontainer 冒烟通过、restricted 起不来 —— 因此 appcontainer 是本机
    **真正可达**的高档位。

    `win_launch` 反向 import 本模块，所以只能在这里延迟导入（顶层导入会成环）。
    """
    global _appcontainer_ok
    if _appcontainer_ok is None:
        try:
            from app.isolation import win_launch

            _appcontainer_ok = win_launch.appcontainer_exec_ready()
        except Exception as exc:  # noqa: BLE001 — 探测本身失败按"不可用"
            logger.warning("appcontainer probe failed: %s", exc)
            _appcontainer_ok = False
    return _appcontainer_ok


def _appcontainer_probe_error() -> str:
    """AppContainer 探测失败的具体原因（给 `reasons` 用，避免只说"不可用"）。"""
    try:
        from app.isolation import win_launch

        return win_launch.appcontainer_exec_error()
    except Exception:  # noqa: BLE001
        return ""


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

    `appcontainer` 同理：包 SID 由**父进程**在 `CreateProcessW` 的
    `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES` 里指定，子进程无法给自己套包。
    `run_code` 的 worker 是普通 `Popen` + 子进程自降级，因此真实强度只到 `low_il`
    （Low IL 仍在硬底线之上：写不了工作区之外，不是"无隔离"）。

    把它说成 `restricted` / `appcontainer` 就是谎报：配置、`/v1/system/isolation`、
    日志三处都会声称比实际更强。所以把"子进程能到哪一档"收成单一事实源，供
    `apply_in_child()` 与调用方共同引用。
    """
    if level in (Level.RESTRICTED, Level.APPCONTAINER):
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

    **恒为空字典**，这是刻意的：两档自建路径都无法用 `subprocess` 表达。

    * `low_il` / `job`：子进程启动后自己降级（见 `apply_in_child`），父侧没有参数 ——
      Windows 没有 `preexec_fn` 的等价物。
    * `restricted`：要受限令牌，必须 `CreateProcessAsUserW`
      （`win_launch.launch_restricted` / `open_restricted_session`）。
    * `appcontainer`：要进程属性列表（`STARTUPINFOEXW` +
      `PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES`），必须 `CreateProcessW`
      （`win_launch.launch_appcontainer` / `open_appcontainer_session`）。

    此前这里塞了一个 `_appcontainer=True` 键，**没有任何消费者** ——
    一旦有人把它展开进 `create_subprocess_exec(...)`，会直接
    `TypeError: unexpected keyword argument`；更糟的是如果被某个 `**kwargs` 吞掉，
    档位显示 appcontainer 而进程根本不在包里。宁可不返回键，也不留这个陷阱。
    """
    _ = decision  # 签名保留：调用方按 decision 决策，语义仍要一致
    return {}


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


def attach_job(
    proc: Any,
    limits: win.ResourceLimits | None = None,
    *,
    label: str = "",
) -> win.JobObject | None:
    """把刚 spawn 的子进程挂进 Job Object，**失败只告警、绝不抛**。

    与 :func:`wrap_async_process` 的区别只有一条：这个版本自己解析档位、自己吞异常。
    用于"隔离是增强、不是前提"的那些 spawn 点 —— 插件、hook、MCP server、git、
    后台命令。那些地方如果因为挂 job 失败就让进程起不来，等于把"防孤儿"的收益
    换成了"功能不可用"，不划算。

    为什么必须有这一层：`wrap_async_process` 需要调用方先 `resolve_level()`，
    而 `resolve_level()` 在配置拼错时会抛 `RuntimeError`。九个 spawn 点各写一遍
    try/except 与档位解析，必然有几处漏写 —— 漏掉的那一处就是孤儿进程的来源。

    `label` 只用于日志定位（哪个子系统没挂上），不参与任何判定。
    """
    try:
        return wrap_async_process(proc, resolve_level(), limits)
    except Exception as exc:  # noqa: BLE001 — 见 docstring：隔离失败不该阻断功能
        logger.warning("job object attach failed (%s): %s", label or "unknown", exc)
        return None


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
    """fail-closed 帮手：降级未获同意、或**被动**落到安全底线以下时抛错。

    与 `local_env_profile.adapt()` 的旧行为正相反 —— 那边是"悄悄改成 local 继续跑"，
    这边是"降级必须经你同意"。

    **三种情况，别混为一谈**：

    * `below_floor` 且 `user_selected`：用户在设置页自己选的 `none`/`job`。
      放行 —— 否则"关掉隔离"会连带把工具全废掉，那不是保护，是罢工。
    * `below_floor` 且**未**由用户选择：本机拿不出底线档位（如 Low IL 不可设置），
      或配置里写了低档位却没经确认就被抬升后仍缺机制。拒绝，且**不看**
      `allow_downgrade` —— 那个开关的语义是"接受稍弱"，不是"接受没有"。
      这种"谁都没同意"的降级正是 README 里「沙箱不可用时拒绝启动，不偷偷回退」
      要挡的情况。
    * 普通 `degraded` 且未允许：`appcontainer → low_il` 这类，可经配置放行。
    """
    if decision.below_floor and not decision.user_selected:
        raise RuntimeError(
            f"拒绝在没有隔离的档位下执行（安全底线 {FLOOR_LEVEL.value}）：{decision.describe()}。"
            "这一档不受 SANDBOX_ISOLATION_ALLOW_DOWNGRADE 影响 —— 该开关只放宽"
            "「稍弱的隔离」，不放宽「没有隔离」。修好机制后重启引擎；"
            "若确实要关闭隔离，请在设置页显式确认（该决定会写审计流水）。"
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
        from app.audit_log import utc_timestamp
        from app.paths import log_dir as resolve_log_dir

        configured = ""
        data_root = ""
        try:
            from app.config import settings

            configured = getattr(settings, "log_dir", "")
            data_root = getattr(settings, "data_dir", "")
        except Exception:  # noqa: BLE001 — 配置不可读则用默认目录
            pass

        # 绝对路径（见 app/paths.py）：以前这里是相对 CWD 的 `logs`，打包态 CWD
        # 只读，mkdir 失败被吞掉 ⇒ 档位变更流水静默全丢。
        log_dir = resolve_log_dir(configured, data_root)

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


def _level_executable(level: Level) -> bool:
    """本机能否**真正执行**该档位（不只是"高于底线"）。

    ## 为什么必须单独判一次

    `selectable` 此前只按 `rank >= FLOOR_LEVEL` 过滤，于是面板把本机**根本做不到**
    的档位也列成"可选"。真机实测（非管理员账户）：

        selectable = ["low_il", "restricted", "appcontainer"]
        PUT restricted → requested=restricted, effective=low_il
        reasons: 受限令牌执行层不可用（CreateProcessAsUserW 失败 err=5）；
                 需要以管理员身份运行引擎以获得 SeIncreaseQuotaPrivilege

    用户选了"受限令牌"，界面回一句"已切换"，实际强度一点没变 —— 这就是
    「无法真实切换受限令牌」的全部原因。让 UI 只列出** attainable** 的档位，
    比事后解释"你选的那个没生效"要诚实得多。

    探测本身是端到端冒烟（`_probe_restricted` / `_probe_appcontainer` 会真的起一个
    子进程读回输出），但结果都有缓存（`_restricted_ok` / `_appcontainer_ok`），
    所以只有第一次查询稍慢，后续是常数时间。
    """
    if level is Level.CONTAINER:
        from app.isolation import kiri

        return (not win.IS_WINDOWS) and kiri.available()
    if not win.IS_WINDOWS:
        # 非 Windows：只有 `none`（setrlimit 基线）与 `container` 是本机真能执行的；
        # Windows 原生机（job/low_il/restricted/appcontainer）在这里没有实现。
        return level is Level.NONE
    if level.rank < Level.JOB.rank:
        # none：没有可"执行"的机制，它本身就是"不隔离"
        return True
    try:
        if level is Level.APPCONTAINER:
            return _probe_appcontainer()
        if level is Level.RESTRICTED:
            return _probe_restricted()
        if level is Level.LOW_IL:
            return _probe_low_il()
        return _probe_job()
    except Exception as exc:  # noqa: BLE001 — 探测失败按"不可用"：宁可少列，也不要撒谎
        logger.warning("isolation probe failed for %s: %s", level.value, exc)
        return False


def _level_unavailable_reason(level: Level) -> str:
    """该档位不可选的**机器原因**（前端悬停/置灰时显示）。

    这里**只**回答"本机缺什么机制"。低于安全底线不再等于不可选 —— 用户可以在
    设置页显式选择（`job` 直接生效，`none` 需二次确认），所以低于底线本身
    不是"不可选"的理由，也不需要文案。

    反过来，某个低档位若真的**不可执行**（如 Job Object 创建失败），仍要给机器
    原因，否则用户会以为只是策略限制。
    """
    if level is Level.CONTAINER:
        from app.isolation import kiri

        if win.IS_WINDOWS:
            return "Windows 的 Kiri 通过命名管道提供 Docker API，本版未实现该驱动"
        return kiri.unavailable_reason()
    if not win.IS_WINDOWS:
        return ""
    try:
        if level is Level.RESTRICTED:
            return win_token.exec_unavailable_reason() or "受限令牌执行层不可用"
        if level is Level.APPCONTAINER:
            return _appcontainer_probe_error() or "AppContainer 执行层不可用"
        if level is Level.LOW_IL:
            return "Low 完整性级别不可设置"
        return "Job Object 不可用"
    except Exception:  # noqa: BLE001
        return ""


def _container_status() -> dict[str, Any]:
    """容器挡位的运行时快照（不在支持的平台上也要返回可读原因）。"""
    try:
        from app.isolation import kiri

        return kiri.status()
    except Exception as exc:  # noqa: BLE001 — 状态查询失败不该让整页失败
        return {
            "version": "",
            "supported": False,
            "installed": False,
            "socket": "",
            "engine": None,
            "reason": f"运行时状态不可读：{exc}",
            "asset": None,
            "install_hint": "",
        }


def status() -> dict[str, Any]:
    """`python -m app.isolation status` 的输出。"""
    caps = win.capability_report()
    want = configured_level()
    decision = resolve_level(want)
    # 受限令牌的探活缓存可能是负结果（引擎启动时 helper 还没起）。若请求 restricted
    # 却降级、而 helper 现在可用，重探一次再出结论 —— 否则用户"启用助手成功"后
    # 面板会一直显示档位不可选，直到某次端点恰好清了缓存。
    if want is Level.RESTRICTED and decision.effective is not Level.RESTRICTED:
        try:
            from app.isolation import win_launch

            if win_launch.helper_available()[0]:
                reset_probes()
                decision = resolve_level(want)
        except Exception as exc:  # noqa: BLE001 — 重探失败沿用原结论
            logger.warning("isolation restricted re-probe skipped: %s", exc)
    # 只列**本机真能做到**的档位。低于安全底线的档位也列出来 —— 它们现在
    # 可以在设置页被显式选择（`job` 直接生效，`none` 需二次确认）；
    # 挡在墙外会让"关掉隔离"变成一个只能改代码才可达的选项。
    #
    # 但"能做"仍要真探测：把做不到的列成"可选"，等于让用户对着一个永远不生效的
    # 开关操作（见 `_level_executable`）。
    selectable = [lv for lv in _ORDER if _level_executable(lv)]
    selectable_values = [lv.value for lv in selectable]
    return {
        "requested": want.value,
        "effective": decision.effective.value,
        "degraded": decision.degraded,
        "floor": FLOOR_LEVEL.value,
        "below_floor": decision.below_floor,
        #: 生效档位是否由用户显式选择（而非被动降级）。前端据此区分
        #: "你自己关的"与"机制缺失导致跌破"，后者要报错。
        "user_selected": decision.user_selected,
        "reasons": decision.reasons,
        "downgrade_allowed": downgrade_allowed(),
        #: 前端据此决定选项卡是否置灰。
        "selectable": selectable_values,
        #: 选择这些档位时必须先二次确认。
        #:
        #: 只有 `none`（完全没有边界）在此列。`job` 仍有 Job Object 边界，
        #: 为它加门槛会把"切一下看看"变得很贵，却换不来对应的安全。
        "acknowledge_required": [
            lv.value for lv in selectable if lv.rank < Level.JOB.rank
        ],
        #: 不可选档位的**原因**，按档位名索引。只描述"本机缺机制"，
        #: 后者要告诉用户去做什么（如"以管理员身份运行"），而不是让他去改配置。
        "unavailable_reasons": {
            lv.value: _level_unavailable_reason(lv)
            for lv in _ORDER
            if lv.value not in selectable_values
        },
        "capabilities": caps,
        #: 容器挡位的运行时快照（是否装了 Kiri、socket、可下载资产、安装命令）。
        #: 前端据此显示"未安装/可下载/已就绪"，而不是只给一个灰掉的档位。
        "container": _container_status(),
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
    "reset_probes",
    "resolve_level",
    "revoke_all",
    "runtime_override",
    "set_runtime_level",
    "spawn_kwargs",
    "status",
    "wrap_async_process",
    "attach_job",
]
