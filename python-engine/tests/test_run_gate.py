"""实例级 run 并发闸（L19）。"""

from __future__ import annotations

from app.run_gate import InstanceRunGate


def test_gate_rejects_when_limit_reached() -> None:
    gate = InstanceRunGate(2)
    assert gate.try_acquire() is True
    assert gate.try_acquire() is True
    assert gate.try_acquire() is False
    assert gate.in_use == 2

    gate.release()
    assert gate.in_use == 1
    assert gate.try_acquire() is True


def test_gate_release_is_idempotent_below_zero() -> None:
    gate = InstanceRunGate(1)
    gate.release()
    assert gate.in_use == 0
    assert gate.try_acquire() is True
    gate.release()
    gate.release()
    assert gate.in_use == 0


def test_zero_limit_disables_gate() -> None:
    gate = InstanceRunGate(0)
    for _ in range(100):
        assert gate.try_acquire() is True
    gate.release()
    assert gate.in_use == 0
    assert gate.limit == 0
