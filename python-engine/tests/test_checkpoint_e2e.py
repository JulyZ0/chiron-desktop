"""P1 检查点端到端：把"用户真的点了一次"的链路整条跑一遍。

覆盖四个真实场景（与验收项一一对应）：

1. **本地非 Git 项目**：建点 → 预览 → 恢复（先不删、再删）→ 新增/删除文件；
2. **远程 client 项目的服务端镜像根**：恢复时用可信 ``root``（镜像），设备路径不参与；
3. **密钥不进快照**：``.env`` / ``.ssh`` / 私钥 / ``.aws`` 既不在清单里，也不在 CAS 里；
4. **失败必须可见**：清单不全、CAS blob 缺失时**不许**返回 ``ok=True``。

这里刻意只走公开入口：``attempt_checkpoint``（建点）、
``checkpoint_maintenance.preview_diff``（内部 diff 端点的实现）、
``app.api.checkpoints.restore_checkpoint``（网关转发到的那个端点）。
单元细节归各自模块的测试；本文件钉的是"用户会看到什么"。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agent import checkpoint_maintenance as cm
from app.agent import checkpoint_service as cs
from app.agent import git_checkpoint as gc
from app.api import checkpoints


class _Request:
    """restore 端点只把它传给 ``_verify_gateway_key``（下面会被替换掉）。"""


class _FakeCheckpointDB:
    """最小 DB：一行 checkpoint + 未跟踪清单 + ``applied_at`` 记账探针。"""

    def __init__(self, row: dict, manifest: list[dict]) -> None:
        self.row = row
        self.manifest = manifest
        self.applied = False

    async def fetch_one(self, _query: str, _args: object) -> dict:
        return self.row

    async def fetch_all(self, _query: str, _args: object) -> list[dict]:
        return self.manifest

    async def execute(self, _query: str, _args: object) -> None:
        self.applied = True


class _PlainProject:
    """一个**不在任何仓库里**的项目 + 它的检查点状态目录。

    必须真正落在仓库之外：放在 ``tmp_path`` 里会向上命中本仓库的 ``.git``，
    ``resolve_layout`` 就会成功，"普通目录"路径根本没被覆盖。因此统一用
    ``tempfile.mkdtemp``，而不是 pytest 的 ``tmp_path``。
    """

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="chiron-cp-e2e-"))
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.state = self.base / "state"

    def cleanup(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)


@pytest.fixture()
def plain(monkeypatch: pytest.MonkeyPatch) -> _PlainProject:
    project = _PlainProject()
    # 双保险：即使某台机器的临时目录恰好在仓库里，也不许 git 向上发现父仓库。
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(project.base))
    monkeypatch.setenv("CHIRON_CHECKPOINT_STATE_DIR", str(project.state))
    monkeypatch.setattr(checkpoints, "_verify_gateway_key", lambda _request: None)
    yield project
    project.cleanup()


def _wire_db(monkeypatch: pytest.MonkeyPatch, fake: _FakeCheckpointDB) -> None:
    monkeypatch.setattr("app.db_client.get_db_client", lambda: fake)


def _checkpoint_row(
    rec: cs.CheckpointRecord, *, root_path: str, manifest: list[dict] | None = None
) -> dict:
    return {
        "state": "ok",
        "session_id": "s-e2e",
        "tree_sha": rec.tree_sha,
        "commit_sha": rec.commit_sha,
        "file_count": rec.file_count,
        "workspace_id": "ws-e2e",
        "root_path": root_path,
        "_manifest": manifest,
    }


# ---------------------------------------------------------------------------
# 1) 本地非 Git 项目：建点 → 预览 → 恢复 → 删除/新增文件
# ---------------------------------------------------------------------------


async def test_plain_project_create_preview_restore_cycle(
    plain: _PlainProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = plain.root
    (root / "a.py").write_text("before\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("keep\n", encoding="utf-8")
    (root / "README.md").write_text("# doc\n", encoding="utf-8")

    rec = cs.attempt_checkpoint(
        root, session_id="s-e2e", seq=1, kind="pre_turn", state_dir=plain.state
    )
    assert rec is not None and rec.succeeded, rec.error if rec else "no record"
    assert rec.file_count == 3
    # 普通目录绝不能被动成 git 仓库。
    assert not (root / ".git").exists()
    # 提交/ref 落在影子裸库（状态目录），内容进 CAS。
    shadow = cs.shadow_git_dir(root, plain.state)
    assert gc.read_ref(shadow, rec.ref_name) == rec.commit_sha

    # 回合把工作区改成"改一个、删一个、加一个"。
    (root / "a.py").write_text("after\n", encoding="utf-8")
    (root / "sub" / "b.txt").unlink()
    (root / "new.md").write_text("draft\n", encoding="utf-8")

    preview = cm.preview_diff(
        root,
        commit_sha=rec.commit_sha,
        untracked=dict(rec.untracked),
        state_dir=plain.state,
        session_id="s-e2e",
        expected_files=rec.file_count,
    )
    assert preview.restored == ("a.py",)
    # 目标有、当前没有 ⇒ 恢复会把它写回来（口径与 git 树模式的 added 一致）。
    assert preview.added == ("sub/b.txt",)
    assert preview.removed == ("new.md",)
    assert preview.untracked_omitted is False
    assert preview.current_files == 3 and preview.target_files == 3

    fake = _FakeCheckpointDB(
        _checkpoint_row(rec, root_path=str(root)),
        [{"path": p, "sha256": d} for p, d in rec.untracked],
    )
    _wire_db(monkeypatch, fake)

    # 第一次恢复：默认不删新增文件，但必须如实报出"这些没有删"。
    first = await checkpoints.restore_checkpoint(
        _Request(), checkpoints.RestoreRequest(checkpoint_id="cp-e2e", allow_delete=False)
    )
    assert first["ok"] is True, first["failed"]
    assert (root / "a.py").read_text(encoding="utf-8") == "before\n"
    assert (root / "sub" / "b.txt").read_text(encoding="utf-8") == "keep\n"
    assert (root / "new.md").exists(), "allow_delete=False 时不得删用户新增的文件"
    assert first["skipped_removed"] == ["new.md"]
    assert fake.applied is True

    # 第二次恢复：显式允许删除，只对这一个路径生效。
    fake.applied = False
    second = await checkpoints.restore_checkpoint(
        _Request(),
        checkpoints.RestoreRequest(
            checkpoint_id="cp-e2e", allow_delete=True, paths=["new.md"]
        ),
    )
    assert second["ok"] is True, second["failed"]
    assert second["deleted"] == ["new.md"]
    assert not (root / "new.md").exists()
    assert fake.applied is True

    # 再建一个点：影子仓库里序号必须自增（钩子看不到影子 ref 时会重复传 1）。
    nxt = cs.attempt_checkpoint(
        root,
        session_id="s-e2e",
        seq=1,
        kind="post_turn",
        state_dir=plain.state,
        parent_sha=rec.commit_sha,
    )
    assert nxt is not None and nxt.succeeded and nxt.seq == 2, nxt.error
    assert not (root / ".git").exists()


# ---------------------------------------------------------------------------
# 2) client 项目：恢复只认服务端镜像根
# ---------------------------------------------------------------------------


async def test_client_mirror_restore_ignores_device_path(
    plain: _PlainProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """client 项目的 ``root_path`` 是设备路径，恢复必须写进网关给的可信 ``root``。"""
    root = plain.root
    (root / "index.ts").write_text("v1\n", encoding="utf-8")
    rec = cs.attempt_checkpoint(
        root, session_id="s-dev", seq=1, kind="pre_turn", state_dir=plain.state
    )
    assert rec is not None and rec.succeeded, rec.error
    (root / "index.ts").write_text("v2\n", encoding="utf-8")

    fake = _FakeCheckpointDB(
        _checkpoint_row(rec, root_path=r"D:\device\project"),
        [{"path": p, "sha256": d} for p, d in rec.untracked],
    )
    _wire_db(monkeypatch, fake)

    result = await checkpoints.restore_checkpoint(
        _Request(),
        checkpoints.RestoreRequest(checkpoint_id="cp-dev", root=str(root)),
    )
    assert result["ok"] is True, result["failed"]
    assert (root / "index.ts").read_text(encoding="utf-8") == "v1\n"
    # 设备路径不能被当成可写目录（它在本机不存在，若被采用就会 409）。
    assert fake.applied is True


# ---------------------------------------------------------------------------
# 3) .env / .ssh / 私钥既不在清单里，也不在 CAS 里
# ---------------------------------------------------------------------------


async def test_plain_project_secrets_stay_out_of_manifest_and_cas(
    plain: _PlainProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = plain.root
    (root / "main.py").write_text("print(1)\n", encoding="utf-8")
    secrets = {
        ".env": "SECRET=top",
        ".ssh/id_rsa": "PRIVATE-RSA",
        ".ssh/config": "Host *",
        ".aws/credentials": "PRIVATE-AWS",
        "deploy/server.pem": "PRIVATE-PEM",
        "id_ed25519": "PRIVATE-ED",
        ".npmrc": "//registry/:_authToken=PRIVATE-NPM",
    }
    for rel, body in secrets.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("noise\n", encoding="utf-8")

    rec = cs.attempt_checkpoint(
        root, session_id="s-sec", seq=1, kind="pre_turn", state_dir=plain.state
    )
    assert rec is not None and rec.succeeded, rec.error
    assert [p for p, _ in rec.untracked] == ["main.py"]
    assert rec.file_count == 1
    assert rec.skipped_count >= 1, "被剔除的密钥要计数，用户有权知道没被保护"

    # CAS 里不得出现任何密钥内容（按摘要分片，逐块读回看明文）。
    store_root = plain.state / "untracked"
    blobs = [p for p in store_root.rglob("*") if p.is_file()]
    assert blobs, "main.py 必须真的进了 CAS"
    for blob in blobs:
        data = blob.read_bytes()
        assert b"PRIVATE" not in data and b"SECRET" not in data, f"{blob} 泄漏了密钥明文"

    # 恢复（允许删除）之后，用户自己新增的密钥文件也不该被"回滚"掉。
    (root / ".env.prod").write_text("SECRET=prod", encoding="utf-8")
    (root / "main.py").write_text("changed\n", encoding="utf-8")
    fake = _FakeCheckpointDB(
        _checkpoint_row(rec, root_path=str(root)),
        [{"path": p, "sha256": d} for p, d in rec.untracked],
    )
    _wire_db(monkeypatch, fake)
    result = await checkpoints.restore_checkpoint(
        _Request(), checkpoints.RestoreRequest(checkpoint_id="cp-sec", allow_delete=True)
    )
    assert result["ok"] is True, result["failed"]
    assert (root / "main.py").read_text(encoding="utf-8") == "print(1)\n"
    for rel, body in secrets.items():
        assert (root / rel).read_text(encoding="utf-8") == body
    assert (root / ".env.prod").exists(), "密钥类文件永不出现在可删除清单里"


# ---------------------------------------------------------------------------
# 4) 失败必须可见：清单不全 / blob 缺失都不许报成功
# ---------------------------------------------------------------------------


async def test_restore_refuses_incomplete_manifest_instead_of_fake_success(
    plain: _PlainProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单少了几条时，恢复只能明确失败。

    缺陷形态：``checkpoint_untracked`` 只落了一部分（上报被截断 / 写库失败）时，
    普通目录的恢复只写回那几条、其余一个字节都不动，却返回 ``ok=True`` ——
    用户看到"回滚成功"，工作区其实是半截。
    """
    root = plain.root
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    rec = cs.attempt_checkpoint(
        root, session_id="s-part", seq=1, kind="pre_turn", state_dir=plain.state
    )
    assert rec is not None and rec.succeeded, rec.error
    assert rec.file_count == 3
    (root / "a.py").write_text("modified\n", encoding="utf-8")

    fake = _FakeCheckpointDB(
        _checkpoint_row(rec, root_path=str(root)),
        # 只有一条清单：库里少了 b.py / c.py。
        [{"path": "a.py", "sha256": dict(rec.untracked)["a.py"]}],
    )
    _wire_db(monkeypatch, fake)

    with pytest.raises(HTTPException) as excinfo:
        await checkpoints.restore_checkpoint(
            _Request(), checkpoints.RestoreRequest(checkpoint_id="cp-part", allow_delete=True)
        )
    assert excinfo.value.status_code == 409
    assert "incomplete" in str(excinfo.value.detail)
    # 拒绝对工作区做任何改动，也不记账（记账会让时间线显示"被用过"）。
    assert (root / "a.py").read_text(encoding="utf-8") == "modified\n"
    assert fake.applied is False

    # 预览同理：清单不全的预览会错误地声称"回滚会删掉 b.py/c.py"。
    with pytest.raises(gc.GitError) as preview_exc:
        cm.preview_diff(
            root,
            commit_sha=rec.commit_sha,
            untracked={"a.py": dict(rec.untracked)["a.py"]},
            state_dir=plain.state,
            expected_files=rec.file_count,
        )
    assert "incomplete" in str(preview_exc.value)


