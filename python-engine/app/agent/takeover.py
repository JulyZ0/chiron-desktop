"""Super-agent 接管 —— L0 程序化判定（**零 token** 的那一层）。

**它解决什么问题**

定时接管最容易失败的方式不是"判断错"，而是"根本没判断就在烧钱"：到了点就无脑起一次
完整 run，而那次 run 有九成概率发现"没事可做"。所以接管分成两级，本模块是**第一级**：

``L0 本模块``  纯程序判定（会话锁、用户活跃、到点、预算、熔断）—— 0 token
``L1 接管 run`` 只有 L0 全部放行，才起一次 ``AgentRuntime(unattended=True)``

**为什么必须是纯函数**

与 `app/agent/watchdog.py` 同一取向：判定一旦自己读时钟、读 Redis、查 DB，就无法在测试里
回放"用户正在打字""锁被占用""预算刚好用尽"这些样本。所以 :func:`decide` 只接收调用方
已经取好的事实（:class:`Signal`），不碰任何 IO —— 采集在 `app/queue/takeover_job.py`。

**fail-closed 到"不接管"**

接管是**自主推进**，误判的代价不对称：漏一次只是"这次没巡检"，用户自己会处理；误启动
则是在用户不知情时动他的工作区，还可能顺手花掉预算。所以任何一项事实缺失、非法、
读不到，一律 ``run=False``。

**接管段的边界（与"用户让步"的关系）**

一次巡检 = 一个**接管段**：从 L0 放行开始，到 run 结束 / 用户打断为止。接管段内前端
锁定操作（只留打断按钮）；而"让步"发生在**接管段开始之前** —— 若那一刻用户正在操作，
本次直接跳过（或由调用方弹模态框询问）。两条规则不冲突的前提正是这个先后顺序：
先问"现在能不能接管"，再在接管期间独占。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: 单次接管 run 的 token 估算。只用于保存期校验"预算够不够跑几次"，不参与运行时判定
#: （实时消耗以 DB 里的 `tokens_used_today` 为准）。
ESTIMATED_RUN_TOKENS = 30_000

#: 定时模式。
KIND_ONCE = "once"
KIND_INTERVAL = "interval"
VALID_KINDS: frozenset[str] = frozenset({KIND_ONCE, KIND_INTERVAL})

#: `once` 模式的最远提前量：24 小时。再长就该用 `interval`，否则用户会忘记自己排过队。
MAX_ONCE_HORIZON_SECONDS = 24 * 3600
#: `interval` 的下限：15 分钟。比这更密时，"巡检"会变成"常驻占用"，且成本按次数线性上涨。
MIN_INTERVAL_SECONDS = 15 * 60
#: 最多同时接管几个会话。每个会话一条周期任务，5 个是"成本可控"与"够用"的折中。
MAX_TAKEOVER_SESSIONS = 5

#: 连续失败多少次触发熔断。3 次是"偶发一次 DB 抖动"与"系统性坏掉"的分界。
MELT_AFTER_FAILURES = 3

#: 授权模式中**不允许被无人值守继承**的一档。
#:
#: `yolo` 在 `requires_confirmation` 里对所有级别都返回 False（见 `app/agent/tool_policy.py`），
#: 即"没人确认也照做"。用户选它时坐在机器前、出事能拦；接管跑在用户离开之后，
#: 继承 `yolo` 等于把"我现在盯着"当成"永远可以无人放行"。降级为 `auto` 之后，
#: `delete` / `external` 仍会走二次校验并在无人可批时挂起成待答交互 —— 既不静默执行，
#: 也不静默失败。
UNATTENDED_UNSAFE_MODE = "yolo"
UNATTENDED_FALLBACK_MODE = "auto"

#: 熔断状态。
MELT_BUDGET = "budget"
MELT_ERRORS = "errors"


class SkipReason(StrEnum):
    """本拍不接管的原因。顺序即判定顺序（短路），也用于审计里的稳定字符串。"""

    DISABLED = "disabled"
    NOT_DUE = "not_due"
    COOLDOWN = "cooldown"
    MELTED = "melted"
    BUDGET = "budget"
    BUSY = "busy"
    USER_ACTIVE = "user_active"
    IDLE = "idle"


#: 这些原因属于"正常的、预期内的跳过"，不该计入失败次数。
#:
#: 把它们和真正的异常分开是熔断能否用的前提：用户天天在操作，如果 `user_active`
#: 也算失败，那么正常使用的用户会在三天后把接管"熔断"掉 —— 而真正坏掉的情况
#: 反而被一堆正常跳过淹没。
BENIGN_SKIPS: frozenset[SkipReason] = frozenset(
    {
        SkipReason.DISABLED,
        SkipReason.NOT_DUE,
        SkipReason.COOLDOWN,
        SkipReason.BUDGET,
        SkipReason.BUSY,
        SkipReason.USER_ACTIVE,
        SkipReason.IDLE,
    }
)


@dataclass(frozen=True)
class Signal:
    """一次判定的**全部输入**。全部由调用方取好，本模块不做任何 IO。

    字段缺失或取值非法时的语义统一为"不接管"（见各字段说明），而不是抛异常 ——
    定时任务里抛异常会让整个循环进入异常路径，代价远大于跳过一拍。
    """

    #: 当前时间（秒级 epoch）。由调用方传入而不是内部读钟，测试才能回放。
    now: float = 0.0

    #: 用户级总开关（`superagent_settings.enabled`）。
    user_enabled: bool = False
    #: 会话级开关（`superagent_takeovers.enabled`）。
    session_enabled: bool = False

    #: 定时模式：``once`` / ``interval``。非法值按未到点处理。
    schedule_kind: str = KIND_INTERVAL
    #: ``once`` 模式的到点时刻。None = 没排。早于 now = 已过期（不再执行）。
    once_at: float | None = None
    #: ``interval`` 模式的间隔（秒）。小于下限时按"未到点"处理。
    interval_seconds: int = 0
    #: 上次真正起过 run 的时刻。None = 从未跑过，此时 interval 模式**立即到点**
    #: （用户刚打开开关，期望的是"马上试一次"，而不是"等一个间隔"）。
    last_run_at: float | None = None

    #: 冷却截止时刻。用户打断后由调用方写入，表示"本次接管已取消，此时刻前不再启动"。
    cooldown_until: float | None = None

    #: 熔断状态（``""`` = 未熔断）。非空即跳过，恢复必须由用户手动触发。
    melt_state: str = ""
    #: 连续失败次数。
    consecutive_failures: int = 0

    #: 当日已消耗 token（调用方负责按 `budget_day` 归零）。
    tokens_used_today: int = 0
    #: 当日预算。
    daily_token_budget: int = 0

    #: 会话是否有 run 正在跑（Redis 会话锁被占用）。**让步信号 1**。
    has_active_run: bool = False
    #: 前端最近一次 presence 心跳时刻。None = 没上报。**让步信号 2**。
    presence_at: float | None = None
    #: 会话最后一条消息的时刻。None = 读不到。**让步信号 3**。
    last_message_at: float | None = None
    #: 用户空闲多久之后才认为"可以接管"（秒）。
    idle_minutes: int = 10

    #: 是否还有未完成的工作（任务板有未完成项，或有可续跑的 checkpoint）。
    #: False = 这一拍无事可做，跳过 —— 这是 L0 里省下最多钱的一条。
    has_pending_work: bool = True


@dataclass(frozen=True)
class Decision:
    """判定结果。``detail`` 只用于日志与 UI 提示，不参与后续判定。"""

    run: bool = False
    reason: SkipReason | None = None
    #: 下次到点时刻（供 UI 显示倒计时）。计算不出来时为 None。
    due_at: float | None = None
    detail: str = ""

    @property
    def benign(self) -> bool:
        """这次跳过是否属于"正常跳过"（不计入熔断失败数）。"""
        return self.reason is None or self.reason in BENIGN_SKIPS


def idle_seconds(signal: Signal) -> float:
    """用户已经空闲了多久（秒）。以"能取到的最近活动"为起点。

    三路信号取**最晚**的那个作为"最后活动时刻"：presence 是最新的用户意图（正在看页面），
    消息时间是已经发生的交互，锁占用则说明 run 正在跑。取最晚者最保守 —— 只要任一
    信号显示"刚刚有人在"，空闲时间就趋近 0。

    都取不到时返回 ``+inf``（视为"从未有人活动过"）：这会让预检放行。这不是放水，
    而是因为"三路全空"通常意味着新会话或采集失败，而 `has_pending_work` 与到点判定
    仍然在前一道把关。
    """
    stamps: list[float] = []
    if signal.presence_at is not None:
        stamps.append(signal.presence_at)
    if signal.last_message_at is not None:
        stamps.append(signal.last_message_at)
    if signal.has_active_run:
        # 锁被占用 = 此刻正在被使用，等价于"最后活动时间就是现在"。
        stamps.append(signal.now)
    if not stamps:
        return float("inf")
    return max(0.0, signal.now - max(stamps))


def is_user_active(signal: Signal) -> bool:
    """用户是否正在操作 —— **三路取或**。

    任一路为真即视为活跃。三路各自覆盖不同的漏网场景：

    * ``has_active_run``：已有 run 在跑（最硬的信号，说明后端确实在为用户干活）；
    * ``presence``：用户打开了会话页面（可能正在打字还没提交 —— 只靠锁会漏掉这段）；
    * ``last_message_at``：刚说过话（覆盖"关掉页面立刻走开"之后的那一小段时间）。
    """
    if signal.has_active_run:
        return True
    window = max(1, int(signal.idle_minutes)) * 60
    return idle_seconds(signal) < window


def next_due_at(signal: Signal) -> float | None:
    """下次到点时刻。已到点返回 ``now``；无法计算（配置非法/已过期/已触发过 once）返回 None。"""
    if signal.schedule_kind == KIND_ONCE:
        if signal.once_at is None:
            return None
        # `once` 触发过一次就结束：`last_run_at` 不早于 `once_at` 即视为已执行。
        if signal.last_run_at is not None and signal.last_run_at >= signal.once_at:
            return None
        return signal.once_at
    if signal.schedule_kind == KIND_INTERVAL:
        interval = int(signal.interval_seconds or 0)
        if interval < MIN_INTERVAL_SECONDS:
            return None
        if signal.last_run_at is None:
            # 从未跑过 → 立即到点。用户刚打开开关时期望"马上试一次"。
            return signal.now
        return signal.last_run_at + interval
    return None


def decide(signal: Signal) -> Decision:
    """L0 判定。按"最便宜且最能拦"的顺序短路，任何异常输入都落到不接管。

    顺序的理由：开关类（零成本、确定性）→ 时间类（纯算术）→ 成本类（已有计数）→
    **让步类（需要采集，是这里最贵的三项）** → 工作量类（需要查 DB）。
    把让步放在工作量之前，是因为用户活跃时根本不必知道"有没有活"。
    """
    # ── 1. 开关 ──────────────────────────────────────────────────────
    if not signal.user_enabled:
        return Decision(reason=SkipReason.DISABLED, detail="user-level takeover disabled")
    if not signal.session_enabled:
        return Decision(reason=SkipReason.DISABLED, detail="session-level takeover disabled")

    # ── 2. 熔断与连续失败 ─────────────────────────────────────────────
    if signal.melt_state:
        return Decision(
            reason=SkipReason.MELTED,
            detail=f"melted ({signal.melt_state}); manual reset required",
        )
    if int(signal.consecutive_failures or 0) >= MELT_AFTER_FAILURES:
        return Decision(
            reason=SkipReason.MELTED,
            detail=f"{signal.consecutive_failures} consecutive failures",
        )

    # ── 3. 冷却（用户打断后的"本次已取消"） ───────────────────────────
    if signal.cooldown_until is not None and signal.now < signal.cooldown_until:
        return Decision(
            reason=SkipReason.COOLDOWN,
            due_at=signal.cooldown_until,
            detail="cancelled by user; waiting for cooldown",
        )

    # ── 4. 到点 ──────────────────────────────────────────────────────
    due_at = next_due_at(signal)
    if due_at is None:
        return Decision(reason=SkipReason.NOT_DUE, detail="schedule exhausted or invalid")
    if signal.now < due_at:
        return Decision(reason=SkipReason.NOT_DUE, due_at=due_at, detail="not due yet")

    # ── 5. 预算 ──────────────────────────────────────────────────────
    budget = int(signal.daily_token_budget or 0)
    used = int(signal.tokens_used_today or 0)
    if budget <= 0:
        return Decision(reason=SkipReason.BUDGET, due_at=due_at, detail="budget not configured")
    if used >= budget:
        return Decision(
            reason=SkipReason.BUDGET,
            due_at=due_at,
            detail=f"daily budget exhausted ({used}/{budget})",
        )

    # ── 6. 让步：用户正在操作（三路取或） ─────────────────────────────
    #
    # 先单独判"锁被占"：它与"用户在看页面"在**处置上不同**——有 run 在跑时这一拍
    # 根本不该动（连模态框都不该弹，用户就在等结果）；而用户只是开着页面时，调用方
    # 可以选择弹框询问"现在可以接管吗"。所以原因必须分开，不能都折成 USER_ACTIVE。
    if signal.has_active_run:
        return Decision(
            reason=SkipReason.BUSY,
            due_at=signal.now,  # run 结束后下一拍再看
            detail="another run holds the session lock",
        )
    if is_user_active(signal):
        return Decision(
            reason=SkipReason.USER_ACTIVE,
            due_at=signal.now,  # 用户随时可能停手，下一拍再看
            detail=f"user active (idle {int(idle_seconds(signal))}s < {signal.idle_minutes}m)",
        )

    # ── 7. 无事可做 ──────────────────────────────────────────────────
    if not signal.has_pending_work:
        return Decision(reason=SkipReason.IDLE, due_at=due_at, detail="no pending work")

    return Decision(run=True, due_at=signal.now, detail="proceed")


def should_melt(
    *, consecutive_failures: int, tokens_used_today: int, daily_token_budget: int
) -> str:
    """是否需要熔断，返回熔断状态（``""`` = 不需要）。

    两道闸分开表达，因为恢复方式不同：``budget`` 次日自动恢复（用户什么都不用做），
    ``errors`` 必须人工处理（说明配置或环境坏了，自动重试只会继续失败）。
    """
    if int(consecutive_failures or 0) >= MELT_AFTER_FAILURES:
        return MELT_ERRORS
    budget = int(daily_token_budget or 0)
    if budget > 0 and int(tokens_used_today or 0) >= budget:
        return MELT_BUDGET
    return ""


def safe_tools_mode(raw: object) -> str:
    """把用户当前的工具授权档位换算成**无人值守可继承**的档位。

    唯一实质变化是 ``yolo`` → ``auto``（理由见 :data:`UNATTENDED_UNSAFE_MODE`）。
    其余取值原样返回；非法/缺失回落到 ``auto``（与 `resolve_tools_mode` 的取向一致）。
    """
    if not isinstance(raw, str) or not raw.strip():
        return UNATTENDED_FALLBACK_MODE
    mode = raw.strip()
    if mode == UNATTENDED_UNSAFE_MODE:
        return UNATTENDED_FALLBACK_MODE
    if mode in {"ask", "auto"}:
        return mode
    return UNATTENDED_FALLBACK_MODE


def apply_interrupt(signal: Signal, *, cancelled_at: float) -> float:
    """用户打断后，"本次接管"被取消到什么时候。

    `once` 模式：本次就是全部 —— 直接排到远期（由调用方把 ``enabled`` 置 false），
    这里返回 ``+inf`` 表示"不要再自动启动"。
    `interval` 模式：本次取消，下一个间隔到期再评估（用户打断的是"这一次"，
    不是"以后都不许")。返回下一个正常到点时刻。
    """
    if signal.schedule_kind == KIND_ONCE:
        return float("inf")
    interval = int(signal.interval_seconds or 0)
    if interval < MIN_INTERVAL_SECONDS:
        return float("inf")
    return cancelled_at + interval


def validate_schedule(
    *, schedule_kind: object, once_at: float | None, interval_seconds: object, now: float
) -> list[str]:
    """保存期静态校验。返回**错误**列表（空 = 通过）。

    只做"无论怎么解释都不合法"的检查；"可能不划算"这类判断由调用方降级成警告，
    因为用户有权选择昂贵但正确的配置。
    """
    errors: list[str] = []
    kind = schedule_kind if isinstance(schedule_kind, str) else ""
    if kind not in VALID_KINDS:
        errors.append(f"schedule_kind must be one of {sorted(VALID_KINDS)}")

    if kind == KIND_ONCE:
        if once_at is None:
            errors.append("once_at is required for once schedule")
        elif once_at <= now:
            errors.append("once_at must be in the future")
        elif once_at - now > MAX_ONCE_HORIZON_SECONDS:
            errors.append("once_at must be within 24 hours")
    elif kind == KIND_INTERVAL:
        try:
            interval = int(interval_seconds)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            interval = 0
        if interval < MIN_INTERVAL_SECONDS:
            errors.append(f"interval_seconds must be >= {MIN_INTERVAL_SECONDS}")
    return errors
