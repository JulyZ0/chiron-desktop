"""S3 存储后端实现。

使用 boto3 将媒体资产存储到 S3 兼容服务（AWS S3、MinIO 等）。
元数据存储在 S3 对象的 metadata 中，无需额外数据库。

**H31：锁与 IO 的边界。** 索引落盘（``metadata.json``）需要在两个约束下完成：

1. 同一时刻只有一个线程把「快照 → put_object」走完，否则并发写入的快照可能**乱序**
   落盘（A 的快照晚于 B 的快照落地 ⇒ 索引里永久缺少 B 的资产）。
2. 任何网络 IO 都不允许发生在内存索引锁里 —— 锁里做 IO 会把一次慢请求放大成全局
   阻塞；而"在持锁函数里再取同一把锁"（旧实现 ``write()`` → ``_save()``）在
   ``threading.Lock`` 上是**直接自死锁**：S3 后端写入永久挂起，调用方看不到错误。

所以这里拆成两级锁，且都**不可嵌套自身**：

- ``_lock``（``threading.Lock``）：只保护 ``self._assets`` 的内存读写与快照序列化，
  持锁时间 = 一次内存遍历 + JSON 编码。
- ``_write_lock``（``threading.Lock``）：串行化"快照 + 落盘"。顺序固定为
  ``_write_lock`` → ``_lock``，反向从不发生，因此没有死锁环。

失败语义：落盘失败时内存索引回滚到写入前状态并抛 :class:`S3StoreError`（明确错误），
绝不挂起、也不留下"内存里有、S3 里没有"的幻影资产。

``client`` 参数允许注入已构造的 S3 客户端（测试与自定义凭据链），传 None 时才按
原有方式用 boto3 建客户端。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, cast

from app.media.store import Asset, BaseStore

logger = logging.getLogger(__name__)


class S3StoreError(RuntimeError):
    """S3 后端无法完成一次明确操作（索引读取之外的失败都以此类型交给调用方）。"""


def _client_error_type() -> type[BaseException] | None:
    """boto3 是可选依赖：没有它时返回 None（只剩 KeyError 判据）。"""
    try:
        from botocore.exceptions import ClientError
    except Exception:  # noqa: BLE001 - 未装 boto3 的部署也应能 import 本模块
        return None
    return ClientError


def _is_missing_object_error(exc: BaseException) -> bool:
    """异常是否等价于"对象不存在 / 无权限"（对调用方是同一件事：取不回）。"""
    client_error = _client_error_type()
    if client_error is not None and isinstance(exc, client_error):
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        return code in {"NoSuchKey", "NoSuchBucket", "AccessDenied", "403", "404"}
    return isinstance(exc, KeyError)


class S3Store(BaseStore):
    """S3 存储后端。"""

    def __init__(
        self,
        bucket: str,
        prefix: str = "media/",
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        # boto3 是 pyproject 里的可选 extra `[s3]`，不在必需依赖闭包内：放在模块顶层会让
        # "整个 app 包能被 import"这个判据（桌面端的运行时导入探测、以及任何只装必需依赖的
        # 部署）反过来要求 boto3。与 app/knowledge/blob.py 的 S3BlobStore 同一口径。
        # ``client`` 已注入时（测试 / 自定义凭据链）连 import 都不需要。
        if client is None:
            import boto3
            from botocore.config import Config as BotoConfig

            # access_key/secret_key 为 None 时走 boto3 默认凭据链（env / IAM role）；
            # 后台「存储配置」下发的凭据则显式传入，让引擎随后台配置写同一个桶。
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=BotoConfig(
                    retries={"max_attempts": 3, "mode": "standard"},
                    signature_version="s3v4",
                ),
            )

        self._bucket = bucket
        self._prefix = prefix.rstrip("/") + "/"
        self._s3 = client
        self._index_key = f"{self._prefix}metadata.json"
        self._assets: dict[str, Asset] = {}
        # 内存索引锁：绝不持有它做网络 IO，也绝不在持锁状态下再次获取它。
        self._lock = threading.Lock()
        # 索引落盘串行化：保证"较新的快照不会先于较旧的快照落地"。
        self._write_lock = threading.Lock()
        self._load()

    # ── 索引读写 ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=self._index_key)
            data = json.loads(resp["Body"].read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 空桶/无权限/损坏都不阻断启动
            logger.warning(
                "Failed to load S3 metadata from %s/%s, starting with empty store: %s",
                self._bucket,
                self._index_key,
                type(exc).__name__,
            )
            return
        loaded: dict[str, Asset] = {}
        try:
            for item in data:
                asset = Asset(**item)
                loaded[asset.id] = asset
        except Exception as exc:  # noqa: BLE001 - 结构不合法等同"没有可用索引"
            logger.warning(
                "S3 metadata at %s/%s is malformed (%s), starting with empty store",
                self._bucket,
                self._index_key,
                type(exc).__name__,
            )
            return
        with self._lock:
            self._assets.update(loaded)

    def _snapshot_locked(self) -> bytes:
        """序列化当前索引。**调用方必须已持有** ``_lock``。"""
        data = [
            {
                "id": a.id,
                "name": a.name,
                "file_url": a.file_url,
                "type": a.type,
                "category": a.category,
                "tags": a.tags,
                "size": a.size,
                "format": a.format,
                "width": a.width,
                "height": a.height,
                "created_at": a.created_at,
            }
            for a in self._assets.values()
        ]
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    def _put_index(self, payload: bytes) -> None:
        """落盘索引。**调用方不得持有** ``_lock``（这里会阻塞在网络 IO 上）。"""
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=self._index_key,
                Body=payload,
                ContentType="application/json",
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成明确的 S3StoreError
            raise S3StoreError(
                f"failed to persist S3 asset index {self._index_key}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _save(self) -> None:
        """把当前内存索引落盘：一次 ``_write_lock`` + 一次快照 + 一次 IO。"""
        with self._write_lock:
            with self._lock:
                payload = self._snapshot_locked()
            self._put_index(payload)

    # ── BaseStore 实现 ────────────────────────────────────────────────────

    def write(
        self,
        name: str,
        content: bytes,
        asset_type: str = "text",
        category: str = "generated",
        tags: list[str] | None = None,
        fmt: str = "",
        width: int = 0,
        height: int = 0,
    ) -> Asset:
        asset_id = uuid.uuid4().hex[:12]
        prefix = asset_id[:8]
        safe_name = name.replace("/", "_").replace("\\", "_")
        key = f"{self._prefix}generated/{prefix}_{safe_name}"

        content_type = fmt if fmt else "application/octet-stream"
        extra_args: dict[str, Any] = {"ContentType": content_type}

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                **extra_args,
            )
        except Exception as exc:  # noqa: BLE001
            raise S3StoreError(
                f"failed to upload object {key}: {type(exc).__name__}: {exc}"
            ) from exc

        # 构造 S3 URL（兼容 AWS 和自定义端点）
        if endpoint_url := self._s3.meta.endpoint_url:
            file_url = f"{endpoint_url}/{self._bucket}/{key}"
        else:
            file_url = f"https://{self._bucket}.s3.amazonaws.com/{key}"

        asset = Asset(
            id=asset_id,
            name=name,
            file_url=file_url,
            type=asset_type,
            category=category,
            tags=tags or [],
            size=len(content),
            format=fmt,
            width=width,
            height=height,
        )
        with self._lock:
            self._assets[asset_id] = asset
        try:
            # 出内存锁之后再拿落盘锁：既不自死锁，也不把网络 IO 拖进索引锁。
            self._save()
        except BaseException:
            # 回滚：不能让"内存里有、S3 里没有"的资产逃出去被后续 list/get 当真。
            with self._lock:
                self._assets.pop(asset_id, None)
            logger.error("S3 asset index update failed; rolled back asset %s", asset_id)
            raise
        return asset

    def get(self, asset_id: str) -> Asset | None:
        with self._lock:
            return self._assets.get(asset_id)

    def list(self) -> list[Asset]:
        with self._lock:
            return list(self._assets.values())

    def write_at(self, key: str, content: bytes) -> None:
        # 归档与媒体资产共用桶但走独立前缀；`key` 由服务端拼（`archives/<ref>.txt`），
        # 不接受客户端输入。这里额外归一 `..` —— 即便上游漏了校验，也不会写到前缀之外。
        safe = key.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError(f"key escapes storage prefix: {key!r}")
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=f"{self._prefix}{safe}",
                Body=content,
                ContentType="text/plain; charset=utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            raise S3StoreError(
                f"failed to upload object {safe}: {type(exc).__name__}: {exc}"
            ) from exc

    def read_at(self, key: str) -> bytes | None:
        safe = key.replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            raise ValueError(f"key escapes storage prefix: {key!r}")
        try:
            resp = self._s3.get_object(Bucket=self._bucket, Key=f"{self._prefix}{safe}")
        except Exception as exc:  # noqa: BLE001
            if not _is_missing_object_error(exc):
                # 配置/网络类故障是"取不回"之外的另一种事：交给调用方当明确错误处理，
                # 而不是伪装成"归档不存在"。
                raise S3StoreError(
                    f"failed to read object {safe}: {type(exc).__name__}: {exc}"
                ) from exc
            logger.warning("s3 archive read failed (key=%s): %s", safe, exc)
            return None
        return cast(bytes, resp["Body"].read())
