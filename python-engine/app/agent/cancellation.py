"""取消传播到**当前调用**（H9）：模型请求 / 工具调用 / 子进程 / 子 Agent。

## 为什么需要这一层

`app/agent/interrupt.py` 只负责**信号**（本地快路径 + Redis 跨副本围栏），
`runtime.run()` 也只在**轮次边界**查它。于是一条长工具调用、一段长模型流、
一个还在跑的子进程或子 Agent，会在用户点下"停止"之后继续跑完 —— 用户以为停了，
实际还在写文件、还在烧 token、还在花额度。真正的取消必须让**正在跑的那一件事**
停下，并且给"停不下来"一个有上限的收口。

## 最长收敛时间（可配置，默认值即口径）

    AGENT_CANCEL_CONVERGENCE_SECONDS  默认 2.0
    AGENT_CANCEL_POLL_SECONDS         默认 min(0.1, convergence/4)

一次取消从"信号落盘"到"当前调用停下"的最长等待 =

    poll_interval（发现信号的最坏延迟，Redis 往返也算在内）
  + terminate_grace（已经要求停下之后，等它真的收尾的宽限）

其中 `terminate_grace = convergence - poll_interval`，所以**总和不超过
`convergence`**。超过宽限仍未收尾的调用**不再无限等**：记警告、返回"结果未知"，
把"我停不下来"如实暴露出来（宁可让上层看见未知，也不假装一切正常）。

## 结果语义：已产生外部动作时是"未知"，不是成功

- 只读调用被取消：`{"cancelled": True, "outcome": "cancelled"}` —— 没副作用，说清没跑完；
- 可能产生外部动作（write/delete/external，含未声明工具 → fail-closed 归 write）：
  `{"cancelled": True, "outcome": "unknown", "result_unknown": True}` —— 到底改没改
  没人知道，**不能回一个"成功"给模型**（模型会据此宣布完成，用户看到的是假成功）。

## 子进程

`communicate_or_cancel()` 是 `proc.communicate()` 的可取消版本：取消或超时都先
**杀进程**再向上抛。子进程在父调用已被取消之后还活着，是最典型的"假取消"。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

#: 取消从"信号"到"当前调用停下"的默认最长收敛时间（秒）。
DEFAULT_CONVERGENCE_SECONDS = 2.0

#: 取消信号轮询间隔的上限（秒）：轮询是本地 dict 查找，绝大多数情况下零网络往返。
DEFAULT_POLL_INTERVAL_SECONDS = 0.1

#: 收敛时间的可配置上下限：太小会让"点停止"变成碰运气，太大等于没有上限。
_MIN_CONVERGENCE_SECONDS = 0.2
_MAX_CONVERGENCE_SECONDS = 30.0


def _env_float(name: str, default: float | None) -> float | None:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number — falling back to %s", name, raw, default)
        return default
    return value if value > 0 else default


def convergence_seconds() -> float:
    """一次取消的最长收敛时间（秒）。"""
    value = _env_float("AGENT_CANCEL_CONVERGENCE_SECONDS", DEFAULT_CONVERGENCE_SECONDS)
    assert value is not None  # _env_float 的 default 非 None 时不会返回 None
    return min(max(float(value), _MIN_CONVERGENCE_SECONDS), _MAX_CONVERGENCE_SECONDS)


def poll_interval_seconds() -> float:
    """取消信号的轮询间隔（秒），且不超过收敛时间的四分之一。"""
    ceiling = convergence_seconds() / 4.0
    value = _env_float(
        "AGENT_CANCEL_POLL_SECONDS", min(DEFAULT_POLL_INTERVAL_SECONDS, ceiling)
    )
    assert value is not None
    return max(min(float(value), ceiling), 0.01)


def terminate_grace_seconds() -> float:
    """已要求停下之后，等它真的收尾的宽限（秒）—— 与轮询合计不超过收敛时间。"""
    return max(convergence_seconds() - poll_interval_seconds(), 0.1)


async def poll_cancel(session_id: str, *, redis: Any = None) -> bool:
    """轮询中断信号：**观测到取消**才返回 True；否则一直等（由调用方取消）。

    为什么返回 bool 而不是"True/永不返回"：没有 `session_id`（合成子会话、单测替身）
    时不存在可观测的信号，这时应当**继续等调用本身**，而不是立刻当成取消。
    读失败（Redis 抖动）按"读不到"处理，不当成取消 —— 与 `probe_interrupt` 同一口径：
    读不到不等于用户下了令。
    """
    from app.agent.interrupt import is_interrupted

    if not session_id:
        return False
    interval = poll_interval_seconds()
    while True:
        try:
            if await is_interrupted(session_id, redis=redis):
                return True
        except asyncio.CancelledError:  # pragma: no cover - 观察者自己被杀
            raise
        except Exception as exc:  # noqa: BLE001 - 读失败不是取消
            logger.debug("cancel poll failed (session=%s): %s", session_id, exc)
        await asyncio.sleep(interval)


async def terminate_process(proc: Any, *, grace: float | None = None) -> bool:
    """杀掉进程并**确认它真的结束了**；返回是否确认收尾。

    为什么必须确认：`proc.kill()` 只是发信号。不 wait 就返回，调用方会以为"停了"，
    而进程可能还在写文件 —— 这正是"假取消"的另一半。
    """
    if proc is None:
        return True
    if getattr(proc, "returncode", None) is not None:
        return True
    try:
        proc.kill()
    except ProcessLookupError:
        return True
    except Exception as exc:  # noqa: BLE001 - 进程已退出/句柄失效都按"已停"处理
        logger.debug("cancel: kill failed: %s", exc)
        return True
    limit = grace if grace is not None else terminate_grace_seconds()
    wait_task: asyncio.Future[Any] = asyncio.ensure_future(proc.wait())
    try:
        done, _ = await asyncio.wait({wait_task}, timeout=limit)
    except asyncio.CancelledError:
        wait_task.cancel()
        raise
    if not done:
        logger.warning(
            "cancel: process did not exit within %.2fs after kill (pid=%s)",
            limit,
            getattr(proc, "pid", "?"),
        )
        wait_task.cancel()
        return False
    return getattr(proc, "returncode", None) is not None


async def communicate_or_cancel(proc: Any, *, timeout: float) -> tuple[bytes, bytes]:
    """`proc.communicate()` 的可取消版本：取消/超时都先杀进程，再原样抛出。

    调用方（沙箱执行）只关心"拿到了输出"或"这两种异常之一"，进程回收在这里统一做，
    避免每个执行点各写一份必然漂移的 kill 逻辑。
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.CancelledError, TimeoutError):
        await terminate_process(proc)
        raise