async def test_restore_reports_missing_blob_and_skips_applied_at(
    plain: _PlainProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CAS blob 丢了 ⇒ ``ok=False`` + ``failed`` 里有可读原因，且不记 ``applied_at``。"""
    root = plain.root
    (root / "a.py").write_text("before\n", encoding="utf-8")
    (root / "b.py").write_text("b\n", encoding="utf-8")
    rec = cs.attempt_checkpoint(
        root, session_id="s-blob", seq=1, kind="pre_turn", state_dir=plain.state
    )
    assert rec is not None and rec.succeeded, rec.error
    (root / "a.py").write_text("after\n", encoding="utf-8")

    manifest = dict(rec.untracked)
    manifest["a.py"] = "0" * 64  # 指向一个不存在的 blob
    fake = _FakeCheckpointDB(
        _checkpoint_row(rec, root_path=str(root)),
        [{"path": p, "sha256": d} for p, d in manifest.items()],
    )
    _wire_db(monkeypatch, fake)

    result = await checkpoints.restore_checkpoint(
        _Request(), checkpoints.RestoreRequest(checkpoint_id="cp-blob")
    )
    assert result["ok"] is False, "blob 缺失绝不能报成功"
    assert [item["path"] for item in result["failed"]] == ["a.py"]
    assert result["failed"][0]["error"], "失败原因必须可读"
    # 已经落地的文件照旧保留（反转风险更大），但整体不得标记成功。
    assert fake.applied is False
    assert (root / "a.py").read_text(encoding="utf-8") == "after\n"
