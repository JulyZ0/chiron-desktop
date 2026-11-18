"""知识库**索引状态**的唯一取值集合（引擎侧权威定义）。

## 为什么单独一个模块

这套取值此前是散落的字面量：``app/knowledge/service.py`` 有 ``STATUS_INDEXING``、
``pageindex-service/app/index_worker.py`` 里到处写 ``"ready"`` / ``"error"`` /
``"indexing"``、前端 ``api/knowledge.ts`` 又抄了一份 ``KbStatus`` 联合类型 —— 而
``KnowledgeDetailView.vue`` 判断"构建完成"时写的是 **``status === 'active'``**，
一个**从未被任何写入方写过的值**。结果是索引真的跑完、库已经是 ``ready``，
界面还在轮询十次然后默默放弃。同源问题还有"是否正在构建"用了 ``'building'``
（同样不存在的取值），于是"构建中"的按钮分支永远不生效。

取值收敛到一处后，任何新增状态只改这里，前端联合类型与 worker 侧字面量按此对齐
（``pageindex-service`` 是独立包、不能 import 引擎代码，那份镜像在
``pageindex-service/app/store.py`` 里并注明与本文件逐项对齐）。
"""

from __future__ import annotations

#: 尚未索引：新建库的初值，也用于"索引被清空/原文全删"后的回落。
STATUS_DISABLED = "disabled"
#: 有索引任务正在执行（worker 取到任务后写入）。
STATUS_INDEXING = "indexing"
#: 索引完成、可检索。
STATUS_READY = "ready"
#: 索引失败（error_message 记录原因）。
STATUS_ERROR = "error"

#: 全部合法取值（DB 写入与前端展示以它为准）。
KB_STATUSES: tuple[str, ...] = (STATUS_DISABLED, STATUS_INDEXING, STATUS_READY, STATUS_ERROR)

#: "正在构建"的判据：只有这一个取值表示索引在执行。
#: 判定必须读**数据库里的 status**，不能只看队列里有没有任务 —— 任务可能已进 DLQ、
#: 可能被别的实例持锁延后、也可能早已 ACK 而 index 仍在跑；反过来只看本地内存里的
#: `building` 标志则跨实例/跨页面都不可见。
BUILDING_STATUSES: frozenset[str] = frozenset({STATUS_INDEXING})

#: 构建**完成**的判据（前端轮询据此停止）。``error`` 也算一种终态：
#: 继续轮询一个已经失败的库只会让用户等到超时。
TERMINAL_STATUSES: frozenset[str] = frozenset({STATUS_READY, STATUS_ERROR})


def is_building(status: str | None) -> bool:
    return str(status or "") in BUILDING_STATUSES


def is_terminal(status: str | None) -> bool:
    return str(status or "") in TERMINAL_STATUSES