def _level_of(tool_call: dict[Any, Any]) -> str:
    """本次调用的动作级别（解析失败按空参数，`tool_level` 会 fail-closed 归 write）。"""
    from app.agent.tool_policy import tool_level

    name = str(tool_call.get("name", "") or "")
    raw = tool_call.get("arguments")
    try:
        import json

        args = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        args = {}
    return tool_level(name, args if isinstance(args, dict) else {})


def cancelled_result(
    tool_name: str,
    *,
    level: str,
    stopped: bool,
    elapsed: float,
) -> dict[str, Any]:
    """取消后的工具结果：**有外部动作可能发生时是"未知"**，绝不伪装成功。

    `stopped=False` 表示宽限内没确认它停下 —— 这时连只读调用也不能报"什么都没发生"，
    因为"没停"本身就意味着它可能还在动手。
    """
    from app.agent.tool_policy import READ

    side_effects_possible = level != READ
    unknown = side_effects_possible or not stopped
    if unknown:
        note = (
            f"Tool '{tool_name}' was cancelled while it was already running"
            f"{'' if stopped else ' and did not stop within the convergence window'}. "
            "It may already have changed external state: **the result is unknown, not a "
            "success**. Do not report this step as done — re-check the target "
            "(file / job / remote resource) before doing anything else."
        )
        return {
            "cancelled": True,
            "outcome": "unknown",
            "result_unknown": True,
            "level": level,
            "attempted": True,
            "elapsed_seconds": round(elapsed, 3),
            "error": note,
            "note": note,
        }
    note = (
        f"Tool '{tool_name}' was cancelled before it finished; it was a read-only call, "
        "so no side effect was produced — but no result was collected either."
    )
    return {
        "cancelled": True,
        "outcome": "cancelled",
        "result_unknown": False,
        "level": level,
        "elapsed_seconds": round(elapsed, 3),
        "error": note,
        "note": note,
    }


