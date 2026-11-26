# 队列消费者 — Redis Streams Consumer Group
# P1-2: 信号处理与优雅关闭 (生产安全检查 2026-08-17)
from __future__ import annotations

import asyncio
import datetime
import json
import logging
import os
import signal
import time
from typing import Any, cast

import redis.asyncio as aioredis

from app.observability.logging import tenant_id_var, trace_id_var
from app.observability.metrics import QUEUE_DEPTH, QUEUE_DLQ_TOTAL, QUEUE_PROCESSING_DURATION, QUEUE_RETRY_TOTAL
from app.queue import heartbeat, idempotency, job_events
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# 队列规格（stream / 消费组 / DLQ / 延迟键）统一在 app/queue/streams.py：引擎任务与
# 知识库索引**分属两条 stream** —— Redis Stream 单消费组语义下，"让引擎 worker 别碰
# kb_index"不能靠过滤（消息已被取走），只能换队列。此前本文件自己又定义了一份流名，
# 且重投用了比生产者更小的 maxlen —— 积压时一次重投就会静默修剪掉最老的任务。
from app.queue.streams import (  # noqa: E402
    ENGINE_QUEUE,
    QueueSpec,
    atomic_promote_delayed,
    atomic_xadd_ack,
    is_foreign,
    queue_for_task,
)

CONSUMER_PREFIX = "worker"
MAX_RETRIES = 3
# 重领（reclaim）上限：与业务失败分开计数。滚动发布/空闲自停会让一条**从未失败**的
# 长任务被反复 reclaim；旧实现把它算进 retry_count，三次发布后就进 DLQ。
MAX_RECLAIMS = 20

# ── 跨实例全局并发门控(N5,防多引擎实例后台 10×N 洪峰)──
# Redis 计数键 engine:worker:inflight;TTL 防进程崩溃泄漏;0=关闭。
GATE_KEY = rkey("engine:worker:inflight")
GATE_TTL = 300
GATE_WAIT_SECS = 3.0
GATE_RETRY_INTERVAL = 0.5

