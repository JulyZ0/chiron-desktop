"""Trace Writer — 将 Agent/Workflow 执行链路写入 Redis Stream (无状态扩展).

职责:
1. 接收 AgentEvent(type="trace_span"),提取 trace_id/span/duration
2. 写入 Redis Stream `chiron:traces`
3. Go 侧可订阅该 stream,聚合完整调用链

多实例部署:
- 所有 Python 实例共享同一 Redis Stream
- Go Gateway 统一读取 stream,不依赖进程内存
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.middleware.privacy_middleware import is_no_retention
from app.redis_keys import rkey

logger = logging.getLogger(__name__)

# Redis Stream 键名 (SaaS: 按 tenant_id 隔离;统一键前缀与 Go trace_handler RedisKey 对齐)
TRACES_STREAM_TPL = rkey("chiron:traces:{}")  # {} 将被 tenant_id 替换


def get_tenant_stream(tenant_id: int) -> str:
    """根据 tenant_id 生成隔离的 Redis Stream 键名."""
    if not tenant_id:
        return TRACES_STREAM_TPL.format("anonymous")  # 匿名租户默认 key
    return TRACES_STREAM_TPL.format(tenant_id)


def _span_user_id(metadata: dict[str, Any] | None) -> str:
    """span 的发起用户（H27）。

    取值顺序：显式 metadata（调用方最清楚自己是谁）→ 运行时上下文。两者都没有时返回
    空串 —— **空不代表"人人可见"**：网关侧对无法归属的 span 只放行 `admin:read`
    （fail-closed），宁可在升级期让老记录只对管理员可见，也不让同租户的普通用户
    凭 trace_id 读到别人的调用链。
    """
    if isinstance(metadata, dict):
        raw = metadata.get("user_id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    try:
        from app.tools.context import get_user_id

        return str(get_user_id() or "").strip()
    except Exception:  # noqa: BLE001 - 归属信息取不到不影响写 span 本身
        return ""


class TraceWriter:
    """异步 trace writer (单例模式,避免重复连接 Redis)."""

    _instance: TraceWriter | None = None
    _redis: Any | None = None

    @classmethod
    async def get_instance(cls, redis_url: str | None = None) -> TraceWriter:
        """获取全局唯一 TraceWriter 实例."""
        if cls._instance is None:
            cls._instance = cls()
            # 使用统一的 Redis 客户端
            from app.redis_client import get_redis

            try:
                cls._redis = await get_redis()
                await cls._redis.ping()
                logger.info("TraceWriter connected to Redis via unified client")
            except Exception as e:
                logger.warning(
                    "TraceWriter Redis ping failed: %s (traces will be lost)", e
                )
                cls._redis = None
        return cls._instance

    @classmethod
    async def close(cls) -> None:
        """关闭 Redis 连接."""
        if cls._redis:
            await cls._redis.close()
            cls._redis = None
            cls._instance = None

    async def write_span(
        self,
        trace_id: str,
        span_name: str,
        duration_ms: int,
        metadata: dict[str, Any] | None = None,
        tenant_id: int | None = None,  # SaaS: 租户隔离
        span_id: str = "",
        parent_span_id: str = "",
        kind: str = "",
    ) -> None:
        """写入单个 span 事件到 Redis Stream (按租户隔离).

        Args:
            trace_id: 用户请求的唯一标识
            span_name: span 名称 (llm_call / tool_execution / workflow_node)
            duration_ms: span 耗时 (毫秒)
            metadata: 额外上下文 (model, input_tokens, tool_name 等)
            tenant_id: 租户 ID (用于流隔离,可选)
            span_id: 该 span 的稳定标识（差距C：执行图靠它建立节点）
            parent_span_id: 父 span 标识 —— **这一条就是"某步从哪来"的答案**；
                例如 `trace:turn:2` 的父是 `trace:run`，一次工具调用的父是它所在的 turn。
            kind: span 类别（run / turn / llm / tool / workflow），供前端按类别着色与折叠。
        """
        # 隐私模式: no_retention 时跳过 trace 写入 (不落盘)
        if is_no_retention():
            logger.debug("Privacy no_retention: skip trace span write: %s", span_name)
            return

        if self._redis is None:
            logger.debug(
                "TraceWriter disabled (Redis not available), skipping span: %s",
                span_name,
            )
            return

        try:
            # SaaS 安全: 按 tenant_id 分 stream
            stream = get_tenant_stream(tenant_id or 0)
            entry = {
                "trace_id": trace_id,
                "span_name": span_name,
                "duration_ms": str(duration_ms),
                "timestamp": str(time.time()),
                "tenant_id": tenant_id or "",
                # H27：span 归属的发起用户。网关按 tenant 分 stream 之后，同租户内
                # 所有用户的调用链混在一条流里，只按 tenant 过滤等于"猜到 trace_id
                # 就能读到别人的完整链路"。这里显式落 `user_id`，Go 侧据此收窄。
                "user_id": _span_user_id(metadata),
                "metadata": json.dumps(metadata or {}, ensure_ascii=False),
                # 差距C：执行图的三要素（稳定的 id、父指针、类别）。
                "span_id": span_id or "",
                "parent_span_id": parent_span_id or "",
                "kind": kind or "",
            }
            await self._redis.xadd(stream, entry, maxlen=10000, approximate=True)
        except Exception as e:
            logger.warning(
                "TraceWriter failed to write span (tenant=%s): %s", tenant_id, e
            )

    async def write_batch(self, spans: list[dict[str, Any]]) -> None:
        """批量写入多个 span (减少 Redis 往返).

        Args:
            spans: 每个 dict 包含 trace_id, span_name, duration_ms, metadata
        """
        if not spans:
            return

        # 隐私模式: no_retention 时跳过 trace 写入 (不落盘)
        if is_no_retention():
            logger.debug(
                "Privacy no_retention: skip trace batch write (%d spans)", len(spans)
            )
            return

        if self._redis is None:
            return

        try:
            entries = []
            for s in spans:
                entry = {
                    "trace_id": s["trace_id"],
                    "span_name": s["span_name"],
                    "duration_ms": str(s["duration_ms"]),
                    "timestamp": str(time.time()),
                    # H27：批量路径同样要写归属，否则经批量落盘的 span 在网关侧
                    # 全部退化成"无法归属"（只有管理员可见）。
                    "user_id": _span_user_id(s.get("metadata")),
                    "metadata": json.dumps(s.get("metadata", {}), ensure_ascii=False),
                    # 差距C：与 write_span 保持同一套字段（批量路径不能少写父指针，
                    # 否则经批量写入的 span 在执行图里会变成孤点）。
                    "span_id": str(s.get("span_id", "") or ""),
                    "parent_span_id": str(s.get("parent_span_id", "") or ""),
                    "kind": str(s.get("kind", "") or ""),
                }
                entries.append(entry)

            pipeline = self._redis.pipeline(transaction=False)
            for span, entry in zip(spans, entries, strict=False):
                # SaaS 安全: 按 tenant_id 分 stream（与 write_span 一致）
                # span 的 tenant_id 已是整数域（write_span 形参 tenant_id: int | None）。
                # 这里再经 to_tenant_int 收口：缺失/无法解析 → 0，get_tenant_stream(0)
                # 本就落 "anonymous" stream，与原 `or "anonymous"` 语义一致。
                # 函数内导入，避免 app.trace 与 app.tools 之间形成循环导入。
                from app.tools.context import to_tenant_int

                stream = get_tenant_stream(to_tenant_int(span.get("tenant_id")))
                pipeline.xadd(stream, entry, maxlen=10000, approximate=True)
            await pipeline.execute()

            logger.debug("TraceWriter wrote %d spans to Redis", len(entries))
        except Exception as e:
            logger.warning("TraceWriter batch write failed: %s", e)


# ── 便捷函数 (供 AgentRuntime 直接调用) ─────────────────────────────────────

_trace_writer: TraceWriter | None = None
_write_lock = asyncio.Lock()


async def record_span(
    trace_id: str,
    span_name: str,
    duration_ms: int,
    metadata: dict[str, Any] | None = None,
    tenant_id: int | None = None,  # SaaS: 租户隔离
    redis_url: str | None = None,
    span_id: str = "",
    parent_span_id: str = "",
    kind: str = "",
) -> None:
    """便捷函数: 记录单个 span 到 Redis Stream (带租户隔离).

    自动初始化 TraceWriter (单例),确保首次调用时建立 Redis 连接.

    ``span_id`` / ``parent_span_id`` / ``kind`` 是**执行图（差距C）**的三要素：有了父指针，
    "某一步是从哪来的"才可查询（此前只有扁平的时间序列）。

    Example:
        await record_span(
            trace_id="abc123",
            span_name="llm_call",
            duration_ms=1500,
            metadata={"model": "gpt-4", "input_tokens": 500},
            tenant_id="tenant_456",
            span_id="abc123:turn:1:llm",
            parent_span_id="abc123:turn:1",
            kind="llm",
        )
    """
    global _trace_writer

    if _trace_writer is None:
        async with _write_lock:
            if _trace_writer is None:
                _trace_writer = await TraceWriter.get_instance(redis_url)

    await _trace_writer.write_span(
        trace_id,
        span_name,
        duration_ms,
        metadata,
        tenant_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        kind=kind,
    )
