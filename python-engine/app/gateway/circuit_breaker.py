# 熔断器 — per-provider 三态: CLOSED → OPEN → HALF_OPEN → CLOSED
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitState(Enum):
    CLOSED = "closed"  # 正常
    OPEN = "open"  # 熔断
    HALF_OPEN = "half_open"  # 试探


#: 写给 Prometheus 的取值。半开单独给 2 —— 与 CLOSED 混成 0 的话，"正在试探"这个
#: 最需要被看见的瞬间会在面板上消失。
_STATE_VALUES = {
    CircuitState.CLOSED: 0.0,
    CircuitState.HALF_OPEN: 1.0,
    CircuitState.OPEN: 2.0,
}


@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    #: 上报指标用的身份。**空 provider = 不上报**（单测里裸建一个不污染全局指标）。
    provider: str = ""
    #: "chat" / "embed"：两张熔断表各自独立，同名 provider 可能只熔断了其中一个。
    kind: str = "chat"
    _failure_count: int = field(default=0, init=False, repr=False)
    _state: CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _last_failure_time: float = field(default=0.0, init=False, repr=False)
    #: 半开探针是否已发出（**只允许一个**）。见 allow() 的说明。
    _probe_in_flight: bool = field(default=False, init=False, repr=False)
    #: 探针发出的时刻，用于回收"发出去却没人回报"的探针。
    _probe_started_at: float = field(default=0.0, init=False, repr=False)
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    def __post_init__(self) -> None:
        # 建表即上报一次 closed，否则"还没出过事"的 provider 在指标里根本不存在，
        # 面板上分不出"健康"与"没采集到"。
        self._publish_state()

    @property
    def state(self) -> CircuitState:
        return self._state

    def _publish_state(self) -> None:
        """把状态推到 Prometheus。**调用方须已持锁**（或在构造期单线程调用）。

        `LLM_CIRCUIT_STATE` 此前只有定义、全仓零处写入，`/metrics` 抓到的永远是空序列。
        上报失败绝不能影响熔断判定本身，因此这里吞掉所有异常只留 debug 日志。
        """
        if not self.provider:
            return
        try:
            from app.observability.metrics import LLM_CIRCUIT_STATE

            LLM_CIRCUIT_STATE.labels(provider=self.provider, kind=self.kind).set(
                _STATE_VALUES[self._state]
            )
        except Exception:  # pragma: no cover - 指标不可用不该拖垮熔断
            logger.debug("circuit breaker metric publish failed", exc_info=True)

    def allow(self) -> bool:
        """是否放行本次调用。**半开态最多放行一个探针**。

        历史缺陷：半开分支此前直接 ``return True``（注释写的却是 "allow one probe"），
        于是并发请求全部穿过去 —— 实测连续 10 次调用 10 次放行。熔断器给出的容量保护
        在最需要它的半开阶段等于不存在；更糟的是探针若没人回报（调用方在真正发请求前
        就异常退出），状态会**永远**停在与 CLOSED 等效的全放行上，再也不回到 OPEN。

        现在的规则：
        * 半开：只放行一个探针，其余请求继续被拒（它们没有探针资格）；
        * 探针超时无回音：视为丢失，一个 ``recovery_timeout`` 之后补发一个 ——
          宁可再试一次，也不要让熔断器卡死在半开（那等于没有熔断）。
        """
        with self._lock:
            now = time.monotonic()
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                if now - self._last_failure_time <= self.recovery_timeout:
                    return False
                self._state = CircuitState.HALF_OPEN
                self._probe_in_flight = False
                self._publish_state()
                logger.info("Circuit breaker HALF_OPEN — allowing probe request")
            return self._claim_probe(now)

    def _claim_probe(self, now: float) -> bool:
        """在半开态领取探针资格（调用方须已持锁）。"""
        if self._probe_in_flight:
            stalled = now - self._probe_started_at
            if stalled <= self.recovery_timeout:
                return False
            # 探针发出后既没成功也没失败：调用方很可能在真正发请求前就退出了。
            logger.warning(
                "Circuit breaker probe lost after %.1fs — re-issuing one", stalled
            )
        self._probe_in_flight = True
        self._probe_started_at = now
        return True

    def record_success(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._state = CircuitState.CLOSED
                self._publish_state()
                logger.info("Circuit breaker CLOSED — recovered")
            self._failure_count = 0
            # 探针已回报，交还资格。CLOSED 下本就为 False，这里统一清掉更不容易漏。
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            self._probe_in_flight = False
            # 半开探针失败时 `_failure_count` 仍 >= 阈值（进半开不清零），
            # 因此一次探针失败就会重新 OPEN —— 这正是"探针不过就继续熔断"。
            if self._failure_count >= self.failure_threshold:
                self._state = CircuitState.OPEN
                self._publish_state()
                logger.warning(
                    "Circuit breaker OPEN — %d consecutive failures",
                    self._failure_count,
                )

    def reset(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = CircuitState.CLOSED
            self._probe_in_flight = False
            self._publish_state()
