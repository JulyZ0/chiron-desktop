"""任务级心跳（G16）—— 让"卡住"与"在跑"可区分。

## 修前会怎样

worker 只在 claim 时写一次 ``lease_until``，之后由 ``_heartbeat_lease`` 续租；而续租
失败只记 debug 日志，任务**内部**的停顿（卡在一个永不返回的 await 上）没有任何痕迹。
上层于是只有两种判断：

* "PEL 里还有它" —— 在跑？（也可能早就卡住了）
* "PEL 里没了" —— 结束了？（也可能是别的原因）

第三种状态"还占着但不动了"看不见，只能等 lease 超时（默认 10 分钟）被别的实例重领，
而且重领之后也没有任何证据说明上一次是卡住的。

## 修后

处理任务期间周期性写 ``queue:job:heartbeat:{job_id}``（hash）：

=============  ==========================================================
``heartbeat_at`` 最后一次心跳的墙钟秒（跨实例比较用，不要求单调）
``started_at``  本次尝试开始的时间
``beats``       心跳次数 —— "在动"的直接证据，也让测试不必依赖时间精度
``task_type``   指标标签
``consumer``    哪个 worker 实例在跑（多实例排查用）
=============  ==========================================================

**为什么用墙钟而不是 Redis TIME**：这是一个**只读**查询面（上层随时来问"卡住了吗"），
比较对象是"现在"。多读一次 Redis 时钟换来的严格性在这里不值得：代价只是 NTP 回拨时
某个任务显得"更新"，多等一轮扫描而已。

## 完成即删除

正常收口（成功/失败/丢弃/延迟重投）时删掉这条记录。留着它，下一次扫描就会把它算成
"卡住"，于是这个信号会被历史噪声淹没 —— 一个永远在响的告警等于没有告警。
TTL 只是兜底（进程被杀、没人来 detach）。

## 旁路

与 G13 事件同款：写心跳失败只记日志，任务照跑。心跳是观测，不是流程的一部分。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from app.redis_keys import rkey

logger = logging.getLogger(__name__)

#: 心跳周期（秒）。比 lease 续租（``HEARTBEAT_SECS``）短得多：续租的频率只影响
#: "多久能被别的实例接管"，而心跳决定"多久能看出卡住"。
DEFAULT_INTERVAL_SECONDS = max(0.05, float(os.getenv("QUEUE_JOB_HEARTBEAT_SECS", "30")))
#: 判定阈值 = interval × 该倍数。默认 3 ⇒ 至少漏两拍才判 stale，
#: 单拍抖动（GC、一次 Redis 抖动）不至于把正常任务报成卡住。
DEFAULT_STALE_MULTIPLIER = max(1.0, float(os.getenv("QUEUE_JOB_STALE_MULTIPLIER", "3")))

HEARTBEAT_KEY_PREFIX = "queue:job:heartbeat:"


def heartbeat_key(job_id: str) -> str:
    """某条作业的心跳键。"""
    return rkey(f"{HEARTBEAT_KEY_PREFIX}{job_id}")


def heartbeat_pattern() -> str:
    """扫描用的匹配串（只读查询用）。"""
    return rkey(f"{HEARTBEAT_KEY_PREFIX}*")


def stale_threshold(
    *,
    interval: float | None = None,
    multiplier: float | None = None,
) -> float:
    """多久没心跳算"卡住"（秒）。"""
    step = DEFAULT_INTERVAL_SECONDS if interval is None else float(interval)
    factor = DEFAULT_STALE_MULTIPLIER if multiplier is None else float(multiplier)
    return step * factor


@dataclass(frozen=True)
class HeartbeatInfo:
    """一条心跳记录的只读视图。"""

    job_id: str
    task_type: str
    heartbeat_at: float
    started_at: float
    beats: int
    consumer: str = ""
    key: str = ""

    def age(self, now: float | None = None) -> float:
        """距最后一次心跳过了多少秒（负数说明时钟被回拨过）。"""
        return (time.time() if now is None else float(now)) - self.heartbeat_at

    def is_stale(
        self,
        *,
        now: float | None = None,
        interval: float | None = None,
        multiplier: float | None = None,
    ) -> bool:
        """超过 N 倍心跳间隔没更新 = 卡住。

        ``beats`` 不参与判定：卡住的任务本来就不该有心跳，而"心跳很多次但最后一条很旧"
        与"只跳过一次然后停了"对上层是同一件事 —— 都该告警。
        """
        return self.age(now) > stale_threshold(interval=interval, multiplier=multiplier)


def _text(raw: dict[Any, Any], name: str) -> str:
    value = raw.get(name, raw.get(name.encode()))
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode()
        except Exception:  # noqa: BLE001 - 解不出来按缺失处理
            return ""
    return "" if value is None else str(value)


def _number(raw: dict[Any, Any], name: str, default: float = 0.0) -> float:
    try:
        return float(_text(raw, name))
    except (TypeError, ValueError):
        return default


def parse_heartbeat(key: str, raw: dict[Any, Any]) -> HeartbeatInfo | None:
    """把 hash 解析成 ``HeartbeatInfo``；读不到 ``heartbeat_at`` 时返回 ``None``。

    没有 ``heartbeat_at`` 的记录无法判定年龄 —— 宁可当作"没有记录"（上层不看），
    也不要拿 ``0`` 当时间戳把它算成"落后了几十年"。
    """
    if not raw or not _text(raw, "heartbeat_at"):
        return None
    job_id = _text(raw, "job_id") or key.rsplit(":", 1)[-1]
    return HeartbeatInfo(
        job_id=job_id,
        task_type=_text(raw, "task_type"),
        heartbeat_at=_number(raw, "heartbeat_at"),
        started_at=_number(raw, "started_at"),
        beats=int(_number(raw, "beats")),
        consumer=_text(raw, "consumer"),
        key=key,
    )


class JobHeartbeat:
    """一条在途任务的心跳（旁路：任何失败只记日志）。"""

    def __init__(
        self,
        redis: Any,
        *,
        job_id: str,
        interval: float | None = None,
        clock: Any = time.time,
        consumer: str = "",
        ttl_seconds: float | None = None,
    ) -> None:
        self._redis = redis
        self._job_id = job_id or ""
        self._interval = (
            DEFAULT_INTERVAL_SECONDS if interval is None else max(0.01, float(interval))
        )
        self._clock = clock
        self._consumer = consumer
        # TTL 必须**明显大于**判定阈值：否则记录会先过期，"卡住"就退化成
        # "记录不存在"，与"正常完成"（同样是没记录）再也分不开。
        default_ttl = max(self._interval * DEFAULT_STALE_MULTIPLIER * 4, 60.0)
        self._ttl_ms = int((default_ttl if ttl_seconds is None else float(ttl_seconds)) * 1000)
        self._started_at = 0.0
        self._task_type = ""
        self._beats = 0
        self._attached = False
        self._task: asyncio.Task[Any] | None = None

    @property
    def job_id(self) -> str:
        return self._job_id

    @property
    def key(self) -> str:
        return heartbeat_key(self._job_id)

    async def attach(self, *, task_type: str = "") -> None:
        """写第一条心跳并开始周期刷新。``job_id`` 为空时什么都不做。"""
        if not self._job_id:
            return
        self._task_type = task_type or ""
        self._started_at = float(self._clock())
        self._attached = True
        await self._write()
        self._task = asyncio.create_task(self._loop())

    async def beat(self) -> None:
        """手动补一拍（测试与"长任务的分段边界"都用它）。"""
        await self._write()

    async def detach(self) -> None:
        """停止刷新并**删掉**记录：正常收口的任务不该在扫描里留下痕迹。

        没 attach 过（任务没跑就丢弃/转发走了）就什么都不做 —— 那种路径本来没有记录，
        去 DELETE 一个从未写过的键只会给"没跑"的任务也加一次 Redis 往返。
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await asyncio.gather(self._task, return_exceptions=True)
            except Exception:  # noqa: BLE001 - 收尾失败不影响主流程
                pass
            self._task = None
        if not self._attached:
            return
        self._attached = False
        try:
            await self._redis.delete(self.key)
        except Exception as exc:  # noqa: BLE001 - 删不掉最多留一个 TTL
            logger.debug("job heartbeat delete failed (id=%s): %s", self._job_id, str(exc)[:120])

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return
            await self._write()

    async def _write(self) -> None:
        if not self._job_id:
            return
        try:
            await self._redis.hset(
                self.key,
                mapping={
                    "job_id": self._job_id,
                    "task_type": self._task_type,
                    # 保留 3 位小数：扫描与断言都要"秒"级精度，而 float 全量会
                    # 让 fakeredis 的往返出现肉眼可见的尾数差异。
                    "heartbeat_at": f"{float(self._clock()):.3f}",
                    "started_at": f"{self._started_at:.3f}",
                    "beats": str(self._beats + 1),
                    "consumer": self._consumer,
                },
            )
            await self._redis.pexpire(self.key, self._ttl_ms)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 心跳失败只记日志，任务照跑
            logger.debug("job heartbeat write failed (id=%s): %s", self._job_id, str(exc)[:120])
            return
        self._beats += 1


