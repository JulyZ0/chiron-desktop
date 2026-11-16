"""prompt cache 保活的**埋点**（差距 G-L3）。

## 为什么只埋点、不发保活请求

多轮之间只要有间隔（用户思考、审批挂起、子 Agent 跑完回来），前缀缓存就会过期，
下一条请求按全价重算。pi 的做法是按 TTL 重发 1-token 请求保活（`CacheWarmer`）。

chiron 不直接照做，因为**保活本身要花钱**：发得比"重算"贵就是净亏。这笔账要按部署
口径算（谁的预算、什么阈值、哪些 provider 的 TTL），而那是产品决策，不是实现细节。
在决策下来之前先做一件无论如何都该做的事：**把这个决策需要的数据采出来** ——
"这次空转等了多久"、"上一次响应里缓存命中/写入了多少 token"。有了它，阈值才有依据；
没有它，做出来也是拍脑袋。

所以本模块的约束是硬的：**`IdleGap.would_warm` 只是判定，模块内没有任何代码路径会发请求**。
真发是后续独立的一步（接入时在 `would_warm` 为真的地方加一次 1-token 请求，并配预算闸）。

## 埋什么：一个事实，三种成因

审批挂起 / steer 等待 / 子 Agent 汇报看起来是三个点，但**对缓存而言是同一件事**：
两次 LLM 调用之间的间隔。分别在三处插桩会得到三份必然漂移的计时逻辑，还会漏掉
"用户就是想了五分钟"这种最常见的空转。因此只在轮次边界量一次间隔，成因作为
`reason` 标注 —— 埋点在边界上，归因靠标签。

## TTL 的事实依据

Anthropic 的 `cache_control: {"type": "ephemeral"}` 默认 5 分钟（写入口径；
`app/providers/anthropic.py:438` 打的就是它）。OpenAI / DeepSeek 侧的缓存 TTL
由厂商决定且不公开，取同一个保守值即可 —— 埋点阶段宁可把"可能没过期"也算成过期，
因为它的用途是**估上界**，不是计费。

## 与 `cached_tokens` 的关系

读用量（命中，`cached_tokens`）已经进了会话统计与 `usage` 事件；这里补的是
**k（读）+ 写**两侧同时进 span —— 判断"保活值不值"要看命中省下的与重新写入要花的
差额，只有读侧读数算不出来。
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 空转成因（稳定标识，改文案不影响它）────────────────────

#: 等待用户确认一次工具调用（`_await_approval`）。
IDLE_APPROVAL = "approval_wait"
#: 运行中指导（steer）在轮次边界被消费前的间隔。
IDLE_STEER = "steer_wait"
#: 子 Agent 汇报挂进下一轮之前的那段。
IDLE_SUBAGENT_REPORT = "subagent_report"
#: 未归因的普通轮次间隔（用户思考 / 客户端重连等）。
IDLE_TURN = "turn_boundary"

#: 缓存 TTL（秒）。Anthropic ephemeral 默认 5 分钟；其余 provider 取同一保守值。
CACHE_TTL_SECONDS = 300.0

#: 间隔小于这个值的不值得单独上报：亚秒级间隔全是同一次交互内的噪声，
#: 上报它们只会把"真的空转了五分钟"淹掉。
MIN_REPORTABLE_IDLE_SECONDS = 1.0


@dataclass(frozen=True)
class CacheUsage:
    """一次 LLM 响应的缓存用量。

    ``read_tokens`` = 命中缓存、按折扣计价的那部分输入；
    ``write_tokens`` = 为写缓存多付的那部分输入（Anthropic 的
    ``cache_creation_input_tokens``；其余 provider 没有这个概念，恒 0）。
    """

    read_tokens: int = 0
    write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.read_tokens + self.write_tokens

    @property
    def hit_rate(self) -> float:
        """读 / (读 + 写)。没有缓存活动时返回 0，而不是除零或 1。"""
        total = self.total
        return (self.read_tokens / total) if total else 0.0


@dataclass(frozen=True)
class IdleGap:
    """一次轮次间空转的测量结果。**不包含任何要发出去的请求。**"""

    seconds: float
    reason: str
    usage: CacheUsage
    ttl_seconds: float = CACHE_TTL_SECONDS

    @property
    def expired(self) -> bool:
        """间隔是否已经超过缓存 TTL —— 超过即"下一次调用必然全价重算"。"""
        return self.seconds >= self.ttl_seconds

    @property
    def would_warm(self) -> bool:
        """**假如**实现了保活，这里就是该发一次保活请求的点。

        两个条件缺一不可：缓存里真的有东西（上次读/写非零）且它已经过期。
        对一件从来没进过缓存的前缀保活是无意义的额外开销。
        """
        return self.expired and self.usage.total > 0

    @property
    def worth_reporting(self) -> bool:
        """噪声闸：亚秒级间隔不上报。"""
        return self.seconds >= MIN_REPORTABLE_IDLE_SECONDS

    def as_metadata(self, model: str = "") -> dict[str, object]:
        """span 元数据。字段名对齐 `llm_call` span，便于在同一张图里对照。"""
        return {
            "reason": self.reason,
            "idle_seconds": round(self.seconds, 3),
            "ttl_seconds": self.ttl_seconds,
            "expired": self.expired,
            # 埋点阶段恒 False：本模块不发请求，这个字段只说明"将来会在这里发"。
            "would_warm": self.would_warm,
            "cache_read_tokens": self.usage.read_tokens,
            "cache_write_tokens": self.usage.write_tokens,
            "cache_hit_rate": round(self.usage.hit_rate, 4),
            "model": model,
        }


def classify_idle_gap(
    seconds: float,
    usage: CacheUsage,
    *,
    reason: str = IDLE_TURN,
    ttl_seconds: float = CACHE_TTL_SECONDS,
) -> IdleGap:
    """把"间隔 + 上次用量"归一成一条可上报的空转记录。

    纯函数：不读时钟、不发请求，因而可直接单测。
    """
    return IdleGap(
        seconds=max(0.0, float(seconds)),
        reason=reason,
        usage=usage,
        ttl_seconds=ttl_seconds,
    )