async def run_tool_with_cancellation(
    runtime: Any,
    tool_call: dict[Any, Any],
    task: Any,
    *,
    dispatch: Callable[[], Coroutine[Any, Any, Any]] | None = None,
) -> tuple[Any, bool]:
    """跑一次工具调用，并让**当前这次调用**可被取消。

    返回 ``(result, cancelled)``。`cancelled=True` 时 result 一定是
    `cancelled_result()` 的形状（`cancelled` / `outcome` / `result_unknown`），
    调用方据此决定是否把这一轮当"已取消"收尾，而不是把结果当成功喂给模型。

    实现要点：

    * 工具本身跑在**独立 task** 里 —— 否则取消只能等到它自然返回（即"轮次边界"老问题）；
    * 观察者（`poll_cancel`）与工具赛跑，谁先到谁说了算；
    * 工具**恰好同时完成**（先完成、后取消）时按"已知结果"返回：它确实跑完了；
    * 超过 `terminate_grace_seconds()` 还没停：不再等，返回未知结果并留日志。
    """
    tool_name = str(tool_call.get("name", "") or "")
    session_id = str(getattr(task, "session_id", "") or "")
    run: Callable[[], Any] = (
        dispatch if dispatch is not None else (lambda: runtime._dispatch_tool(tool_call, task))
    )

    level = _level_of(tool_call)
    started = time.monotonic()
    work: asyncio.Task[Any] = asyncio.create_task(
        cast("Coroutine[Any, Any, Any]", run()), name=f"tool:{tool_name}"
    )
    watch: asyncio.Task[bool] = asyncio.create_task(
        poll_cancel(session_id), name=f"cancel-watch:{session_id or tool_name}"
    )
    try:
        done, _ = await asyncio.wait(
            {work, watch}, return_when=asyncio.FIRST_COMPLETED
        )
        if work in done:
            watch.cancel()
            return work.result(), False
        # 观察者先完成：True = 观测到取消；False = 没有可观测信号的会话 ⇒ 继续等工具。
        try:
            cancelled = bool(watch.result())
        except asyncio.CancelledError:  # pragma: no cover - 观察者被外部取消
            cancelled = False
        if not cancelled:
            return await work, False

        work.cancel()
        grace = terminate_grace_seconds()
        stopped = False
        try:
            await asyncio.wait({work}, timeout=grace)
            stopped = work.done() and work.cancelled()
        except asyncio.CancelledError:
            # 外层（run）自己也被取消：工具侧已经 cancel 过，照旧向上传播。
            raise
        elapsed = time.monotonic() - started
        if work.done() and not work.cancelled():
            # 取消到达的同时它已经跑完：结果**是已知的**，照实返回（不制造"未知"）。
            watch.cancel()
            exc = work.exception()
            if exc is None:
                return work.result(), False
            raise exc
        if not stopped:
            logger.warning(
                "cancel convergence exceeded for tool %s (session=%s): still running after %.2fs",
                tool_name,
                session_id,
                grace,
            )
        logger.info(
            "tool %s cancelled mid-flight (session=%s level=%s stopped=%s elapsed=%.2fs)",
            tool_name,
            session_id,
            level,
            stopped,
            elapsed,
        )
        return (
            cancelled_result(tool_name, level=level, stopped=stopped, elapsed=elapsed),
            True,
        )
    finally:
        for pending in (work, watch):
            if pending is not None and not pending.done():
                pending.cancel()
        # 未被取回的异常不该变成 "Task exception was never retrieved" 噪声。
        for done_task in (work, watch):
            if done_task is not None and done_task.done() and not done_task.cancelled():
                try:
                    done_task.exception()
                except Exception:  # noqa: BLE001
                    pass
