"""熔断器三态回归测试（app/gateway/circuit_breaker.py）。

为什么需要这组测试：`CircuitBreaker` 此前**零测试覆盖**，而它的半开分支写错了 ——
注释是 "allow one probe"，实现是 ``return True``（探针无限放行）。熔断器在
半开阶段给出的容量保护等于不存在，且探针若没人回报，状态会永久停在那个全放行态。

这里同时钉住"探针必须只有一个"与"探针丢失要能自愈"两件事，用很小的
``recovery_timeout`` 走真实时间（避免 patch 全局 ``time.monotonic`` 影响 pytest 自身计时）。
"""

from __future__ import annotations

import time

from app.gateway.circuit_breaker import CircuitBreaker, CircuitState

#: 足够容纳 sleep 与定时精度，又让测试总耗时保持在毫秒级。
_TIMEOUT = 0.05


def _open_breaker() -> CircuitBreaker:
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=_TIMEOUT)
    for _ in range(2):
        cb.record_failure()
    assert cb.state is CircuitState.OPEN
    return cb


def test_closed_allows_everything() -> None:
    cb = CircuitBreaker(failure_threshold=2)
    assert cb.state is CircuitState.CLOSED
    assert all(cb.allow() for _ in range(10))


def test_opens_after_threshold_and_rejects_until_timeout() -> None:
    cb = _open_breaker()
    # 未超时：一律拒绝，且不能被 allow() 反复调用"蹭"进半开。
    assert cb.allow() is False
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False


def test_half_open_allows_exactly_one_probe() -> None:
    """核心回归：半开态只放行一个探针，其余并发请求继续被拒。"""
    cb = _open_breaker()
    time.sleep(_TIMEOUT + 0.02)

    allowed = [cb.allow() for _ in range(10)]
    assert allowed.count(True) == 1, f"半开态应只放行 1 个探针，实际 {allowed.count(True)}"
    assert cb.state is CircuitState.HALF_OPEN


def test_successful_probe_closes_circuit() -> None:
    cb = _open_breaker()
    time.sleep(_TIMEOUT + 0.02)
    assert cb.allow() is True

    cb.record_success()
    assert cb.state is CircuitState.CLOSED
    # 恢复后应重新全放行。
    assert all(cb.allow() for _ in range(5))


def test_failed_probe_reopens_circuit() -> None:
    cb = _open_breaker()
    time.sleep(_TIMEOUT + 0.02)
    assert cb.allow() is True

    cb.record_failure()
    assert cb.state is CircuitState.OPEN
    assert cb.allow() is False


def test_lost_probe_is_reissued_after_timeout() -> None:
    """探针发出去却没人回报（调用方异常退出）不能让熔断器永久卡在半开。"""
    cb = _open_breaker()
    time.sleep(_TIMEOUT + 0.02)
    assert cb.allow() is True

    # 拿到探针后既不上报成功也不上报失败；本次仍应被拒（探针还在飞）。
    assert cb.allow() is False

    time.sleep(_TIMEOUT + 0.02)
    # 超过一个 recovery_timeout 仍无回音 ⇒ 视为丢失，补发一个（仍然只有一个）。
    assert cb.allow() is True
    assert cb.allow() is False


def test_reset_clears_state_and_probe() -> None:
    cb = _open_breaker()
    time.sleep(_TIMEOUT + 0.02)
    assert cb.allow() is True

    cb.reset()
    assert cb.state is CircuitState.CLOSED
    assert all(cb.allow() for _ in range(5))