# ── 只读查询（上层判断"卡住"的入口）────────────────────────────────────────


async def read_heartbeat(redis: Any, job_id: str) -> HeartbeatInfo | None:
    """读一条心跳；读不到（或 Redis 不可用）返回 ``None``。"""
    if not job_id:
        return None
    key = heartbeat_key(job_id)
    try:
        raw = await redis.hgetall(key)
    except Exception as exc:  # noqa: BLE001 - 查询失败等于"不知道"，不等于"卡住"
        logger.debug("job heartbeat read failed (id=%s): %s", job_id, str(exc)[:120])
        return None
    return parse_heartbeat(key, raw or {})


async def scan_heartbeats(redis: Any, *, limit: int = 500) -> list[HeartbeatInfo]:
    """列出当前所有心跳记录（只读；Redis 读不到时返回已读到的部分）。"""
    pattern = heartbeat_pattern()
    found: list[HeartbeatInfo] = []
    try:
        async for key in redis.scan_iter(match=pattern, count=100):
            name = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            try:
                raw = await redis.hgetall(name)
            except Exception as exc:  # noqa: BLE001 - 单条读失败不阻断整轮
                logger.debug("job heartbeat scan read failed (%s): %s", name, str(exc)[:120])
                continue
            info = parse_heartbeat(name, raw or {})
            if info is not None:
                found.append(info)
            if len(found) >= limit:
                break
    except Exception as exc:  # noqa: BLE001 - 扫描失败按"没看到任何记录"处理
        logger.debug("job heartbeat scan failed: %s", str(exc)[:120])
        return found
    return found


