"""任务预算 —— 主 Agent 与子 Agent **共用一套**实现。

**为什么需要它**

子 Agent 早就有 tokens / wall / cost 三轴预算（`SUBAGENT_MAX_*`），主 Agent 却只有 `max_turns`
一个轮次上限：一轮里可以塞任意多次工具调用，token 与成本没有任何引擎侧上限（唯一兜底是网关
的回合超时）。于是"主 Agent 卡在循环里烧钱"根本没有闸门。

**五轴**

| 轴 | 含义 | 主 Agent 默认 |
|---|---|---|
| ``steps`` | 工具调用总次数 —— `max_turns` 是"轮"，一轮内可调很多次工具，这是最直接的漏口 | 60 |
| ``turns`` | LLM 往返轮数（即既有 `max_turns`，保留） | 0（由 task.max_turns 管） |
| ``wall``  | 墙钟（秒） | 0 = 不限（由网关回合超时兜底） |
| ``tokens`` | input + output 累计 | 400k |
| ``cost``  | 按 provider 计价换算 | 0 = 不限 |

**语义（与子 Agent 一致）**：超限是"**失败**"而不是"取消" —— 走失败收尾
（`status=failed`、`error=budget_exceeded:<轴>`），前端能看到原因，而不是静默中断。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

#: 主 Agent 步数上限：一次代码任务（读 → 改 → 跑测试 → 修）常态就接近 60 次工具调用，
#: 再加上排查失败的那几轮，60 会先于 token 轴撞上。放到 150 后，真正失控仍由 tokens 轴拦住
#: （400k 在那个调用量级下是更贴切的闸门），可用 `AGENT_MAX_STEPS` 覆盖。
DEFAULT_MAX_STEPS = 150
#: 主 Agent token 上限的**下限**：只拦"明显失控"（正常长任务在数十万 token 量级）。
#:
#: 真正的上限按**模型窗口**自适应（见 `TaskBudget.scale_to_window`）：每轮都会把完整历史
#: 重发一次，累计 input 随轮次近似**平方**增长，固定 400k 会让 40 轮的长任务在十几轮就撞线。
#: 窗口大的模型本来就有额度跑更多轮，没有理由和 128k 的小模型共用同一条线。
DEFAULT_MAX_TOKENS = 400_000
#: 窗口自适应的倍数：上限 = 窗口 × 该倍数（再与 `DEFAULT_MAX_TOKENS` 取大）。
#: 一次长任务的重发总量在窗口的同一数量级，取 3 倍留出"读写 + 反复试错"的余量。
DEFAULT_TOKEN_WINDOW_RATIO = 3.0
#: 自动续段的上限：token 轴越界不再直接失败，而是新开一段额度继续跑。
#: 它必须有个头 —— 没有上限的"续段"就是没有闸门，正是这条预算要防的事。
DEFAULT_MAX_SEGMENTS = 8
#: wall 默认不限：该不该限时由调用方 / 网关的回合超时决定，引擎侧不重复限制
DEFAULT_MAX_SECONDS = 0
#: 成本默认不限：计价口径随 provider 变，交给上层显式传
DEFAULT_MAX_COST = 0.0


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@dataclass
class TaskBudget:
    """一次 agent run 的预算（每轴 0 = 该轴不限）。"""

    #: 工具调用总次数上限。**dataclass 默认 0（不限）**，由 :func:`from_env` 决定默认值：
    #: 主 Agent 60、子 Agent 0（子 Agent 的步数由 `max_turns` 与看门狗管，不需要这一轴）。
    steps: int = 0
    turns: int = 0
    tokens: int = DEFAULT_MAX_TOKENS
    wall: int = DEFAULT_MAX_SECONDS
    cost: float = DEFAULT_MAX_COST
    started_at: float = field(default_factory=time.time)
    #: 单段 token 额度（`tokens` 的基准值）。续段时用它把 `tokens` 抬高一个段，
    #: 从而"新开一段额度"，而不是重置已用量 —— 已用量同时是计费口径（`done` 事件），
    #: 清零会让网关少扣费，所以只抬上限、不动用量。
    segment_tokens: int = 0
    #: 还能续几段。0 = 不续（子 Agent / 显式禁用）：越界即失败。
    segments_left: int = 0
    #: token 上限是否由**调用方显式指定**（构造参数或 `*_RUN_MAX_TOKENS`）。
    #:
    #: 显式指定 = 用户要的是一条硬线，自适应与续段都**不得**越过它：
    #: 把 `AGENT_RUN_MAX_TOKENS=50k` 悄悄放大成窗口的 3 倍，等于这个配置项没有生效。
    tokens_pinned: bool = False

    def scale_to_window(
        self, window: int, ratio: float = DEFAULT_TOKEN_WINDOW_RATIO
    ) -> None:
        """把 tokens 轴的上限按**模型窗口**放大（只放大，不缩小）。

        为什么不能是固定值：每轮都重发完整历史，累计 input 随轮次近似平方增长。
        128k 窗口的模型十几轮就到百万量级，1M 窗口的模型同样轮数只用到小头 ——
        两者共用一条线，必然是小窗口的先撞线、大窗口的白白受限。

        只放大不缩小：显式配置（`AGENT_RUN_MAX_TOKENS`）与默认下限仍然是地板，
        窗口表查不到或填了离谱小值时不会把上限压到正常任务跑不完的水平。
        **显式配置则完全不动**（`tokens_pinned`）：那是用户要的一条硬线，悄悄放大
        等于这个配置项没有生效。
        """
        if self.tokens_pinned:
            return
        try:
            span = int(window or 0)
        except (TypeError, ValueError):
            return
        if span <= 0:
            return
        scaled = int(span * float(ratio or DEFAULT_TOKEN_WINDOW_RATIO))
        if scaled > self.tokens:
            self.tokens = scaled
        # 续段步长锚定**窗口**，不锚定当前上限：`scale_to_window` 每轮开头都会调用，
        # 若每轮都拿"当前 tokens"当步长，续段抬高上限后下一轮步长也跟着抬高，
        # 八段下来就是复利膨胀 —— 那等于没有闸门。所以只在该 run 首次确定后固定。
        if self.segment_tokens <= 0:
            self.segment_tokens = scaled if scaled > 0 else self.tokens

    def grant_next_segment(self) -> bool:
        """tokens 轴越界时开一段新额度。返回 False 表示段数已用尽（该真失败了）。

        **只抬上限**：`_tokens_spent()` 继续累加，所以计费与线账看到的仍是全程用量，
        而这条 run 又获得了一个窗口量级的预算继续跑 —— 这正是"长任务能跑完"要有的事。
        """
        if self.segments_left <= 0:
            return False
        # 显式配置的额度是硬线：连"续段"也不能越过它，否则等于配置项没生效。
        # 这条 run 会以 `budget_exceeded:tokens` 收尾，用户能看到原因。
        if self.tokens_pinned:
            return False
        step = int(self.segment_tokens or self.tokens)
        if step <= 0:
            return False
        self.segments_left -= 1
        self.tokens += step
        return True

    @property
    def enabled(self) -> bool:
        return bool(self.steps or self.turns or self.tokens or self.wall or self.cost)

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def exceeded(
        self, *, steps: int = 0, turns: int = 0, tokens: int = 0, cost: float = 0.0
    ) -> str:
        """返回越界的轴名（``steps`` / ``turns`` / ``tokens`` / ``wall`` / ``cost``）；未越界返回空串。

        判定顺序固定（steps → turns → tokens → wall → cost）：同时越界时给出"最直接可归因"的
        那个轴，便于前端解释"为什么被中止"。
        """
        if self.steps and steps >= self.steps:
            return "steps"
        if self.turns and turns >= self.turns:
            return "turns"
        if self.tokens and tokens >= self.tokens:
            return "tokens"
        if self.wall and self.elapsed() >= self.wall:
            return "wall"
        if self.cost and cost >= self.cost:
            return "cost"
        return ""

    def describe(self) -> str:
        """人类可读摘要（写日志 / 事件，便于排障）。"""
        parts = []
        if self.steps:
            parts.append(f"steps<={self.steps}")
        if self.turns:
            parts.append(f"turns<={self.turns}")
        if self.tokens:
            parts.append(f"tokens<={self.tokens}")
        if self.wall:
            parts.append(f"wall<={self.wall}s")
        if self.cost:
            parts.append(f"cost<={self.cost}")
        return ",".join(parts) or "none"


class BudgetExceeded(RuntimeError):  # noqa: N818 — 名字由 subagent/budget.py 的 re-export 与 runner 的 error 文本固定，改名属 API 变更
    """预算越界。消息即 ``budget_exceeded:<轴>`` —— 会被 runner 写进 run 的 error。"""

    def __init__(self, axis: str):
        super().__init__(f"budget_exceeded:{axis}")
        self.axis = axis


def from_env(
    prefix: str = "AGENT",
    *,
    steps: int = 0,
    turns: int = 0,
    tokens: int = 0,
    seconds: int = 0,
    cost: float = 0.0,
    default_tokens: int = DEFAULT_MAX_TOKENS,
    default_steps: int = DEFAULT_MAX_STEPS,
    max_segments: int = 0,
) -> TaskBudget:
    """按「显式参数 > 环境变量（``{prefix}_MAX_*``）> 默认」构造预算。

    显式传 0 表示"用默认"（调用方不传就是默认策略）。主 Agent 用默认前缀 ``AGENT``；
    子 Agent 传 ``prefix="SUBAGENT"``、自己的 token 默认值，以及 ``default_steps=0``
    （它不需要步数轴）—— 同一个实现、两套阈值来源，避免两份会各自漂移的预算代码。

    ⚠ **tokens / turns 两轴的名字带 `RUN_`**（``AGENT_RUN_MAX_TOKENS`` /
    ``SUBAGENT_RUN_MAX_TOKENS`` / ``..._RUN_MAX_TURNS``），与其余三轴不同。这不是手滑：
    那两轴的裸名（``AGENT_MAX_TOKENS`` / ``AGENT_MAX_TURNS``）在**网关**里是另一个意思
    （单次调用的 max output / 每次提交的默认回合数），同名不同义会让共享 env 的部署
    把引擎的 run 预算顶成"单次输出上限"（M20）。steps / seconds / cost 三轴只由引擎读，
    不与网关撞名，因此保留原名 —— 改名要精确到"真正会撞的那两个"。

    **token 上限区分"默认"与"显式"**：显式（构造参数或 `{prefix}_RUN_MAX_TOKENS`）
    得到的是一条硬线，窗口自适应与续段都不会越过它；只有落到 `default_tokens` 的
    默认策略才随模型窗口放大、并在撞线时续段。

    ⚠ 显式值**包括 0**：该轴 0 表示"不限"（见 `exceeded` 的 `if self.tokens and ...`），
    把 `XXX_RUN_MAX_TOKENS=0` 当成"没配"而回落默认值，等于用户关不掉这一轴。
    """
    env_raw = (os.getenv(f"{prefix}_RUN_MAX_TOKENS") or "").strip()
    env_tokens = int(env_raw) if env_raw.lstrip("-").isdigit() else None
    # 显式 = 构造参数给了非 0，或环境变量给了合法数字（含 0）。
    explicit_tokens = int(tokens or 0) or env_tokens
    return TaskBudget(
        steps=int(steps or 0) or _env_int(f"{prefix}_MAX_STEPS", default_steps),
        turns=int(turns or 0) or _env_int(f"{prefix}_RUN_MAX_TURNS", 0),
        tokens=default_tokens if explicit_tokens is None else explicit_tokens,
        tokens_pinned=explicit_tokens is not None,
        wall=int(seconds or 0) or _env_int(f"{prefix}_MAX_SECONDS", DEFAULT_MAX_SECONDS),
        cost=float(cost or 0.0) or float(_env_int(f"{prefix}_MAX_COST", int(DEFAULT_MAX_COST))),
        segments_left=max(0, int(max_segments or 0)),
    )
