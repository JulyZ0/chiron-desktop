"""G13：队列任务的起停/完成/失败进**会话级事件**流水（``job.started`` / ``job.settled``）。

修前会怎样：任务的状态只在队列内部（PEL + idempotency）里，会话（PG）与前端（SSE）
各自靠约定再拼一份进度 —— 三份互相独立，谁也说不出"这个作业现在在哪一步"；
失败进 DLQ 时会话里连一条记录都没有，用户只看到"没有结果"。

这里钉住三件事：

1. 成功任务有 started + settled 两条，且 ``settled.status == completed``；
2. 失败任务的 ``settled.status == failed`` 且 ``error`` 非空（错误文本是用户能看到的唯一解释）；
3. 事件投递是**旁路**：Redis 抛错时任务本身仍然成功（与 ``app/agent/event_sink.py`` 同一原则）。

第 4 项是跨语言契约：Go relay 订阅的通道名必须等于引擎发布的那一个 ——
这种字符串一旦漂移，事件照发、没人收，而且不会有任何报错。
"""

from __future__ import annotations

import json
import pathlib
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.observability.metrics import QUEUE_JOB_EVENT_FAILED
from app.queue import idempotency, job_events
from app.queue.streams import ENGINE_QUEUE
from app.queue.worker import MAX_RETRIES, QueueWorker
from tests.source_scan import code_of

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


