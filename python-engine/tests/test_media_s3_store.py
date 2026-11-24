# ruff: noqa: N803 - 假 S3 客户端的形参必须与 boto3 的关键字参数同名
"""H31：S3 后端保存路径的锁/IO 边界与故障语义（假客户端，不连真 S3）。
覆盖 PRD 9.7 H31 验收：

- 保存路径单次持锁或使用可重入锁且无嵌套 IO（旧实现 ``write()`` 持 ``_lock`` 调用
  ``_save()`` → 同一把非可重入锁二次获取 ⇒ 永久挂起）；
- 并发写入把索引写坏（较旧快照晚于较新快照落地）不会丢资产；
- 故障时返回明确错误（``S3StoreError``）而不是挂起，且内存索引回滚；
- 重启恢复：新实例能从已落盘的 ``metadata.json`` 读回资产。
"""

from __future__ import annotations

import json
import threading
import time
import types
from typing import Any

import pytest

from app.media.s3_store import S3Store, S3StoreError

_BUCKET = "chiron-media"
_INDEX = "media/metadata.json"


class _FakeBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class _FakeMeta:
    endpoint_url = "http://minio.local:9000"


class FakeS3:
    """最小 S3 客户端替身：只实现存储后端用到的那几个方法。"""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_keys: list[str] = []
        self.fail_put: BaseException | None = None
        #: 只让索引写入失败，用来单独验证保存/回滚路径（对象本体上传成功）。
        self.fail_index_put: BaseException | None = None
        #: 非 None 时 ``put_object`` 会阻塞在这里，用来观察"IO 期间锁是否被占用"。
        self.gate: threading.Event | None = None
        self.put_entered = threading.Event()
        self.meta = _FakeMeta()
        self.exceptions = types.SimpleNamespace(NoSuchKey=KeyError)
        self._guard = threading.Lock()

    def put_object(  # noqa: N803 - 必须与 boto3 的关键字参数同名
        self, Bucket: str, Key: str, Body: bytes, **_kwargs: Any
    ) -> None:
        self.put_keys.append(Key)
        self.put_entered.set()
        if self.gate is not None:
            assert self.gate.wait(5.0), "fake gate never released"
        if self.fail_put is not None:
            raise self.fail_put
        if self.fail_index_put is not None and Key == _INDEX:
            raise self.fail_index_put
        with self._guard:
            self.objects[(Bucket, Key)] = Body
        # 模拟真实网络的调度窗口，放大并发写入的乱序风险。
        time.sleep(0.001)

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        with self._guard:
            if (Bucket, Key) not in self.objects:
                raise KeyError(Key)
            return {"Body": _FakeBody(self.objects[(Bucket, Key)])}

    def index_ids(self) -> list[str]:
        raw = self.objects[(self._bucket_owner(), _INDEX)]
        return [item["id"] for item in json.loads(raw.decode("utf-8"))]

    def _bucket_owner(self) -> str:
        return _BUCKET


def make_store(client: FakeS3 | None = None) -> tuple[S3Store, FakeS3]:
    fake = client if client is not None else FakeS3()
    return S3Store(bucket=_BUCKET, client=fake), fake


def test_write_returns_instead_of_deadlocking():
    """旧实现：write() 持 _lock 再调 _save() → 非可重入锁自死锁（永久挂起）。"""
    store, fake = make_store()
    result: dict[str, Any] = {}

    def _write() -> None:
        result["asset"] = store.write("hello.txt", b"hi", fmt="text/plain")

    worker = threading.Thread(target=_write, daemon=True)
    worker.start()
    worker.join(timeout=5.0)

    assert not worker.is_alive(), "S3 write() 死锁：保存路径嵌套获取了同一把锁"
    asset = result["asset"]
    assert asset.id
    assert store.get(asset.id) is asset
    assert fake.index_ids() == [asset.id]


def test_index_io_does_not_hold_the_asset_lock():
    """落盘期间内存索引锁必须已释放，否则一次慢 IO 会阻塞所有 get/list。"""
    store, fake = make_store()
    first = store.write("first.txt", b"1")
    fake.gate = threading.Event()
    fake.put_entered.clear()

    writer = threading.Thread(target=lambda: store.write("second.txt", b"2"), daemon=True)
    writer.start()
    try:
        assert fake.put_entered.wait(5.0), "第二次写入没有进入 put_object"

        started = time.monotonic()
        assert store.get(first.id) is first
        assert len(store.list()) >= 1
        assert time.monotonic() - started < 1.0, "索引 IO 持锁导致 get/list 被阻塞"
    finally:
        fake.gate.set()
        writer.join(timeout=5.0)
    assert not writer.is_alive()


def test_concurrent_writes_do_not_lose_index_entries():
    store, fake = make_store()
    written: list[str] = []
    guard = threading.Lock()

    def _write(index: int) -> None:
        asset = store.write(f"f{index}.txt", f"{index}".encode())
        with guard:
            written.append(asset.id)

    threads = [threading.Thread(target=_write, args=(i,)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    assert all(not thread.is_alive() for thread in threads)

    assert sorted(fake.index_ids()) == sorted(written)
    assert sorted(asset.id for asset in store.list()) == sorted(written)


def test_index_failure_raises_clear_error_and_rolls_back():
    store, fake = make_store()
    fake.fail_index_put = OSError("network is down")

    with pytest.raises(S3StoreError) as excinfo:
        store.write("boom.txt", b"x")
    assert "network is down" in str(excinfo.value)
    # 内存索引不能留下"有记录但 S3 索引里没有"的幻影资产
    assert store.list() == []
    assert (_BUCKET, _INDEX) not in fake.objects
    # 对象本体已上传（能力所限无法回滚），但索引没落盘 ⇒ 重启后不会出现幻影
    restarted, _ = make_store(fake)
    assert restarted.list() == []


def test_object_upload_failure_is_explicit():
    store, fake = make_store()
    fake.fail_put = RuntimeError("403 Forbidden")

    with pytest.raises(S3StoreError):
        store.write("denied.txt", b"x")
    assert store.list() == []


def test_restart_recovers_persisted_index():
    store, fake = make_store()
    first = store.write("a.txt", b"a")
    second = store.write("b.txt", b"b")

    restarted, _ = make_store(fake)
    assert sorted(asset.id for asset in restarted.list()) == sorted([first.id, second.id])
    assert restarted.get(second.id) is not None


def test_corrupt_index_starts_empty_instead_of_crashing():
    fake = FakeS3()
    fake.objects[(_BUCKET, _INDEX)] = b'{"not": "a list"}'
    store, _ = make_store(fake)
    assert store.list() == []


def test_write_at_and_read_at_roundtrip_rejects_escape():
    store, fake = make_store()
    store.write_at("archives/ref.txt", b"payload")
    assert store.read_at("archives/ref.txt") == b"payload"
    assert store.read_at("archives/missing.txt") is None

    with pytest.raises(ValueError):
        store.write_at("../escape.txt", b"x")
    with pytest.raises(ValueError):
        store.read_at("a/../../escape.txt")
    assert fake.put_keys == ["media/archives/ref.txt"]


def test_read_failure_that_is_not_missing_raises_clear_error():
    store, fake = make_store()
    fake.get_object = types.MethodType(  # type: ignore[method-assign]
        lambda self, **_kwargs: (_ for _ in ()).throw(OSError("connection reset")), fake
    )
    with pytest.raises(S3StoreError):
        store.read_at("archives/ref.txt")
