"""检查点恢复 API：本地普通目录与 client 镜像根的端到端回归。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agent import checkpoint_service as cs
from app.api import checkpoints


class _Request:
    pass


class _DB:
    def __init__(self, checkpoint: dict, manifest: list[dict]) -> None:
        self.checkpoint = checkpoint
        self.manifest = manifest
        self.applied = False

    async def fetch_one(self, _query: str, _args: object) -> dict:
        return self.checkpoint

    async def fetch_all(self, _query: str, _args: object) -> list[dict]:
        return self.manifest

    async def execute(self, _query: str, _args: object) -> None:
        self.applied = True


@pytest.mark.asyncio
@pytest.mark.parametrize("client_mirror", [False, True])
async def test_restore_plain_checkpoint_uses_trusted_mirror_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client_mirror: bool
) -> None:
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "a.txt").write_text("before\n", encoding="utf-8")
    state = tmp_path / "state"
    record = cs.create_checkpoint(
        root, session_id="s1", seq=1, kind="pre_turn", state_dir=state
    )
    assert record is not None

    (root / "a.txt").write_text("after\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")
    fake_db = _DB(
        {
            "state": "ok",
            "session_id": "s1",
            "tree_sha": record.tree_sha,
            # client 项目库里存的是设备路径，恢复时不能把它当服务端 root。
            "root_path": r"D:\device\project" if client_mirror else str(root),
        },
        [{"path": path, "sha256": digest} for path, digest in record.untracked],
    )
    monkeypatch.setattr(checkpoints, "_verify_gateway_key", lambda _request: None)
    monkeypatch.setattr("app.db_client.get_db_client", lambda: fake_db)
    monkeypatch.setenv("CHIRON_CHECKPOINT_STATE_DIR", str(state))

    body = checkpoints.RestoreRequest(
        checkpoint_id="cp1",
        root=str(root) if client_mirror else None,
        allow_delete=True,
    )
    result = await checkpoints.restore_checkpoint(_Request(), body)

    assert result["ok"] is True, result["failed"]
    assert (root / "a.txt").read_text(encoding="utf-8") == "before\n"
    assert not (root / "new.txt").exists()
    assert fake_db.applied is True
    assert not (root / ".git").exists()
