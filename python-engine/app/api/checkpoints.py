"""还原点：查询与回滚（内部端点，沿用 ``/v1/system/isolation`` 的凭据校验）。

## 为什么回滚放在引擎侧

回滚要动的是**用户磁盘上的工作区**：把一批文件改回快照里的内容、删掉快照之后
新增的文件、清掉因此变空的目录。这件事只有引擎能做对，因为：

* 快照的 CAS blob 存在引擎自己算出的 ``checkpoint_state_dir()`` 里；
* ``git_checkpoint.restore()`` 的两阶段落地（先全部取到临时目录，再逐个落地）
  是**唯一**被实测验证过的实现，网关侧另写一份必然与它行为不一致。

网关的职责只是鉴权与转发，前端只需要给一个 ``checkpoint_id``。

## 归属与路径都由可信调用方确定

请求体通常只有 ``checkpoint_id``（以及可选的路径子集与 ``allow_delete``）。
本地项目根从数据库取；client 项目的设备 Windows 路径不能直接在服务端使用，因此
网关在完成 tenant/user 归属校验后可传可信的 ``root``（实际服务端镜像）。该字段
只接受内部令牌调用，外部客户端不能借它指定任意写目录。

## ``allow_delete`` 默认 False

这是 ``git_checkpoint`` 的既有契约（D5：可配置，但默认禁止删除）。
"回滚"在用户心里的默认含义常常是"把改过的文件改回去"，而不是"删掉我之后新建
的文件"。默认不删、并在响应的 ``skipped_removed`` 里如实列出"这些没有删"，
比自作主张删掉更安全，也让用户能带着知情再点一次。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.agent import checkpoint_hooks
from app.agent import checkpoint_service as cs
from app.agent import git_checkpoint as gc
from app.api.isolation import _verify_gateway_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/checkpoints", tags=["checkpoints"])

#: ``paths`` 的上限。逐文件回滚是给人用的，上千条只可能是脚本出了错。
MAX_RESTORE_PATHS = 2000


class RestoreRequest(BaseModel):
    checkpoint_id: str = Field(..., min_length=1, max_length=64)
    root: str | None = Field(
        None,
        max_length=4096,
        description="可信网关为 client 项目传入的服务端镜像根；本地项目不要传",
    )
    allow_delete: bool = Field(
        False, description="是否删除快照之后新增的文件（默认否）"
    )
    paths: list[str] | None = Field(
        None, description="只回滚这些路径；None = 全部"
    )


def _as_int(value: object) -> int:
    """把 DB 里取回的计数转成 int；``None``/不可解析按 0（不抛，DB 驱动可能是 Decimal/str）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _split_paths(paths: list[str] | None) -> list[str] | None:
    if paths is None:
        return None
    if len(paths) > MAX_RESTORE_PATHS:
        raise HTTPException(
            status_code=400,
            detail=f"too many paths (max {MAX_RESTORE_PATHS})",
        )
    # 拒绝绝对路径与上跳：``restore`` 内部是 ``root / path``，绝对路径会让它写到
    # 工作区之外。这不是可以靠调用方自律的事。
    out: list[str] = []
    for raw in paths:
        p = raw.replace("\\", "/").strip()
        if not p or p.startswith("/") or (len(p) > 1 and p[1] == ":"):
            raise HTTPException(status_code=400, detail=f"invalid path: {raw}")
        if ".." in p.split("/"):
            raise HTTPException(status_code=400, detail=f"invalid path: {raw}")
        out.append(p)
    return out