class FakeRedis:
    """假 Redis —— 同时充当"假 relay"：``publish`` 就是网关收到的那一条。

    刻意用真的 dict + ``json.loads`` 而不是 ``AsyncMock``：payload 形状要靠 JSON 往返
    验证（AsyncMock 只能证明"调用过"，证明不了"网关收到什么"）。
    心跳（G16）用到的 hash 命令也在这里实现，免得同一份替身各自长一半。
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.hashes: dict[str, dict[str, str]] = {}
        self.ttl_ms: dict[str, int] = {}
        self.fail_publish = False

    async def publish(self, channel: str, payload: str) -> int:
        if self.fail_publish:
            raise ConnectionError("redis pub/sub unavailable")
        self.published.append((channel, json.loads(payload)))
        return 1

    async def hset(self, key: str, mapping: Any = None, **kwargs: Any) -> int:
        data = {k: str(v) for k, v in (mapping or kwargs).items()}
        self.hashes.setdefault(key, {}).update(data)
        return len(data)

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def pexpire(self, key: str, ms: int) -> int:
        self.ttl_ms[key] = int(ms)
        return 1

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += 1 if self.hashes.pop(key, None) is not None else 0
            self.ttl_ms.pop(key, None)
        return removed

    async def scan_iter(self, match: str | None = None, count: int | None = None):  # noqa: ARG002
        prefix = (match or "").rstrip("*")
        for key in list(self.hashes):
            if key.startswith(prefix):
                yield key


@pytest.fixture(autouse=True)
def _mock_fenced_transitions(monkeypatch):
    """只替换"并发/租约"这一层；被测的是事件与心跳，不是 fencing 本身。"""
    claim = idempotency.Attempt("engine", "tool_job:t1", "state", "token", 1)
    monkeypatch.setattr(
        idempotency,
        "claim",
        AsyncMock(return_value=idempotency.ClaimDecision(idempotency.ClaimStatus.ACQUIRED, claim)),
    )
    monkeypatch.setattr(idempotency, "complete", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "ack_completed", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "retry", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "defer", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "dead_letter", AsyncMock(return_value=True))
    monkeypatch.setattr(idempotency, "forward", AsyncMock(return_value=True))


def make_worker(redis: FakeRedis) -> QueueWorker:
    # 心跳间隔压到毫米级：本文件不测心跳节奏，但不该让后台循环等在 30s 的 sleep 上。
    return QueueWorker(redis=redis, queue=ENGINE_QUEUE, job_heartbeat_interval=0.01)


def fields(
    task_type: str = "tool_job",
    task_id: str = "t1",
    payload: str = "{}",
    retry_count: int = 0,
) -> dict[bytes, bytes]:
    return {
        b"task_type": task_type.encode(),
        b"task_id": task_id.encode(),
        b"payload": payload.encode(),
        b"retry_count": str(retry_count).encode(),
    }


def events(redis: FakeRedis) -> list[dict[str, Any]]:
    return [payload for _channel, payload in redis.published]


def settled_event(redis: FakeRedis) -> dict[str, Any]:
    settled = [e for e in events(redis) if e["type"] == job_events.EV_JOB_SETTLED]
    assert settled, "没有 job.settled 事件"
    return settled[-1]


class TestLifecycleEvents:
    async def test_successful_task_emits_started_then_completed(self) -> None:
        """修前：成功任务在会话流水里一片空白（只有队列内部的 status=completed）。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()

        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}')
        )

        assert [e["type"] for e in events(redis)] == [
            job_events.EV_JOB_STARTED,
            job_events.EV_JOB_SETTLED,
        ]
        started, settled = events(redis)
        assert started["status"] == job_events.ST_RUNNING
        assert settled["status"] == job_events.ST_COMPLETED
        assert settled["job_id"] == "j1"
        assert settled["task_type"] == "tool_job"
        assert settled["session_id"] == "s1"
        assert settled["error"] == ""
        worker._dispatch.assert_awaited_once()
        idempotency.complete.assert_awaited_once()

    async def test_failed_task_settles_with_status_and_error(self) -> None:
        """修前：失败只剩一行日志，会话/前端拿不到"为什么没结果"。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock(side_effect=RuntimeError("boom"))

        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}', retry_count=0)
        )

        settled = settled_event(redis)
        assert settled["status"] == job_events.ST_FAILED
        assert "boom" in settled["error"]
        assert settled["job_id"] == "j1"
        idempotency.retry.assert_awaited_once()

    async def test_discarded_task_settles_without_started(self) -> None:
        """没跑就被丢弃（重领次数耗尽）→ 只有 settled(status=discarded)。

        修前这种任务彻底静默：不进消息、不进会话，只是"再也没出现"。
        这里同时断言**不发 started** —— 从没跑过的任务不该先闪一张"运行中"的卡片。
        """
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()

        await worker._process_message(
            "1-0",
            fields(payload='{"job_id": "j1", "session_id": "s1"}', retry_count=MAX_RETRIES + 1),
        )

        got = events(redis)
        assert [e["type"] for e in got] == [job_events.EV_JOB_SETTLED]
        assert got[0]["status"] == job_events.ST_DISCARDED
        assert got[0]["error"]
        worker._dispatch.assert_not_awaited()

    async def test_deferred_attempt_settles_as_deferred(self) -> None:
        """"等一会儿再来"不是失败：本次尝试以 deferred 收口，重投后会再发一次 started。

        修前这里既不发事件也不区分语义 —— 前端只能把"没结束"当成"还在跑"。
        """
        from app.queue.worker import RetryLaterError

        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock(side_effect=RetryLaterError("session busy"))

        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}')
        )

        settled = settled_event(redis)
        assert settled["status"] == job_events.ST_DEFERRED
        assert "session busy" in settled["error"]
        idempotency.defer.assert_awaited_once()
        idempotency.retry.assert_not_awaited()


class TestBypass:
    async def test_delivery_error_does_not_fail_the_task(self) -> None:
        """修前没有事件层；引入它之后最贵的错误是"投递故障把任务搞失败"。

        Redis pub/sub 抛错时：任务照常完成，失败计入指标（否则故障只表现为"进度不动"）。
        """
        redis = FakeRedis()
        redis.fail_publish = True
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()
        before = QUEUE_JOB_EVENT_FAILED.labels(event=job_events.EV_JOB_STARTED)._value.get()

        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}')
        )

        worker._dispatch.assert_awaited_once()
        idempotency.complete.assert_awaited_once()
        idempotency.dead_letter.assert_not_awaited()
        assert not redis.published
        after = QUEUE_JOB_EVENT_FAILED.labels(event=job_events.EV_JOB_STARTED)._value.get()
        assert after == before + 1, "投递失败必须计数（旁路静默 = 故障不可见）"

    async def test_raise_inside_emit_is_also_absorbed_by_the_call_site(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """调用点再兜一层：emit 自身抛错（签名变化、内部前置动作失败）也不得外溢。"""

        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("event sink exploded")

        monkeypatch.setattr(job_events, "emit_job_event", boom)
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()

        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}')
        )

        worker._dispatch.assert_awaited_once()
        idempotency.complete.assert_awaited_once()


class TestPayloadContract:
    async def test_payload_carries_the_documented_fields(self) -> None:
        """字段是线上契约（Go relay + 前端都按名字读），因此逐个钉住。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()

        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}')
        )

        required = {"type", "job_id", "task_type", "status", "session_id", "error", "ts"}
        for payload in events(redis):
            assert required <= set(payload), payload
            assert isinstance(payload["ts"], (int, float))

    async def test_payload_is_sent_on_the_relay_channel(self) -> None:
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()
        await worker._process_message(
            "1-0", fields(payload='{"job_id": "j1", "session_id": "s1"}')
        )
        channels = {channel for channel, _ in redis.published}
        assert channels == {job_events.job_events_channel()}

    async def test_job_id_falls_back_to_task_id_and_session_may_be_empty(self) -> None:
        """没有 job_id 的任务（memory_save 之类）用 task_id 定位；会话为空照样发。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()
        await worker._process_message("1-0", fields(payload="{}"))
        settled = settled_event(redis)
        assert settled["job_id"] == "t1"
        assert settled["session_id"] == ""

    async def test_broken_payload_still_yields_an_event(self) -> None:
        """载荷不是 JSON 时任务会失败 —— 而这正是最需要事件的时候。"""
        redis = FakeRedis()
        worker = make_worker(redis)
        worker._dispatch = AsyncMock()
        await worker._process_message("1-0", fields(payload="not-json", task_id="t9"))
        settled = settled_event(redis)
        assert settled["status"] == job_events.ST_FAILED
        assert settled["job_id"] == "t9"
        assert settled["error"]


class TestCrossLanguageContract:
    async def test_go_relay_subscribes_to_the_channel_we_publish_to(self) -> None:
        """Go relay 的订阅名必须等于引擎的发布名（剥注释后再匹配，避免注释满足断言）。"""
        assert job_events.job_events_channel().endswith(job_events.JOB_EVENTS_CHANNEL_NAME)
        go_src = code_of(_REPO_ROOT / "internal" / "api" / "job_events_relay_g13.go")
        assert f'db.RedisKey("{job_events.JOB_EVENTS_CHANNEL_NAME}")' in go_src, (
            "Go relay 订阅的通道名与 python-engine/app/queue/job_events.py 不一致"
            "—— 事件照发、没人收，且不会报错"
        )
        assert "RelayEvent" in go_src, "relay 必须走 RelayEvent（Publish 会造成跨实例 N 份重复）"

    async def test_relay_drops_events_without_session(self) -> None:
        """无会话归属的事件无法路由（SSE 按 session 过滤）：relay 必须丢弃而不是广播。

        这条断言的是 Go 源码里的判据存在 —— 它是"串扰"与"可见"之间唯一的守卫。
        """
        go_src = code_of(_REPO_ROOT / "internal" / "api" / "job_events_relay_g13.go")
        assert "session_id" in go_src
        assert "decodeJobEvent" in go_src
