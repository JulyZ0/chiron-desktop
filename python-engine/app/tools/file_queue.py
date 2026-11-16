"""按 realpath 的**执行期**串行队列（G-T3）。

## 为什么需要它

`write_claim`（差距 P4）解决的是**跨 Agent 抢占**：粒度是**租约**（TTL + owner），
它回答"这个文件现在是不是别人在写"。而两个写者**同时**进来时，租约只能让后到者
**被拒**，不能让它们**排队** —— 后到者拿到一条 `error`，模型只好自己重试，
重试时又是同一个竞态。粒度是"谁占用"，不是"谁先写"。

`_split_tool_batch`（C3）解决的是**批内**问题：同一批里路径重复就整批降级串行。
那是**事前静态判定** —— 它看不见批外的并发（主 Agent 与子 Agent 同时写、`run_code`
里的嵌套调用与主循环同时写、API 直连与对话同时写）。

pi 的 `withFileMutationQueue` 是**执行期**串行化：同一个 realpath 的写请求排队，
无论来自哪个批、哪个入口。本模块落实同一件事。

## 为什么落在 `registry.execute`

`tool.handler(...)` 在生产路径上**只有一个调用点**（`ToolRegistry.execute`）。所有写
入口都汇聚到那里：主循环的 `_dispatch_tool`、`run_code` 的嵌套调用
（`_safe_tool_call` → `registry.execute`）、`use_capability` 代理、`multi_agent`、
以及 `api/tools.py` 的直连。放在 handler 外层这一处，就等于覆盖了全部入口；
放在 `runtime` 里则只能覆盖主循环那一条。

## 与 `write_claim` 互补，不替代

* **key 集合完全相同**（`write_claim.FILE_WRITE_TOOLS`），两者对"哪些工具算写"
  必须同口径，否则"租约挡住了 A 却没挡住 B"这类洞会从这里长出来；
* **租约挡跨进程/跨副本**（虽然它本身也只是进程内，见该模块的说明），
  队列挡**同进程内的同时进入**；
* 两者都在，语义才是完整的："有人正在写 ⇒ 排队等它写完"（队列），而不是
  "有人正在写 ⇒ 你失败"（只有租约）。

## 边界（不假装覆盖的东西)

* **`replace_in_files` 不在此列**：它的寻址单位是"一棵目录下的 N 个文件"，
  没有单一路径可作 key。用它的 `root` 当 key 会与具体文件的 key 不重合 ——
  等于给自己一个**错误的安全感**。它默认 `dry_run=True` 只预览，真正落盘时
  是批内串行 + 逐文件 undo 快照兜底。这条边界写在这里，是因为"看着覆盖了
  其实没有"比"明确不覆盖"更危险。
* **只进程内**：多副本共享卷时看不到对方。与 `write_claim` 同一个取舍 ——
  那需要 Redis 往返，代价与复杂度都不值当，且正确归属是外部编排。
* **不跨 loop**：`asyncio.Lock` 绑定它被使用的 loop。条目在最后一个使用者
  离开时就删除，所以进程内多 loop（测试、多 worker 线程）不会串味。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

#: 参与串行化的工具。**必须与 `write_claim.FILE_WRITE_TOOLS` 同口径** ——
#: 两者一个管"同时进入"、一个管"谁占用"，覆盖的集合不一致就会出现
#: "被队列排队的那个没被租约保护"（或反过来）的洞。
FILE_MUTATION_TOOLS: frozenset[str] = frozenset({"write_file", "edit_file", "delete_file"})

#: 参数里可能出现被写路径的键（与 `write_claim.path_of` 保持同序同集合）。
_PATH_KEYS = ("path", "file_path", "filepath", "filename", "file", "target")


class _Entry:
    """一个 realpath 的队列条目。`users` 是引用计数，用于"最后一个离开时删条目"。"""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


_entries: dict[str, _Entry] = {}


def mutation_key(tool_name: str, params: dict[str, Any] | None) -> str:
    """该调用要串行化的 realpath；不参与串行化时返回空串。

    key 必须落到 **realpath**（`safe_join` 内部已经 `resolve()`），因为同一个文件
    有太多写法：`a.txt` / `./a.txt` / `sub/../a.txt` / 绝对路径 / 符号链接 ——
    按书写形态做 key 等于"每个写法各排一条队"，一条也排不上。
    Windows 还要 `normcase` 折掉大小写与分隔符：`D:\\p\\A.TXT` 与 `d:/p/a.txt`
    是同一个文件。

    解析失败（路径逃出工作区、非法字符）时返回空串 —— **不做裁决**，
    让下游 `safe_join` 自己报那个更准确错（"path escapes sandbox"），
    而不是在这里换一条让人摸不着头脑的串行化错误。
    """
    if tool_name not in FILE_MUTATION_TOOLS or not isinstance(params, dict):
        return ""
    raw = ""
    for key in _PATH_KEYS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            raw = value.strip()
            break
    if not raw:
        return ""
    try:
        from app.tools.sandbox import safe_join

        target = safe_join(raw)
    except (ValueError, OSError):
        return ""
    return os.path.normcase(str(target))


@asynccontextmanager
async def serialized(
    tool_name: str, params: dict[str, Any] | None
) -> AsyncIterator[None]:
    """在同一个 realpath 上**排队执行**；不参与串行化时是零开销的直通。

    等待期间不持有任何进程级资源，所以排队是廉价的：它只让第二个写者多等一个
    写操作的时长（文件写是毫秒级），换掉的是一次静默覆盖。
    """
    key = mutation_key(tool_name, params)
    if not key:
        yield
        return

    # `_entries.get` 与插入之间**没有 await** ⇒ 在单 loop 内是原子的，不会建出两把锁。
    entry = _entries.get(key)
    if entry is None:
        entry = _entries[key] = _Entry()
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        # 最后一个使用者离开就删条目：既防止字典随"写过的文件数"无限长，
        # 也保证 `asyncio.Lock` 不会跨 loop 复用（每个 loop 内自成一对）。
        if entry.users <= 0 and _entries.get(key) is entry:
            _entries.pop(key, None)


def pending(path: str) -> int:
    """还有多少人在这个路径上排队（含正在写的那一个）。诊断/测试用。"""
    entry = _entries.get(os.path.normcase(str(path)))
    return 0 if entry is None else entry.users


def reset() -> None:
    """清空全部队列条目（测试用）。"""
    _entries.clear()
