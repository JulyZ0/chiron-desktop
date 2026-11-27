"""引擎实例级 run 并发闸。

为什么需要它：队列 worker 有自己的并发上限，但 `/v1/agent/submit` 这条**直连**链路
与 reconciler 的**自动续跑**此前没有实例级上限。多用户同时提交时，每条 run 都会
创建 runtime、占内存与上游连接；超过机器容量后不是"变慢"，而是互相拖垮（GC、
连接池、事件队列一起抖）。网关侧的每用户限流管不到"同一实例上有多少个 run 在跑"。

设计取舍：

* **只按实例计数**，不做跨实例协调。跨实例总量属于容量规划（网关按引擎实例负载
  路由），这里要的是"单进程别被压垮"的硬边界。
* **非阻塞**：直连请求拿不到槽位立即 429（让调用方退避），不排队 —— 排队会让
  客户端误以为已经开始，实际还在队列里，超时语义更糟。
* **自动续跑拿不到槽位就跳过本轮**：reconciler 是串行的，等槽位会把它钉住。
* ``engine_max_concurrent_runs=0`` 关闭（存量兼容）；默认 16 是"单机桌面 + 少量
  并发会话"的安全余量，服务器部署应按 CPU/内存显式设置。
"""

from __future__ import annotations

import threading


class InstanceRunGate:
    """进程内计数器；只在事件循环里调用，但加锁保证测试/多 loop 下不丢计数。"""

    def __init__(self, limit: int) -> None:
        self._limit = max(0, int(limit))
        self._in_use = 0
        self._mu = threading.Lock()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        with self._mu:
            return self._in_use

    def try_acquire(self) -> bool:
        if self._limit <= 0:
            return True
        with self._mu:
            if self._in_use >= self._limit:
                return False
            self._in_use += 1
            return True

    def release(self) -> None:
        if self._limit <= 0:
            return
        with self._mu:
            if self._in_use > 0:
                self._in_use -= 1


_gate: InstanceRunGate | None = None
_gate_mu = threading.Lock()


def instance_run_gate() -> InstanceRunGate:
    global _gate
    if _gate is None:
        from app.config import settings

        with _gate_mu:
            if _gate is None:
                _gate = InstanceRunGate(
                    int(getattr(settings, "engine_max_concurrent_runs", 0) or 0)
                )
    return _gate


def reset_instance_run_gate_for_tests() -> None:
    """测试专用：配置在进程内是静态的，换值必须显式重建。"""
    global _gate
    with _gate_mu:
        _gate = None