# ── PEL 崩溃恢复（XCLAIM 认领）─────────────────────────────────────────────
# worker 崩溃后其未 ACK 消息停留在消费组 PEL；本实例周期认领 idle 超阈值的
# pending 消息重投（tool_job 重执行）。
# 处理中的消息由 _heartbeat_lease 周期刷新 idle（lease），所以阈值不必再大于
# 单任务处理上限——只有真正失联（崩溃/被杀/网络分区）的消息才会 idle 超时。
# 原值 (3600+180)s 使崩溃任务最长滞留约 1 小时才被恢复；现默认 600s。
CLAIM_MIN_IDLE_SECS = int(os.getenv("QUEUE_CLAIM_MIN_IDLE_SECS", "600"))
CLAIM_MIN_IDLE_MS = CLAIM_MIN_IDLE_SECS * 1000
# lease 心跳周期：阈值 1/3（下限 30s），保证阈值窗口内至少刷新两次。
HEARTBEAT_SECS = max(1, CLAIM_MIN_IDLE_SECS // 3)
CLAIM_LOOP_SECS = 60
CLAIM_BATCH = 50

_GATE_ACQUIRE_LUA = """
local v = redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[1])
if v > tonumber(ARGV[2]) then
  redis.call('DECR', KEYS[1])
  return 0
end
return 1
"""

_GATE_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if v and tonumber(v) > 0 then
  return redis.call('DECR', KEYS[1])
end
return 0
"""


# ── 延迟重试（P2-3）─────────────────────────────────────────────────────────
#
# 为什么需要：`agent_followup` 最常见的失败不是"任务本身有问题"，而是**父会话此刻正忙**
# （会话运行锁 409）。原来的重试是"立刻放回队尾"——3 次重试预算在毫秒级耗尽，随后进 DLQ，
# 而需要等的条件通常要几十秒甚至几分钟才释放。结果就是：子 Agent 的结论明明已经落库，
# 却因为一次锁竞争再也回不到主对话。
# 这里给"等一会儿再试"一条独立通路：延迟集合（ZSET，score=可执行时间），
# 到点由 `_promote_delayed` 搬回主队列，**不消耗重试预算**（它没有失败），
# 由 deadline 与 delay 次数上限兜底，超限则进 DLQ（可见、可人工重投）。
DELAYED_ZSET = ENGINE_QUEUE.delayed
DELAY_LOOP_SECS = float(os.getenv("QUEUE_DELAY_LOOP_SECS", "15"))
DELAY_BASE_SECONDS = float(os.getenv("QUEUE_DELAY_BASE_SECONDS", "20"))
DELAY_MAX_SECONDS = float(os.getenv("QUEUE_DELAY_MAX_SECONDS", "300"))
DELAY_MAX_ATTEMPTS = int(os.getenv("QUEUE_DELAY_MAX_ATTEMPTS", "12"))
DELAY_BATCH = 50


class RetryLaterError(RuntimeError):
    """"现在不行，等一会儿再来"——**不是失败**。

    抛出它的 handler 表示：条件（会话锁、并发配额、上游暂时不可达）会自行好转，
    应当延迟重投而不是消耗重试预算、更不该进 DLQ。
    """

    def __init__(self, message: str, delay_seconds: float = 0.0) -> None:
        super().__init__(message)
        #: 退避基值（秒）。0 表示用全局默认基值；上游给了 Retry-After 时用它。
        self.delay_seconds = float(delay_seconds or 0)


def _as_int(value: Any, default: int = 0) -> int:
    """把消息字段（bytes / str / int / None）解析成 int，解析不了回落默认值。

    队列消息来自 Redis，字段可能是 bytes、str 或干脆缺失；直接 ``int()`` 会抛异常，
    而那等于把"一条字段脏掉的消息"变成"延迟重试逻辑本身崩溃"。
    """
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode()
        except Exception:  # noqa: BLE001 - 解不出来就按缺失处理
            return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _text_field(fields: dict[Any, Any], name: str | bytes, default: str = "") -> str:
    value = fields.get(name, default) if isinstance(name, bytes) else fields.get(name, fields.get(name.encode(), default))
    if isinstance(value, (bytes, bytearray)):
        return value.decode()
    return str(value) if value is not None else default


def _text_fields(fields: dict[Any, Any]) -> dict[str, str]:
    message: dict[str, str] = {}
    for key in fields:
        name = key.decode() if isinstance(key, bytes) else str(key)
        message[name] = _text_field(fields, key)
    return message


def _retry_after(resp: Any) -> float:
    """读上游的 ``Retry-After``（秒）。解析不了就返回 0（用默认退避基值）。

    网关目前不发这个头，但读它是为了让"上游明确告诉我们等多久"时不必猜。
    """
    raw = ""
    try:
        raw = str(resp.headers.get("retry-after") or "").strip()
    except Exception:  # noqa: BLE001 - 头不可读时按默认退避处理
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


class QueueWorker:
    """
    Redis Streams 消费者

    生命周期:
      - 启动: XREADGROUP BLOCK 5s
      - 处理: ACK 成功 / NACK 失败
      - 重试: NACK 后超时 30s 可被 XCLAIM
      - 死信: retry_count >= 3 → XADD 到 DLQ
      - 关闭: 停止消费 → 等待 in-flight → 退出
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        concurrency: int = 10,
        queue: QueueSpec = ENGINE_QUEUE,
        gateway: Any = None,
        memory_service: Any = None,
        global_concurrency: int = 10,
        job_heartbeat_interval: float | None = None,
    ) -> None:
        self._redis = redis
        self._concurrency = concurrency
        self._gate_limit = global_concurrency
        self._gateway = gateway  # GatewayRouter（用于 RAG 构建/嵌入），可为 None
        self._memory_service = (
            memory_service  # MemoryService（用于记忆存储），可为 None
        )
        self._running = False
        self._semaphore = asyncio.Semaphore(concurrency)
        self._in_flight: set[asyncio.Task[Any]] = set()
        # 本 worker 消费哪条队列（引擎 / 知识库索引）—— 见 app/queue/streams.py
        self._q = queue
        self._consumer_name = f"{CONSUMER_PREFIX}-{id(self):x}"
        # G16：任务级心跳周期（秒）。``None`` = 用 ``QUEUE_JOB_HEARTBEAT_SECS`` 的默认值。
        # 注入它是为了让测试用毫秒级间隔、不必真的等 30 秒，而不是为了让生产改行为。
        self._job_heartbeat_interval = job_heartbeat_interval
        self._reclaim_task: asyncio.Task[Any] | None = None
        self._delay_task: asyncio.Task[Any] | None = None
        #: Super-agent 看门狗的定时循环（P1-3）。**默认不创建** —— 见 `start()`。
        self._watchdog_task: asyncio.Task[Any] | None = None
        #: Super-agent 接管的巡检循环。**默认不创建** —— 见 `_start_takeover()`。
        #: 与看门狗分开是刻意的：两者节奏不同（看门狗全局 5 分钟，接管按每会话间隔到点）。
        self._takeover_task: asyncio.Task[Any] | None = None

    @property
    def in_flight_count(self) -> int:
        """本地正在执行的任务数（供空闲自停/诊断判断"是否真的没人用"）。"""
        return len(self._in_flight)

    async def start(self) -> None:
        """启动消费者"""
        self._running = True

        # P1-2: 注册信号处理器（支持 SIGTERM/Kill -15）
        await self._register_signal_handlers()

        # 确保 Consumer Group 存在
        try:
            await self._redis.xgroup_create(
                self._q.stream, self._q.group, id="0", mkstream=True
            )
            logger.info("Consumer group '%s' created", self._q.group)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise
            logger.debug("Consumer group '%s' already exists", self._q.group)

        logger.info(
            "Queue worker started: consumer=%s concurrency=%d",
            self._consumer_name,
            self._concurrency,
        )

        # PEL 崩溃恢复：后台认领循环（worker 崩溃后由其它实例接管其 pending 任务）
        self._reclaim_task = asyncio.create_task(self._reclaim_loop())
        logger.info("Queue worker reclaim loop started (idle=%ds)", CLAIM_MIN_IDLE_MS // 1000)

        # 延迟重试：把到期的"等一会儿再来"任务搬回主队列
        self._delay_task = asyncio.create_task(self._delay_loop())
        logger.info("Queue worker delayed-retry loop started (every %.0fs)", DELAY_LOOP_SECS)

        # Super-agent 看门狗（P1-3）：**只有显式开启才创建**任务。
        # 关掉时连一个空转的协程都没有 —— "关掉它时零影响"是硬要求，不是"影响很小"。
        self._start_watchdog()

        # Super-agent 接管：同款取向 —— 关着就不创建任务。
        # 它比看门狗更"重"（会真的起 run、花钱、改文件），所以默认关的理由更充分。
        self._start_takeover()

        while self._running:
            try:
                # 管理端「暂停队列」：置位期间不消费新任务。
                # P1 修复：queue:paused 此前只被网关写入（admin_extras.go 的
                # PauseQueue）、全仓库无人读取 —— 后台按钮是死功能。
                if await self._redis.exists(rkey("queue:paused")):
                    await asyncio.sleep(0.5)
                    continue

                # 管理端「清空队列」：先把本 group 的 pending 全部 ACK（避免残留
                # dangling PEL 被 reclaim 循环反复认领），再截断主队列与延迟队列。
                # 同样修复了 queue:flush 只写不读的死功能。
                if await self._redis.exists(rkey("queue:flush")):
                    await self._redis.delete(rkey("queue:flush"))
                    # PEL 是**组级共享**的：别的实例正在执行的消息也在里面。旧实现把
                    # 它们一起 XACK + xtrim(0)，对方完成时对不上 PEL，任务被标 failed
                    # 而副作用已经发生。有他人在跑就拒绝本轮 flush（管理员稍后重试），
                    # 只清理本 consumer 的 pending。
                    other_pending = await self._other_consumers_pending()
                    if other_pending:
                        logger.warning(
                            "Queue flush skipped: %s task(s) in flight on other consumers",
                            other_pending,
                        )
                        continue
                    while True:
                        pending = await self._redis.xpending_range(
                            self._q.stream, self._q.group, min="-", max="+", count=1000
                        )
                        if not pending:
                            break
                        await self._redis.xack(
                            self._q.stream,
                            self._q.group,
                            *[p["message_id"] for p in pending],
                        )
                    await self._redis.xtrim(self._q.stream, maxlen=0)
                    await self._redis.delete(self._q.delayed)
                    logger.warning("Queue flushed by admin request")
                    continue

                await self._consume_batch()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Queue worker error: %s", e)
                await asyncio.sleep(1)

    async def stop(self) -> None:
        """P1-2: 优雅停止（带超时保护）。"""
        self._running = False
        # 先抓引用再 cancel：cancel() 只是投递请求，不等它跑完 finally（心跳 detach /
        # 子进程 terminate），后面的 close_pool/aclose 可能先执行，留下孤儿任务。
        background = [
            t for t in (
                self._reclaim_task,
                self._delay_task,
                self._watchdog_task,
                self._takeover_task,
            )
            if t is not None
        ]
        for task in background:
            task.cancel()
        self._reclaim_task = None
        self._delay_task = None
        self._watchdog_task = None
        self._takeover_task = None
        if background:
            await asyncio.gather(*background, return_exceptions=True)

        logger.info(
            "Queue worker stopping, waiting for %d in-flight tasks...",
            len(self._in_flight),
        )
        if self._in_flight:
            # P1-2: 设置超时，防止 task 永久挂起
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._in_flight, return_exceptions=True),
                    timeout=30.0,  # 最多等待 30 秒
                )
            except TimeoutError:
                logger.error(
                    "Queue worker shutdown timed out, cancelling remaining tasks"
                )
                for task in self._in_flight:
                    task.cancel()
                # cancel 后必须再等一轮：被取消任务的 finally 才是心跳/子进程清理点。
                await asyncio.gather(*self._in_flight, return_exceptions=True)
        logger.info("Queue worker stopped")

    async def _register_signal_handlers(self) -> None:
        """P1-2: 注册 UNIX 信号处理器（Kubernetes Docker Pod 兼容）"""
        loop = asyncio.get_event_loop()

        def _signal_handler() -> None:
            """信号回调：触发异步停止"""
            asyncio.create_task(self.stop())

        try:
            loop.add_signal_handler(signal.SIGTERM, _signal_handler)
            loop.add_signal_handler(signal.SIGINT, _signal_handler)
            logger.info("Registered signal handlers for SIGTERM/SIGINT")
        except NotImplementedError:
            # Windows 不支持 add_signal_handler
            logger.debug("Signal handling not supported on this platform")

    async def _consume_batch(self) -> None:
        """批量消费一批消息"""
        try:
            results = await self._redis.xreadgroup(
                self._q.group,
                self._consumer_name,
                {self._q.stream: ">"},
                count=self._concurrency,
                block=5000,  # 5 秒超时
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("XREADGROUP error: %s", e)
            return

        if not results:
            return

        # Redis 的 XREADGROUP 返回 (stream, [(id, fields), ...])；stub 的返回类型比这更宽
        # （含 str），这里按实际协议收窄，避免把 str 解包成两个字符。
        for _stream, messages in cast(
            "list[tuple[Any, list[tuple[str, dict[str, Any]]]]]", results
        ):
            for stream_id, fields in messages:
                await self._spawn_message(stream_id, fields)

    async def _requeue_unprocessed(self, stream_id: str, fields: dict[Any, Any]) -> None:
        """把停机窗口里已读入但未处理的消息放回队列并 ACK 原条目。

        先 XADD 再 XACK：两步之间进程崩溃会留下一条重复（原 pending + 新条目），
        但任务幂等键保证不会执行两次；反过来先 ACK 再 XADD，崩溃就是丢任务。
        """
        try:
            await self._redis.xadd(self._q.stream, fields)
            await self._redis.xack(self._q.stream, self._q.group, stream_id)
            logger.info("requeued unprocessed task during shutdown: id=%s", stream_id)
        except Exception as exc:  # noqa: BLE001 - 放回失败就留给 reclaim
            logger.warning("requeue unprocessed task failed (id=%s): %s", stream_id, exc)

    async def _other_consumers_pending(self) -> int:
        """返回**其他 consumer** 的 pending 条数（用于拒绝会误伤他们的 flush）。"""
        try:
            summary = await self._redis.xpending(self._q.stream, self._q.group)
        except Exception:  # noqa: BLE001 - 读不到就不做危险操作
            return 1
        if not isinstance(summary, dict):
            return 1
        total = 0
        for c in summary.get("consumers") or []:
            name = c.get("name") if isinstance(c, dict) else None
            count = c.get("pending") if isinstance(c, dict) else None
            if name and name != self._consumer_name:
                total += int(count or 0)
        return total

    async def _spawn_message(self, stream_id: str, fields: dict[Any, Any]) -> None:
        """把一条消息投入本地处理：本地信号量 + 跨实例全局门控 + 超时保护。"""
        if not self._running:
            # 停机窗口里 XREADGROUP 可能已把消息读进来（返回后才看到 _running=false）。
            # 直接丢弃会让它滞留 PEL 直到 reclaim（默认 600s）；先放回队列再 ACK。
            await self._requeue_unprocessed(stream_id, fields)
            return
        # 等待本地信号量（每实例）
        await self._semaphore.acquire()
        # 跨实例全局门控：超限等待上限后把消息放回队尾并 ACK（避免认领窗口重复处理）
        if not await self._try_acquire_gate(stream_id, fields):
            self._semaphore.release()
            return
        task = asyncio.create_task(self._process_message(stream_id, fields))
        timeout_task = asyncio.create_task(asyncio.wait_for(task, timeout=3600))
        self._in_flight.add(timeout_task)
        timeout_task.add_done_callback(self._task_done)

    async def _heartbeat_lease(
        self,
        stream_id: str,
        claim: idempotency.Attempt,
        owner: asyncio.Task[Any],
    ) -> None:
        """Renew ownership and PEL idle time with the same fenced token."""
        while not owner.done():
            try:
                await asyncio.sleep(HEARTBEAT_SECS)
            except asyncio.CancelledError:
                return
            if owner.done():
                return
            try:
                renewed = await idempotency.renew(
                    self._redis,
                    claim,
                    stream=self._q.stream,
                    group=self._q.group,
                    consumer=self._consumer_name,
                    message_id=stream_id,
                    lease_seconds=CLAIM_MIN_IDLE_SECS,
                )
                if not renewed:
                    logger.warning("worker attempt fenced off: id=%s", stream_id)
                    owner.cancel()
                    return
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001 - a transient failure is not proof of lost ownership
                logger.debug("worker lease heartbeat failed for %s: %s", stream_id, e)

    async def _reclaim_loop(self) -> None:
        """PEL 崩溃恢复认领循环：周期接管 idle 超阈值的 pending 消息。"""
        while self._running:
            try:
                await asyncio.sleep(CLAIM_LOOP_SECS)
                await self._reclaim_pending()
                # 队列深度指标：此前 update_queue_depth() 全仓没有调用点，
                # prometheus_alerts 里基于 queue_depth 的积压告警永远不会触发。
                await self.update_queue_depth()
                # G16：顺手盘点"心跳停了、但还没到 claim 阈值"的任务。
                # 修前这一档完全不可见：它们看起来和正常跑着的一样，直到 lease 超时
                # 才以"失联"的形式被重领 —— 而重领既晚，也说不清上一次是卡住的。
                await self._sweep_stale_heartbeats()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("worker reclaim loop error: %s", e)

    async def _sweep_stale_heartbeats(self) -> int:
        """只读盘点卡住的任务，返回本轮发现的数量（G16）。

        **不改任何队列状态**：卡住之后怎么处置（放弃、重投、叫人来看）是上层的决定。
        指标与日志在这里落地 —— 没有它们，"卡住"只能靠人去翻 Redis。
        """
        try:
            stale = await heartbeat.scan_stale_heartbeats(
                self._redis, interval=self._job_heartbeat_interval
            )
        except Exception as exc:  # noqa: BLE001 - 观测失败不能把回收循环打散
            logger.debug("worker stale heartbeat sweep failed: %s", str(exc)[:120])
            return 0
        return len(stale)

    async def _reclaim_pending(self) -> None:
        """认领本组内 idle 超过 CLAIM_MIN_IDLE_MS 的 pending 消息并重新处理。

        覆盖场景：worker 崩溃/被杀后其未 ACK 消息滞留 PEL；其它实例接管后
        tool_job 重新执行（retry_count 递增，
        超过 MAX_RETRIES 进 DLQ）。正在其它实例执行的长任务（idle < 阈值）不会被抢。
        """
        try:
            pend = await self._redis.xpending_range(
                self._q.stream,
                self._q.group,
                min="-",
                max="+",
                count=CLAIM_BATCH,
                idle=CLAIM_MIN_IDLE_MS,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker pending scan failed: %s", e)
            return
        if not pend:
            return
        ids: list[Any] = []
        for p in pend:
            mid = p.get("message_id") if isinstance(p, dict) else p.message_id
            if mid:
                ids.append(mid)
        if not ids:
            return
        try:
            claimed = await self._redis.xclaim(
                self._q.stream, self._q.group, self._consumer_name, CLAIM_MIN_IDLE_MS, ids
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker xclaim failed: %s", e)
            return
        for item in claimed:
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                await self._spawn_claimed(
                    cast("str", item[0]), cast("dict[str, Any]", item[1])
                )

    async def _spawn_claimed(self, msg_id: str, fields: dict[Any, Any]) -> None:
        """把一次 reclaim 记在**独立**的 claim_count 上（不是业务失败）。"""
        message = _text_fields(fields)
        message["claim_count"] = str(_as_int(message.get("claim_count")) + 1)
        await self._spawn_message(msg_id, message)

    async def _try_acquire_gate(self, stream_id: str, fields: dict[Any, Any]) -> bool:
        """全局并发门控：抢到槽位返回 True；等待 GATE_WAIT_SECS 仍满则放回队尾返回 False。
        Redis 故障/未配置：fail-open（返回 True，交由后续调用报错/本地并发约束）。"""
        if not self._gate_limit or self._redis is None:
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + GATE_WAIT_SECS
        while True:
            try:
                ok = await self._redis.eval(
                    _GATE_ACQUIRE_LUA, 1, GATE_KEY, GATE_TTL, self._gate_limit
                )
            except Exception:
                return True  # fail-open
            if ok:
                return True
            if loop.time() >= deadline:
                requeue = _text_fields(fields)
                try:
                    moved = await atomic_xadd_ack(
                        self._redis,
                        source=self._q.stream,
                        target=self._q.stream,
                        group=self._q.group,
                        consumer=self._consumer_name,
                        message_id=stream_id,
                        fields=requeue,
                        maxlen=self._q.maxlen,
                    )
                    if moved is None:
                        return True
                except Exception as exc:
                    logger.warning("worker gate requeue failed: %s", exc)
                    return True
                task_id = requeue.get("task_id", "")
                logger.info(
                    "worker global concurrency full; message requeued id=%s",
                    task_id,
                )
                return False
            await asyncio.sleep(GATE_RETRY_INTERVAL)

    async def _release_gate(self) -> None:
        if not self._gate_limit or self._redis is None:
            return
        try:
            await self._redis.eval(_GATE_RELEASE_LUA, 1, GATE_KEY)
        except Exception:
            pass  # 计数键由 TTL 兜底

    def _start_watchdog(self) -> None:
        """按配置拉起 Super-agent 看门狗（P1-3）。

        两道闸都读配置：``enabled`` 决定"要不要跑"，``act`` 决定"判断出来是否动手"。
        ``enabled=False`` 时**不创建任务** —— 关掉它必须是真的零影响，而不是"影响很小"。
        """
        try:
            from app.config import settings

            enabled = bool(getattr(settings, "superagent_watchdog_enabled", False))
        except Exception as exc:  # noqa: BLE001 - 读不到配置 = 没开（fail-closed）
            logger.debug("watchdog: config unavailable, staying off (%s)", exc)
            return
        if not enabled or not self._running:
            return
        from app.queue.watchdog_job import watchdog_loop

        self._watchdog_task = asyncio.create_task(
            watchdog_loop(self._redis, should_stop=lambda: not self._running)
        )
        logger.info("Super-agent watchdog started")

    def _start_takeover(self) -> None:
        """按配置拉起 Super-agent 接管巡检。

        与 `_start_watchdog` 同款：``enabled=False`` 时**不创建任务**，
        关掉它必须是真零影响（不空转、不查库、不抢租约）。
        """
        try:
            from app.config import settings

            enabled = bool(getattr(settings, "superagent_takeover_enabled", False))
        except Exception as exc:  # noqa: BLE001 - 读不到配置 = 没开（fail-closed）
            logger.debug("takeover: config unavailable, staying off (%s)", exc)
            return
        if not enabled or not self._running:
            return
        from app.queue.takeover_job import takeover_loop

        self._takeover_task = asyncio.create_task(
            takeover_loop(self._redis, should_stop=lambda: not self._running)
        )
        logger.info("Super-agent takeover loop started")

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._in_flight.discard(task)
        self._semaphore.release()
        if self._gate_limit and self._redis:
            asyncio.create_task(self._release_gate())
        if task.exception():
            logger.error("Task exception: %s", task.exception())

    async def _forward_foreign(
        self,
        stream_id: str,
        task_type: str,
        fields: dict[str, str],
        claim: idempotency.Attempt,
    ) -> None:
        """Atomically forward the unchanged envelope and ACK the owned attempt."""
        target = queue_for_task(task_type)
        moved = await idempotency.forward(
            self._redis,
            claim,
            source=self._q.stream,
            target=target.stream,
            group=self._q.group,
            consumer=self._consumer_name,
            message_id=stream_id,
            fields=fields,
            maxlen=target.maxlen,
        )
        if moved:
            logger.warning(
                "Forwarded foreign task: id=%s type=%s from=%s to=%s",
                stream_id,
                task_type,
                self._q.key,
                target.key,
            )

    async def _process_message(self, stream_id: str, fields: dict[Any, Any]) -> None:
        """Process one message under an exclusive, fenced Redis attempt."""
        message = _text_fields(fields)
        task_type = message.get("task_type", "")
        task_id = message.get("task_id", "")
        payload_raw = message.get("payload", "{}")
        retry_count = _as_int(message.get("retry_count"))
        claim_count = _as_int(message.get("claim_count"))
        tenant_id = _as_int(message.get("tenant_id"))
        # 队列 worker 在**另一个任务/进程**里运行：producer 把 trace_id 写进了
        # envelope，但 contextvars 不会跨过去。不恢复的话这条任务的所有日志
        # 都缺 trace_id，网关侧一跳转就断链。本函数在自己的 task 里跑，
        # 直接用 set 即可（task 结束随之丢弃），不需要 reset。
        if _trace := message.get("trace_id"):
            trace_id_var.set(str(_trace))
        if tenant_id:
            tenant_id_var.set(int(tenant_id))
        idem_key = idempotency.key_for(task_type, task_id, message.get("idempotency_key", ""))
        deadline_raw = message.get("deadline", "")

        decision = await idempotency.claim(
            self._redis,
            namespace=self._q.key,
            key=idem_key,
            task_id=task_id,
            task_type=task_type,
            tenant_id=tenant_id,
            lease_seconds=CLAIM_MIN_IDLE_SECS,
        )
        if decision.status is idempotency.ClaimStatus.COMPLETED:
            await idempotency.ack_completed(
                self._redis,
                namespace=self._q.key,
                key=idem_key,
                stream=self._q.stream,
                group=self._q.group,
                consumer=self._consumer_name,
                message_id=stream_id,
            )
            return
        if decision.status is idempotency.ClaimStatus.BUSY:
            logger.debug("Task already owned: id=%s key=%s", task_id, idem_key)
            return
        claim = decision.claim
        assert claim is not None

        # ── G13：把这条任务的起停变成**会话级事件** ────────────────────────────
        # 修前：起停/完成/失败只存在于队列内部（PEL + idempotency），会话（PG）与前端
        # （SSE）各自靠约定再拼一份"进度"，三份之间没有任何共同来源。失败进 DLQ 时
        # 会话里连一条记录都没有，用户只看到"没有结果"。
        # 现在由 worker 在关键节点发 `job.started` / `job.settled`（经 Redis → Go relay
        # → 会话 SSE），前端只订阅这一处。投递是**旁路**：见下面的 `emit`。
        job_id, session_id = job_events.resolve_job_identity(payload_raw, task_id)

        async def emit(event: str, status: str, error: str = "") -> None:
            """本任务的作业事件。任何失败**只记日志**，绝不影响任务本身。

            ``job_events.emit_job_event`` 内部已经吞掉异常；这里再兜一层，是因为
            "事件投递故障把任务搞失败"这种耦合代价最高，两层都在才敢说绝不外溢。

            为什么**等待**投递而不是丢给后台 task：``started`` 必须在 ``settled`` 之前
            到达（前端的卡片状态机按顺序走），而"每条事件起一个 task"会把这个顺序交给
            事件循环调度。等待的代价只多一次 Redis 往返 —— 这个 worker 本来就在同一条
            连接上做 claim/complete，投递失败也只是记日志，不改变任务的成败。
            """
            try:
                await job_events.emit_job_event(
                    self._redis,
                    event=event,
                    job_id=job_id,
                    task_type=task_type,
                    status=status,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    error=error,
                )
            except Exception as exc:  # noqa: BLE001 - 旁路：只记日志
                logger.warning(
                    "job event emit failed (type=%s id=%s): %s",
                    event,
                    job_id,
                    str(exc)[:160],
                )

        # ── G16：任务级心跳 ────────────────────────────────────────────────────
        # 修前：claim 之后只有 lease 续租，续租失败也只记 debug —— 任务"还占着但不动了"
        # 完全没有痕迹，上层只能等 lease 超时把它当成"失联"重领，且说不出它卡过。
        # 这里先建好对象（未 attach 前 detach 是空操作），保证每一个提前 return 的出口
        # 都走同一个 finally 把记录收掉。
        job_heartbeat = heartbeat.JobHeartbeat(
            self._redis,
            job_id=job_id,
            interval=self._job_heartbeat_interval,
            consumer=self._consumer_name,
        )

        owner = asyncio.current_task()
        lease_heartbeat = (
            asyncio.create_task(self._heartbeat_lease(stream_id, claim, owner))
            if owner is not None
            else None
        )
        start = time.monotonic()
        dispatch_succeeded = False
        try:
            if claim_count > MAX_RECLAIMS:
                moved = await self._move_to_dlq(
                    claim, stream_id, message, f"reclaim attempts exhausted: {claim_count}"
                )
                if moved:
                    logger.warning("Task moved to DLQ: id=%s (claims=%d)", task_id, claim_count)
                # 没跑就被丢弃：只有 settled，没有 started ——
                # 从没执行过的任务不该先闪一张"运行中"的卡片（G13）。
                await emit(
                    job_events.EV_JOB_SETTLED,
                    job_events.ST_DISCARDED,
                    f"reclaim attempts exhausted: {claim_count}",
                )
                return

            if deadline_raw:
                try:
                    deadline = datetime.datetime.strptime(
                        deadline_raw, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=datetime.UTC)
                except ValueError:
                    deadline = None
                if deadline is not None and deadline < datetime.datetime.now(datetime.UTC):
                    logger.warning(
                        "Task expired (deadline=%s), moved to DLQ: id=%s type=%s",
                        deadline_raw,
                        task_id,
                        task_type,
                    )
                    await self._move_to_dlq(
                        claim, stream_id, message, f"deadline exceeded: {deadline_raw}"
                    )
                    await emit(
                        job_events.EV_JOB_SETTLED,
                        job_events.ST_DISCARDED,
                        f"deadline exceeded: {deadline_raw}",
                    )
                    return

            if is_foreign(task_type, self._q):
                # 不是本队列的任务：它会在**目标队列**里发自己的 started/settled。
                # 这里发一条只会让同一任务在会话里出现两份进度 —— 正是要消灭的东西。
                await self._forward_foreign(stream_id, task_type, message, claim)
                return

            # 真正的执行开始：先挂心跳（处理期间持续可见），再发 started。
            # 顺序是刻意的 —— started 一旦发出，前端就开始等"在动"的证据；
            # 先有心跳再发事件，作者不必去想"卡片已经亮了但其实还没挂上心跳"。
            await job_heartbeat.attach(task_type=task_type)
            await emit(job_events.EV_JOB_STARTED, job_events.ST_RUNNING)

            payload = json.loads(payload_raw)
            await self._dispatch(task_type, payload, tenant_id)
            dispatch_succeeded = True
            completed = await idempotency.complete(
                self._redis,
                claim,
                self._q.stream,
                self._q.group,
                self._consumer_name,
                stream_id,
            )
            if not completed:
                logger.warning("Stale task completion ignored: id=%s", task_id)
                # 执行过但完成转移被 fencing 拒了（attempt 已被别人接管）：
                # 对外如实说"这次尝试没落地"，而不是留一张永远"运行中"的卡片。
                await emit(
                    job_events.EV_JOB_SETTLED,
                    job_events.ST_FAILED,
                    "completion transition rejected (attempt fenced off)",
                )
                return
            elapsed = time.monotonic() - start
            QUEUE_PROCESSING_DURATION.labels(task_type=task_type).observe(elapsed)
            logger.info(
                "Task completed: id=%s type=%s (%.2fs)", task_id, task_type, elapsed
            )
            await emit(job_events.EV_JOB_SETTLED, job_events.ST_COMPLETED)

        except RetryLaterError as e:
            # "等一会儿再试"：条件（会话锁 / 并发配额 / 上游暂时不可达）会自行好转。
            # 既不是失败，就不能走下面的"消耗重试预算"路径 —— 立即重投的预算会在
            # 毫秒级耗尽并进 DLQ，而这里要等的条件通常要几十秒才释放。
            logger.warning(
                "Task deferred: id=%s type=%s reason=%s", task_id, task_type, str(e)[:200]
            )
            # 本次**尝试**结束（不是任务终态）：deferred 收掉这次的卡片说清"在等"，
            # 重投后会再发一条 started。混成"还在跑"，前端就只能一直转圈。
            await emit(job_events.EV_JOB_SETTLED, job_events.ST_DEFERRED, str(e)[:200])
            await self._defer_message(claim, stream_id, message, e)

        except Exception as e:
            if dispatch_succeeded:
                logger.error("Task completion transition failed: id=%s error=%s", task_id, e)
                raise
            logger.error("Task failed: id=%s type=%s error=%s", task_id, task_type, e)
            await emit(job_events.EV_JOB_SETTLED, job_events.ST_FAILED, str(e))
            if retry_count >= MAX_RETRIES:
                moved = await self._move_to_dlq(claim, stream_id, message, str(e))
                if moved:
                    logger.warning(
                        "Task moved to DLQ: id=%s (retries=%d)", task_id, retry_count
                    )
            else:
                await self._retry_message(claim, stream_id, message, task_type)

        finally:
            if lease_heartbeat is not None:
                lease_heartbeat.cancel()
                await asyncio.gather(lease_heartbeat, return_exceptions=True)
            # 无论成功/失败/丢弃/延迟重投都收掉心跳：留着它，下一轮扫描就会把
            # 已经结束的任务算成"卡住"（G16）。
            await job_heartbeat.detach()

    # ── 延迟重试与死信 ──

    async def _move_to_dlq(
        self,
        claim: idempotency.Attempt,
        stream_id: str,
        message: dict[str, str],
        error: str,
    ) -> bool:
        """Atomically preserve the full envelope in DLQ and ACK this attempt."""
        fields = dict(message)
        fields["error"] = error
        moved = await idempotency.dead_letter(
            self._redis,
            claim,
            source=self._q.stream,
            target=self._q.dlq,
            group=self._q.group,
            consumer=self._consumer_name,
            message_id=stream_id,
            fields=fields,
            maxlen=self._q.dlq_maxlen,
        )
        if moved:
            QUEUE_DLQ_TOTAL.labels(task_type=fields.get("task_type", "")).inc()
        return moved

    async def _retry_message(
        self,
        claim: idempotency.Attempt,
        stream_id: str,
        message: dict[str, str],
        task_type: str,
    ) -> bool:
        """Atomically requeue the unchanged envelope with an incremented retry budget."""
        fields = dict(message)
        fields["retry_count"] = str(_as_int(fields.get("retry_count")) + 1)
        moved = await idempotency.retry(
            self._redis,
            claim,
            source=self._q.stream,
            target=self._q.stream,
            group=self._q.group,
            consumer=self._consumer_name,
            message_id=stream_id,
            fields=fields,
            maxlen=self._q.maxlen,
        )
        if moved:
            QUEUE_RETRY_TOTAL.labels(task_type=task_type).inc()
            logger.info(
                "Task re-queued for retry: id=%s (retry=%s/%d)",
                fields.get("task_id", ""),
                fields["retry_count"],
                MAX_RETRIES,
            )
        return moved

    @staticmethod
    def _defer_delay(attempts: int, exc: RetryLaterError) -> float:
        """退避时长：指数增长，上限 ``DELAY_MAX_SECONDS``。

        上游给了明确等待时长（如 Retry-After）时以它作基值，否则用
        ``DELAY_BASE_SECONDS``。会话锁的典型持有时长是"一个 turn"（数十秒到数分钟），
        所以基值不能太小，否则会白跑很多轮。
        """
        base = exc.delay_seconds or DELAY_BASE_SECONDS
        return float(min(base * (2 ** max(0, attempts - 1)), DELAY_MAX_SECONDS))

    async def _defer_message(
        self,
        claim: idempotency.Attempt,
        stream_id: str,
        message: dict[str, str],
        exc: RetryLaterError,
    ) -> None:
        """Atomically defer without spending the ordinary failure retry budget."""
        task_id = message.get("task_id", "")
        task_type = message.get("task_type", "")
        attempts = _as_int(message.get("delay_count")) + 1
        if attempts > DELAY_MAX_ATTEMPTS:
            logger.error(
                "Task deferred %d times, giving up: id=%s type=%s reason=%s",
                attempts - 1,
                task_id,
                task_type,
                str(exc)[:200],
            )
            await self._move_to_dlq(
                claim,
                stream_id,
                message,
                f"deferred retry limit ({DELAY_MAX_ATTEMPTS}) reached: {exc}",
            )
            return

        message = dict(message)
        message["delay_count"] = str(attempts)
        delay = self._defer_delay(attempts, exc)
        member = json.dumps(message, ensure_ascii=False)
        try:
            deferred = await idempotency.defer(
                self._redis,
                claim,
                source=self._q.stream,
                target=self._q.delayed,
                group=self._q.group,
                consumer=self._consumer_name,
                message_id=stream_id,
                member=member,
                score=time.time() + delay,
            )
        except Exception as fallback_error:  # noqa: BLE001
            logger.warning(
                "Task defer failed (%s), falling back to immediate requeue: id=%s",
                str(fallback_error)[:160],
                task_id,
            )
            if _as_int(message.get("retry_count")) >= MAX_RETRIES:
                await self._move_to_dlq(
                    claim,
                    stream_id,
                    message,
                    f"defer failed and retries exhausted: {exc}",
                )
            else:
                message.pop("delay_count", None)
                await self._retry_message(claim, stream_id, message, task_type)
            return
        if not deferred:
            return
        logger.warning(
            "Task deferred %.0fs (attempt %d/%d): id=%s type=%s reason=%s",
            delay,
            attempts,
            DELAY_MAX_ATTEMPTS,
            task_id,
            task_type,
            str(exc)[:160],
        )

    async def _delay_loop(self) -> None:
        """周期性把到期的延迟任务搬回主队列。"""
        while self._running:
            try:
                await asyncio.sleep(DELAY_LOOP_SECS)
                await self._promote_delayed()
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                logger.warning("worker delayed-retry loop error: %s", e)

    async def _promote_delayed(self, now: float | None = None) -> int:
        """Atomically promote due members; only one instance can remove each one."""
        ts = time.time() if now is None else now
        try:
            members: list[Any] = await self._redis.zrangebyscore(
                self._q.delayed, "-inf", ts, start=0, num=DELAY_BATCH
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("worker delayed scan failed: %s", e)
            return 0
        promoted = 0
        for raw in members or []:
            member = raw.decode() if isinstance(raw, bytes) else str(raw)
            try:
                added = await atomic_promote_delayed(
                    self._redis,
                    delayed=self._q.delayed,
                    target=self._q.stream,
                    member=member,
                    maxlen=self._q.maxlen,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("worker delayed promotion failed: %s", e)
                continue
            if added is not None:
                promoted += 1
        return promoted

    async def _dispatch(
        self, task_type: str, payload: dict[Any, Any], tenant_id: int = 0
    ) -> None:
        """分发任务到具体处理器"""
        if task_type == "memory_save":
            await self._handle_memory_save(payload, tenant_id)
        elif task_type == "memory_consolidate":
            await self._handle_memory_consolidate(payload, tenant_id)
        elif task_type == "memory_rollup":
            await self._handle_memory_rollup(payload, tenant_id)
        elif task_type == "tool_job":
            await self._handle_tool_job(payload)
        elif task_type == "agent_followup":
            await self._handle_agent_followup(payload)
        else:
            # 未知任务类型：显式失败（回队重投）而非 ACK 丢弃。
            # 滚动升级期间旧版 worker 收到新版 task_type（tool_job 等）时，
            # 抛错使消息 retry++ 回队尾，由新版 worker 重取；超 MAX_RETRIES 进 DLQ，
            # 避免新类型任务在升级窗口被静默确认丢弃。
            raise ValueError(f"unknown task type: {task_type}")

    async def _handle_agent_followup(self, payload: dict[Any, Any]) -> None:
        """子 Agent 完成 → 请网关在父会话上开新一轮（真正的执行在 Go）。

        payload: {run_id, session_id, tenant_id, user_id, status, profile, depth, summary}

        为什么不在引擎侧开新一轮：``messages`` 落库、SSE 推送、turn 状态、计费与会话锁
        全在 Go；引擎侧自己跑一遍，这一轮在对话里是"看不见"的（见
        ``app/subagent/followup.py`` 与 ``internal/api/agent_followup.go``）。
        """
        run_id = str(payload.get("run_id") or "")
        session_id = str(payload.get("session_id") or "")
        if not run_id or not session_id:
            logger.warning("agent_followup payload 不完整: %s", payload)
            return

        from app.config import settings

        if not settings.internal_token:
            raise RuntimeError("agent_followup 需要 internal_token 才能调用网关")

        from app.http_client import shared_client

        url = f"{settings.gateway_internal_url.rstrip('/')}/v1/internal/agent-followup"
        headers = {"X-Internal-Token": settings.internal_token}
        try:
            async with shared_client(timeout=20.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except Exception as exc:  # noqa: BLE001 - 网关暂时不可达（重启/滚动升级）
            raise RetryLaterError(f"agent_followup transport error: {exc}") from exc

        if resp.status_code == 409:
            # 该会话正在跑别的 turn：**要等**，不是失败。走延迟重试，不消耗重试预算。
            raise RetryLaterError(
                "agent_followup: session busy (409)", delay_seconds=_retry_after(resp)
            )
        if resp.status_code == 429:
            # 实例/租户并发已满：同样只是要等
            raise RetryLaterError(
                "agent_followup: too many requests (429)", delay_seconds=_retry_after(resp)
            )
        if 400 <= resp.status_code < 500:
            # 载荷问题：重试也没用（重试只会把同一条坏消息反复送到 DLQ）。
            # 明确记录并 ACK —— 可见地失败，而不是排队空转。
            logger.error(
                "agent_followup rejected by gateway (no retry): status=%s run=%s session=%s body=%s",
                resp.status_code, run_id, session_id, resp.text[:200],
            )
            return
        if not resp.is_success:
            raise RuntimeError(
                f"agent_followup rejected: {resp.status_code} {resp.text[:200]}"
            )
        logger.info("agent_followup accepted: run=%s session=%s", run_id, session_id)

    async def _handle_tool_job(self, payload: dict[Any, Any]) -> None:
        """处理后台命令任务（run_in_background 队列化）：独立子进程执行 + 结果写 Redis。

        payload: {job_id, command, shell_key} + 投递时的身份（tenant_id/user_id/session_id）
        """
        from app.tools.job_runner import execute_tool_job, restore_job_context

        job_id = payload.get("job_id") or ""
        command = payload.get("command") or ""
        if not job_id or not command:
            logger.warning("tool_job payload 缺失: %s", payload)
            return
        # worker 的 contextvars 是空的：先从载荷恢复身份，再执行 —— 否则 exec_audit
        # 只能记下"执行了什么"，答不出"谁执行的"。
        restore_job_context(payload)
        res = await execute_tool_job(self._redis, job_id, command)
        logger.info(
            "tool_job done: job=%s status=%s exit_code=%s",
            job_id, res.get("status"), res.get("exit_code"),
        )
        await self._notify_job_finished(payload, res)

    async def _notify_job_finished(
        self, payload: dict[Any, Any], res: dict[str, Any]
    ) -> None:
        """后台命令结束 → 请网关在源会话上开新一轮（P0-1）。

        复用 agent_followup 那条已验证的通道（幂等 + 限流 + 会话锁 + 计费预检全在网关），
        不另造一套。**取消不发**：那是用户自己停的，再唤起一轮只是噪音。

        开关不在这里判：`users.settings` 只有网关能读，引擎无从知道用户要不要。
        """
        status = str(res.get("status") or "")
        if status not in ("completed", "failed"):
            return
        job_id = str(payload.get("job_id") or "")
        session_id = str(payload.get("session_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not job_id or not session_id or not user_id:
            return

        from app.subagent.followup import enqueue_followup
        from app.tools.context import to_tenant_int

        output = str(res.get("output") or "")
        # 取尾部：错误与结论在那里，头部通常只是命令回显。
        tail = output[-1800:] if len(output) > 1800 else output
        try:
            await enqueue_followup(
                run_id=f"job:{job_id}",
                session_id=session_id,
                tenant_id=to_tenant_int(payload.get("tenant_id")),
                user_id=user_id,
                status=status,
                summary=tail,
                profile="tool_job",
                kind="job",
            )
        except Exception as exc:  # noqa: BLE001 - 回投失败不能影响作业本身的终态
            logger.warning("tool_job followup 投递失败: job=%s err=%s", job_id, str(exc)[:160])

    async def _handle_memory_save(self, payload: dict[Any, Any], tenant_id: int = 0) -> None:
        """处理记忆持久化任务

        payload: {key, value, source, user_id?, slot?, confidence?}
        """
        key = payload.get("key")
        if not key:
            raise ValueError("memory_save payload 缺少 key")
        if not self._memory_service:
            raise RuntimeError("memory_save 需要 memory_service 支持")

        from app.memory.layers import SlotType, SourceType

        slot = SlotType(payload.get("slot", "fact"))
        source = SourceType(payload.get("source", "derived"))
        confidence = int(payload.get("confidence", 50))
        user_id = payload.get("user_id", "")

        await self._memory_service.update_profile(
            tenant_id=tenant_id,
            user_id=user_id,
            slot=slot,
            item_key=key,
            item_value=str(payload.get("value", "")),
            confidence=confidence,
            source=source,
        )
        logger.info("memory_save 完成: key=%s tenant=%s", key, tenant_id)

    async def _handle_memory_consolidate(self, payload: dict[Any, Any], tenant_id: int) -> None:
        """处理记忆巩固任务：将对话消息巩固为 L3 摘要。

        payload: {session_id, user_id, turn_count, trigger}
        """
        if not self._memory_service:
            raise RuntimeError("memory_consolidate 需要 memory_service 支持")

        session_id = payload.get("session_id")
        user_id = payload.get("user_id", "")
        if not session_id:
            raise ValueError("memory_consolidate payload 缺少 session_id")

        # 从会话历史获取消息（简化：后续集成 ContextManager）
        messages = await self._get_session_messages(tenant_id, user_id, session_id)
        if not messages:
            logger.warning("No messages to consolidate: session=%s", session_id)
            return

        turn_count = int(payload.get("turn_count", 0) or 0)
        # 走 MemoryService 而不是就地再建一个 Consolidator：过去这里 `Consolidator(store=…)`
        # 只给了 store，embedder/summariser 都是空的 —— 摘要退化成抽取式、向量恒为 NULL，
        # 与 app/main.py 装配的那一个口径分裂。装配只该有一处。
        # turn 区间如实记 0..turn_count：`_get_session_messages` 取的是**整段会话**，
        # 过去标成 `turn_count-10` 会让摘要的 lineage 与实际覆盖内容不符。
        outcome = await self._memory_service.save_summary(
            tenant_id,
            user_id,
            session_id,
            messages,
            0,
            turn_count,
        )

        if outcome.get("error"):
            # 抛出而不是记一条 error 日志就 ACK：巩固失败是"该写的记忆没写"，
            # 静默确认等于永久丢失。走 worker 的重试→DLQ，失败至少看得见。
            raise RuntimeError(f"consolidate failed: {outcome['error']}")
        if outcome.get("skipped"):
            # 模型判定"这一窗没什么值得记" —— 正常收口，不是失败，不该重试。
            logger.info("Consolidate skipped: session=%s 无值得长期记忆的内容", session_id)
        elif outcome.get("deduplicated"):
            logger.debug("Consolidate deduplicated: session=%s", session_id)
        else:
            logger.info(
                "Consolidate completed: session=%s, summary_id=%s",
                session_id,
                (outcome.get("summary") or {}).get("id"),
            )

    async def _handle_memory_rollup(self, payload: dict[Any, Any], tenant_id: int) -> None:
        """处理记忆 rollup 任务：会话结束时的总结归档。

        payload: {session_id, user_id, trigger}
        """
        if not self._memory_service:
            raise RuntimeError("memory_rollup 需要 memory_service 支持")

        session_id = payload.get("session_id")
        user_id = payload.get("user_id", "")
        if not session_id:
            raise ValueError("memory_rollup payload 缺少 session_id")

        # 获取会话全部消息
        messages = await self._get_session_messages(tenant_id, user_id, session_id)
        if not messages:
            logger.debug("No messages to rollup: session=%s", session_id)
            return

        # 调用 Consolidator 进行完整 rollup —— 同样经 MemoryService，理由见
        # `_handle_memory_consolidate`。
        outcome = await self._memory_service.save_summary(
            tenant_id,
            user_id,
            session_id,
            messages,
            0,
            len(messages),
        )

        if outcome.get("error"):
            raise RuntimeError(f"rollup failed: {outcome['error']}")
        logger.info(
            "Rollup completed: session=%s, messages=%d, summary_id=%s",
            session_id,
            len(messages),
            (outcome.get("summary") or {}).get("id"),
        )

    async def _get_session_messages(
        self, tenant_id: int, user_id: str, session_id: str
    ) -> list[dict[str, Any]]:
        """获取会话消息（从数据库）。"""
        from app.db import get_pool

        pool = get_pool()
        # 租户条件不能省：巩固是"把会话内容写进长期记忆"的写入动作，此前只按 session_id
        # 取消息（`session_id` 有 FK、全局唯一，所以不是已证实的越权，但任务信封里的
        # tenant_id 一旦被投错就会白送给别人加记忆，且没有任何一道条件会拦）。
        # unified_messages 自己没有租户列，只能经 unified_sessions 关联。
        rows = await pool.fetch(
            """SELECT m.role, m.content
               FROM unified_messages m
               JOIN unified_sessions s ON s.id = m.session_id
               WHERE m.session_id = $1 AND s.tenant_id = $2
               ORDER BY m.created_at ASC""",
            session_id,
            tenant_id,
        )
        messages = [
            {"role": row["role"], "content": row["content"]}
            for row in rows
            if row["content"] and isinstance(row["content"], str)
        ]
        if not messages:
            # 空结果要分两种：会话真没内容，还是这个会话不属于该租户（含历史会话的
            # tenant_id 为 NULL）。前者正常，后者是配置/数据问题，必须说出来 ——
            # "记忆为什么没生效"不该变成一个查不出来的问题。
            owner = await pool.fetchval(
                "SELECT tenant_id FROM unified_sessions WHERE id = $1", session_id
            )
            if owner is not None and owner != tenant_id:
                logger.warning(
                    "Skipping consolidation for session=%s: 会话属于租户 %r，任务租户是 %r",
                    session_id,
                    owner,
                    tenant_id,
                )
            elif owner is None:
                logger.warning(
                    "Skipping consolidation for session=%s: unified_sessions 里没有这个会话",
                    session_id,
                )
        return messages

    async def update_queue_depth(self) -> None:
        """更新队列深度指标"""
        try:
            info = await self._redis.xinfo_stream(self._q.stream)
            QUEUE_DEPTH.labels(stream=self._q.stream).set(info.get("length", 0))
        except Exception:
            logger.warning("Failed to update queue depth metric")