def _count_stale(task_type: str) -> None:
    """stale 计数。指标模块不可用时静默跳过。"""
    try:
        from app.observability.metrics import QUEUE_JOB_STALE

        QUEUE_JOB_STALE.labels(task_type=task_type or "-").inc()
    except Exception:  # noqa: BLE001
        return


async def scan_stale_heartbeats(
    redis: Any,
    *,
    now: float | None = None,
    interval: float | None = None,
    multiplier: float | None = None,
    limit: int = 500,
    count_metric: bool = True,
) -> list[HeartbeatInfo]:
    """"哪些任务卡住了" —— 只读判断入口。

    每次发现一条 stale 记录就给 ``queue_job_stale_total`` 记一次（多实例各记各的），
    并留一条 warning 日志。**不改任何队列状态**：卡住之后怎么处置（放弃、重投、
    叫人来看）是上层的决定，这一层只负责把事实说出来。
    """
    stale: list[HeartbeatInfo] = []
    for info in await scan_heartbeats(redis, limit=limit):
        if not info.is_stale(now=now, interval=interval, multiplier=multiplier):
            continue
        stale.append(info)
        if count_metric:
            _count_stale(info.task_type)
        logger.warning(
            "job heartbeat stale: job=%s type=%s consumer=%s age=%.1fs (threshold=%.1fs)",
            info.job_id,
            info.task_type or "-",
            info.consumer or "-",
            info.age(now),
            stale_threshold(interval=interval, multiplier=multiplier),
        )
    return stale