@router.post("/restore")
async def restore_checkpoint(request: Request, body: RestoreRequest) -> dict[str, Any]:
    """把一个还原点重新应用到工作区。"""
    _verify_gateway_key(request)

    from app.db_client import get_db_client

    db = get_db_client()
    row = await db.fetch_one(
        """SELECT c.ref_name, c.tree_sha, c.commit_sha, c.session_id, c.state,
                  COALESCE(c.file_count, 0) AS file_count,
                  c.workspace_id, w.root_path AS root_path
             FROM project_checkpoints c
             JOIN session_map_workspaces w ON w.id = c.workspace_id
            WHERE c.id = $1""",
        [body.checkpoint_id],
    )
    if not row:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    if str(row.get("state") or "ok") != "ok":
        # 状态不是 ok 的快照本身就没完整写出来，拿它回滚会得到半截工作区。
        raise HTTPException(
            status_code=409,
            detail=f"checkpoint state is {row.get('state')!r}, not 'ok'",
        )
    root_raw = str(body.root or row.get("root_path") or "").strip()
    if not root_raw:
        raise HTTPException(status_code=409, detail="workspace has no root path")
    root = Path(root_raw)
    if body.root is not None and not root.is_absolute():
        raise HTTPException(status_code=400, detail="trusted root must be absolute")
    if not root.is_dir():
        raise HTTPException(status_code=409, detail="workspace root no longer exists")

    tree = str(row.get("tree_sha") or "")
    if not tree:
        raise HTTPException(status_code=409, detail="checkpoint has no tree")

    manifest = await db.fetch_all(
        "SELECT path, sha256 FROM checkpoint_untracked WHERE checkpoint_id = $1",
        [body.checkpoint_id],
    )
    untracked = {
        str(r.get("path") or ""): str(r.get("sha256") or "") for r in manifest
    }
    untracked.pop("", None)

    state_dir = checkpoint_hooks.checkpoint_state_dir()
    session_id = str(row.get("session_id") or "")
    index_file = state_dir / f"snap-{session_id}.idx"

    restore_paths = _split_paths(body.paths)
    shadow = cs.shadow_git_dir(root, state_dir)
    try:
        try:
            gc.resolve_layout(root)
            plain = False
        except gc.GitError:
            # 只有确实创建过影子仓库的普通目录才能走 CAS 恢复；真实 Git 仓库
            # 若因 git 缺失/损坏而解析失败，绝不能退化成"只恢复未跟踪文件"。
            if not (shadow / "HEAD").exists():
                raise
            plain = True

        if plain:
            # 普通目录的**整个快照**就是这份清单（没有 git 树）。清单短于建点时
            # 上报的文件数，说明 `checkpoint_untracked` 没落全（上报被截断/写库失败）。
            # 此时恢复只会写出其中一部分，却仍然返回 ``ok=True`` —— 用户看到"回滚成功"
            # 而工作区只回来一半。宁可明确失败，也不假成功。
            expected = _as_int(row.get("file_count"))
            if expected > len(untracked):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"checkpoint file manifest is incomplete: "
                        f"{len(untracked)} of {expected} files known; "
                        "refusing to restore a partial snapshot"
                    ),
                )
            result = cs.restore_plain_directory(
                root,
                manifest=untracked,
                store_root=state_dir / "untracked",
                paths=restore_paths,
                allow_delete=body.allow_delete,
            )
        else:
            tree_paths, non_regular = gc._tree_entries(root, tree)
            target = gc.Snapshot(
                tree=tree,
                untracked=untracked,
                tree_paths=frozenset(tree_paths),
                non_regular=frozenset(non_regular),
            )
            result = gc.restore(
                root,
                target=target,
                store_root=state_dir / "untracked",
                paths=restore_paths,
                allow_delete=body.allow_delete,
                index_file=index_file,
            )
    except gc.GitError as exc:
        logger.warning("checkpoint restore failed id=%s: %s", body.checkpoint_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        logger.warning("checkpoint restore io failed id=%s: %s", body.checkpoint_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # 记账：回滚成功才记 ``applied_at``，让时间线能标出"这个点被用过"。
    # 失败**不记** —— 记了会让用户以为它已经生效。
    if result.ok:
        try:
            await db.execute(
                "UPDATE project_checkpoints SET applied_at = NOW() WHERE id = $1",
                [body.checkpoint_id],
            )
        except Exception as exc:  # noqa: BLE001 — 记账失败不该让回滚看起来失败
            logger.warning("checkpoint applied_at update failed id=%s: %s",
                           body.checkpoint_id, exc)

    return {
        "ok": result.ok,
        "written": list(result.written),
        "added": list(result.added),
        "deleted": list(result.deleted),
        "pruned_dirs": list(result.pruned_dirs),
        "failed": [{"path": p, "error": e} for p, e in result.failed],
        "skipped_removed": list(result.skipped_removed),
        "protected_removed": list(result.protected_removed),
        "unsupported": list(result.unsupported),
    }
