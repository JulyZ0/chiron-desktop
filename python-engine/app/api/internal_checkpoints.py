"""检查点维护的内部端点：**差异预览**（只读）与 **CAS 回收（GC）**。

## 为什么在引擎侧

差异预览要复用 ``git_checkpoint`` 的快照/差异原语；GC 要枚举**本机**的
``refs/chiron/checkpoints/**`` 与 ``checkpoint_state_dir()/untracked`` 分片。这两样
都只有引擎能拿到 —— 网关不知道状态目录在哪，也不该知道。

## 鉴权与参数来源

两者都用 ``X-Internal-Token``（与 ``/v1/checkpoints/restore`` 同款，见
:func:`app.api.isolation._verify_gateway_key`）。**归属校验在网关**：这里只认
``root`` / ``commit_sha``，无法判断调用者是否有权操作那个工作区。所以这两个端点
**绝不能**直接暴露给浏览器 —— 网关侧分别是「按 checkpoint_id 查归属」与
「平台 owner 写权限」两条路由。

## GC 的引用集合必须来自数据库，且读不到就中止

见 :mod:`app.agent.checkpoint_maintenance` 模块头：把"查不到"当成"没人引用"
等于清空所有项目的未跟踪文件缓存。因此数据库查询失败时这里直接返回 503，
不做任何删除。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent import checkpoint_hooks
from app.agent import checkpoint_maintenance as cm
from app.agent import git_checkpoint as gc
from app.api.isolation import _verify_gateway_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal/checkpoints", tags=["internal"])

#: 一次 GC 最多扫多少个仓库根。管理端会把平台内的项目根一起传进来；
#: 上限只是防病态配置（几千个已失效的 root_path），不限制正常使用。
MAX_GC_ROOTS = 200


class DiffRequest(BaseModel):
    #: 工作区根（绝对路径）。**必须**是它自己 git 仓库的顶层，否则 4xx。
    root: str = Field(..., min_length=1, max_length=4096)
    #: 检查点 commit。形状在 ``preview_diff`` 里再校验一次。
    commit_sha: str = Field(..., min_length=1, max_length=64)
    session_id: str = Field("", max_length=128)
    #: 有它就能精确取回未跟踪清单；没有则退化为按 (commit, session) 反查。
    checkpoint_id: str = Field("", max_length=64)


class GcRequest(BaseModel):
    #: 单个根（兼容旧调用方）；``roots`` 为空时才用它。
    root: str = Field("", max_length=4096)
    roots: list[str] = Field(default_factory=list)
    retention_days: int = Field(30, description="保留期（天）；0 = 未引用即删")
    dry_run: bool = Field(True, description="默认只预演，不删文件")


def _abs_dir(raw: str) -> Path:
    """校验并返回一个存在的绝对目录；不合法抛 400。

    拒绝相对路径是刻意的：``Path("demo")`` 在引擎工作目录下，可能是任何地方。
    """
    text = (raw or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="root is required")
    path = Path(text)
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="root must be an absolute path")
    if not path.is_dir():
        raise HTTPException(status_code=409, detail="workspace root no longer exists")
    return path


async def _load_untracked(
    *, checkpoint_id: str, commit_sha: str, session_id: str
) -> dict[str, str] | None:
    """读目标快照的未跟踪清单；**读不到返回 None**（而不是空字典）。

    这个区别是整个预览里最重要的一处：

    * ``{}`` = "这个点确实没有未跟踪文件" → 预览里当前多出来的未跟踪文件
      会正确地出现在 ``removed`` 里；
    * ``None`` = "不知道" → 预览必须带 ``untracked_omitted=true``，且不把这批
      文件列进 ``removed``（否则用户会以为回滚会删掉它们，而实际上清单只是没读到）。

    数据库不可用时同样返回 ``None``（预览仍然能覆盖 git 跟踪的文件）。
    """
    try:
        from app.db_client import get_db_client

        db = get_db_client()
        cid = checkpoint_id.strip()
        if not cid and commit_sha:
            # 网关没传 id 时按 (commit, session) 反查；仍取最新一条，
            # 因为同一 commit 在多个会话里可能被引用（parent 链复用）。
            row = await db.fetch_one(
                """SELECT id FROM project_checkpoints
                    WHERE commit_sha = $1 AND ($2 = '' OR session_id = $2)
                    ORDER BY created_at DESC LIMIT 1""",
                [commit_sha, session_id],
            )
            if row:
                cid = str(row.get("id") or "")
        if not cid:
            return None
        rows = await db.fetch_all(
            "SELECT path, sha256 FROM checkpoint_untracked WHERE checkpoint_id = $1",
            [cid],
        )
        manifest = {
            str(r.get("path") or ""): str(r.get("sha256") or "") for r in rows
        }
        manifest.pop("", None)
        return manifest
    except Exception as exc:  # noqa: BLE001 — 读不到清单不影响跟踪文件的预览
        logger.warning("checkpoint diff: untracked manifest unavailable: %s", exc)
        return None


@router.post("/diff")
async def checkpoint_diff(body: DiffRequest, request: Request) -> dict[str, Any]:
    """预览"回到这个检查点"会动哪些文件；**只读，绝不修改工作区**。"""
    _verify_gateway_key(request)
    root = _abs_dir(body.root)

    untracked = await _load_untracked(
        checkpoint_id=body.checkpoint_id,
        commit_sha=body.commit_sha,
        session_id=body.session_id,
    )

    try:
        result = cm.preview_diff(
            root,
            commit_sha=body.commit_sha,
            untracked=untracked,
            state_dir=checkpoint_hooks.checkpoint_state_dir(),
            session_id=body.session_id,
        )
    except gc.GitError as exc:
        logger.warning("checkpoint diff failed root=%s: %s", root, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("checkpoint diff io failed root=%s: %s", root, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    out = result.as_dict()
    out["untracked_known"] = untracked is not None
    return out


async def _reference_sets() -> tuple[set[str], set[str]]:
    """从 PG 取 ``(活引用, 全部清单摘要)``。**失败必须抛** —— 见模块头。"""
    from app.db_client import get_db_client

    db = get_db_client()
    live_rows = await db.fetch_all(
        """SELECT DISTINCT u.sha256 AS sha256
             FROM checkpoint_untracked u
             JOIN project_checkpoints c ON c.id = u.checkpoint_id"""
    )
    all_rows = await db.fetch_all("SELECT DISTINCT sha256 FROM checkpoint_untracked")
    live = {str(r.get("sha256") or "").strip() for r in live_rows}
    every = {str(r.get("sha256") or "").strip() for r in all_rows}
    live.discard("")
    every.discard("")
    return live, every


@router.post("/gc")
async def checkpoint_gc(body: GcRequest, request: Request) -> dict[str, Any]:
    """回收未被引用的未跟踪 CAS 分片。

    ``dry_run`` 默认 **True**：管理端先看计划，确认后再发一次 ``dry_run=false``。
    """
    _verify_gateway_key(request)

    raw_roots = [r for r in ([body.root] if body.root.strip() else []) + list(body.roots) if r.strip()]
    if len(raw_roots) > MAX_GC_ROOTS:
        raise HTTPException(status_code=400, detail=f"too many roots (max {MAX_GC_ROOTS})")
    roots: list[Path] = []
    for raw in raw_roots:
        try:
            roots.append(_abs_dir(raw))
        except HTTPException:
            # 已失效的项目根不该让整次 GC 失败：它本来就没有引用可算。
            continue

    try:
        live, every = await _reference_sets()
    except Exception as exc:  # noqa: BLE001 — 见模块头：读不到引用就不许删
        logger.error("checkpoint gc aborted: reference set unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="database unavailable; gc aborted (refusing to delete without reference set)",
        ) from exc

    retention = cm.clamp_retention_days(body.retention_days)
    try:
        report = cm.gc_execute(
            checkpoint_hooks.checkpoint_state_dir() / "untracked",
            roots=roots,
            referenced=live,
            manifest_digests=every,
            retention_days=retention,
            dry_run=bool(body.dry_run),
        )
    except OSError as exc:
        logger.warning("checkpoint gc io failed: %s", exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    out = report.as_dict()
    out["retention_days"] = retention
    return out
