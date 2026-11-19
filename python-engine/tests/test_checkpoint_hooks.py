"""`checkpoint_hooks` 的契约测试。

钩子挂在 `runtime.run()` 主路径上，因此**只有两个**要求：
未绑定项目时什么都不做；任何失败都不抛异常。

此外固定住"失败记录也要送到网关"这条上报契约 —— 只存在快照里、不落库的失败
记录，用户在前端依然什么都看不到。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.agent import checkpoint_hooks as hooks
from app.agent import checkpoint_service as cs


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "a.py").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=root, check=True, capture_output=True)
    return root


def test_no_project_root_is_a_noop(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hooks, "project_root", lambda: None)
    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st") is None


def test_creates_checkpoint_when_project_bound(monkeypatch, tmp_path: Path) -> None:
    root = _repo(tmp_path)
    monkeypatch.setattr(hooks, "project_root", lambda: root)
    monkeypatch.setattr(hooks, "_session_id", lambda: "sess-1")
    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st") == 1
    # 第二次必须是 2：序号来自已落下的 ref，不是每次从 1 重来。
    assert hooks.turn_checkpoint("post_turn", state_dir=tmp_path / "st") == 2


def test_failure_never_raises(monkeypatch, tmp_path: Path) -> None:
    """项目根是个普通目录（非 git）→ 返回 None，不抛异常。"""
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setattr(hooks, "project_root", lambda: plain)
    monkeypatch.setattr(hooks, "_session_id", lambda: "s2")
    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st2") is None


def test_seq_survives_packed_refs(monkeypatch, tmp_path: Path) -> None:
    """★★ `git pack-refs` 之后序号必须继续递增（还原点不能"用一阵子就不工作了"）。

    缺陷形态（实测）：`_next_seq` 曾遍历 `.git/refs/chiron/checkpoints/<session>/`
    数松散文件。`git gc` 默认带 `pack-refs --all --prune`，打包后目录消失、ref 只留在
    `.git/packed-refs` 里 ⇒ 序号回退到 1 ⇒ `write_ref` 的 CAS（旧值必须为空）必然失败
    ⇒ 此后**每次**建点都静默失败（fail-open，对话照常），用户只看到时间线不再增长。

    判据：打包后仍能建点，且序号是 3 而不是重复的 1。
    """
    root = _repo(tmp_path)
    monkeypatch.setattr(hooks, "project_root", lambda: root)
    monkeypatch.setattr(hooks, "_session_id", lambda: "sess-p")

    assert hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st") == 1
    assert hooks.turn_checkpoint("post_turn", state_dir=tmp_path / "st") == 2

    # 把松散 ref 收进 packed-refs（等价于用户仓库被 gc 过）
    subprocess.run(
        ["git", "pack-refs", "--all", "--prune"],
        cwd=root, check=True, capture_output=True,
    )
    loose = root / ".git" / "refs" / "chiron" / "checkpoints" / "sess-p"
    assert not loose.exists(), "前提不成立：pack-refs 后松散目录应当消失"

    # 打包之后必须还能建点（旧实现会静默返回 None）
    seq = hooks.turn_checkpoint("pre_turn", state_dir=tmp_path / "st")
    assert seq == 3, f"pack-refs 后序号回退/建点失败：seq={seq!r}（期望 3）"


# ---------------------------------------------------------------------------
# 上报契约：失败记录必须带着原因送到网关
# ---------------------------------------------------------------------------


class _FakeResponse:
    status_code = 200
    text = "ok"


#: 最近一次 `report_checkpoint` 真正发出去的请求（`_FakeClient.post` 落进来）。
_CAPTURED: list[dict[str, object]] = []


class _FakeClient:
    """够用的 httpx.AsyncClient 替身：记录 POST 的地址与载荷，不做任何网络访问。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, *, json: object = None, headers: object = None) -> _FakeResponse:
        _CAPTURED.append({"url": url, "payload": json, "headers": headers})
        return _FakeResponse()


@pytest.mark.asyncio
async def test_failed_record_is_reported_with_reason(monkeypatch) -> None:
    """★★ 失败态记录必须上报，且 `state` / `error` / `error_code` 一个都不能少。

    缺陷形态：上报层曾把"没有 commit_sha"的记录直接挡回去 —— 而失败态**本来就没有**
    commit。于是网关的 `project_checkpoints` 只会有成功行，时间线上"这一刻没保护成功"
    永远不出现，用户看到的是"还原点好像没有了"。
    """
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _CAPTURED.clear()

    reason = (
        "project root is inside another git repository: /tmp/outer/inner "
        "(repository top-level: /tmp/outer)"
    )
    rec = cs.CheckpointRecord(
        seq=7,
        kind="pre_turn",
        commit_sha="",
        tree_sha="",
        parent_sha=None,
        head_sha=None,
        ref_name="",
        file_count=0,
        skipped_count=0,
        untracked_count=0,
        state="failed",
        error=reason,
        error_code="git",
    )

    ok = await hooks.report_checkpoint(
        rec, session_id="sess-1", turn_id="turn-9", base_url="http://gw.local/", token="tok"
    )

    assert ok is True
    assert len(_CAPTURED) == 1, "失败记录必须真的发出一次"
    sent = _CAPTURED[0]
    assert sent["url"] == "http://gw.local/v1/internal/checkpoints"
    payload = sent["payload"]
    assert isinstance(payload, dict)
    assert payload["session_id"] == "sess-1"
    # turn_id 必须透传（网关靠它把检查点与计费回合对上）。
    assert payload["turn_id"] == "turn-9"
    assert payload["seq"] == 7 and payload["kind"] == "pre_turn"
    assert payload["state"] == "failed"
    assert payload["error"] == reason
    assert payload["error_code"] == "git"
    assert payload["commit_sha"] == "" and payload["ref_name"] == ""


@pytest.mark.asyncio
async def test_success_record_without_commit_is_not_reported(monkeypatch) -> None:
    """成功态却缺 commit ⇒ 记录本身不成立，不该往时间线上塞一行。"""
    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    _CAPTURED.clear()  # 这条路径根本不会构造客户端，必须主动清
    rec = cs.CheckpointRecord(
        seq=1,
        kind="post_turn",
        commit_sha="",
        tree_sha="a" * 40,
        parent_sha=None,
        head_sha=None,
        ref_name="refs/chiron/checkpoints/s/000001-post_turn",
        file_count=1,
        skipped_count=0,
        untracked_count=0,
    )
    assert await hooks.report_checkpoint(
        rec, session_id="s", base_url="http://gw.local", token="tok"
    ) is False
    assert _CAPTURED == []
