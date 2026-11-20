"""``/internal/deps/snapshots*`` 契约测试。

快照是"装坏了能回去"的唯一保证，所以这四件事必须被锁死：

1. **落盘位置与内容**：``<data_dir>/deps-snapshots/<id>.json``，含 pip list 结果 +
   时间戳 + 触发动作；保留最近 20 份（清理更旧的）；
2. **id 白名单**：id 会被拼进文件路径，``../x`` / ``a/b`` / ``.env`` 一律拒绝（白名单 +
   realpath 双保险）；
3. **差异算法**：多出的卸、缺失的装、版本不同的按快照版本钉住 —— 大小写不敏感匹配，
   但展示保留原始大小写；
4. **恢复的执行**：``dry_run`` 只算差异不建任务；真恢复走后台任务（复用单任务闸），
   一次最多装 50 个包，快照文件里的非法条目在入口就被拒绝。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

import app.api.deps as deps_api
from app.api.deps import (
    MAX_RESTORE_INSTALLS,
    MAX_SNAPSHOTS,
    compute_restore_diff,
    snapshot_path,
)
from app.config import settings
from app.main import create_app

BASE = "/internal/deps"

CURRENT = [
    {"name": "fastapi", "version": "0.115.0"},
    {"name": "requests", "version": "2.32.0"},
]


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """任务表 / 单任务闸 / 数据目录三者都隔离（快照会真的写文件）。"""
    monkeypatch.setattr(deps_api, "_jobs", {})
    monkeypatch.setattr(deps_api, "_running_job_id", None)
    monkeypatch.setattr(deps_api, "_pypi_cache", {})
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


def _pip_ok(packages: list[dict[str, str]]) -> deps_api.PipResult:
    return deps_api.PipResult(ok=True, returncode=0, output=json.dumps(packages))


# ── 路径校验 ─────────────────────────────────────────────────────────────


class TestSnapshotPath:
    def test_accepts_generated_id_and_stays_inside_root(self, tmp_path) -> None:
        sid = deps_api.new_snapshot_id()
        path = snapshot_path(sid)
        assert path.name == f"{sid}.json"
        assert path.parent.resolve() == (tmp_path / deps_api.SNAPSHOT_DIRNAME).resolve()

    @pytest.mark.parametrize(
        "bad",
        [
            "",
            "  ",
            "../etc/passwd",
            "../../secret",
            "a/b",
            "a\\b",
            "20250101T000000000Z-abcdef.json",
            "20250101T000000000Z",  # 缺随机后缀（引擎一定会带）
            "..",
            ".env",
            "20250101T000000000Z-abcde",  # 后缀位数不对
            "2025010T000000000Z-abcdef",  # 日期位数不对
            "20250101T000000000Z-ABCDEF",  # 大小写不符
        ],
    )
    def test_rejects_non_whitelisted_ids(self, bad: str) -> None:
        with pytest.raises(ValueError):
            snapshot_path(bad)

    def test_realpath_check_is_a_second_gate(self, monkeypatch, tmp_path) -> None:
        """即便白名单被放宽到"含斜杠"，realpath 也必须挡住 `../`。

        这条用例的存在意义：白名单是**人写的正则**，改坏它是很容易的事；只要有一条
        路径解析后的前缀校验，穿越就仍然出不去。
        """
        monkeypatch.setattr(deps_api, "SNAPSHOT_ID_RE", __import__("re").compile(r"^[A-Za-z0-9._/\\-]{1,64}$"))
        assert snapshot_path("normal-id").name == "normal-id.json"
        with pytest.raises(ValueError):
            snapshot_path("../escape")


# ── 创建与列表 ───────────────────────────────────────────────────────────


async def test_create_snapshot_writes_file_and_meta(monkeypatch, tmp_path) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        assert args[:2] == ["list", "--format=json"]
        return _pip_ok(CURRENT)

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots", json={"label": "  before   upgrade "})

    assert resp.status_code == 200
    meta = resp.json()["snapshot"]
    assert meta["trigger"] == "manual"
    assert meta["label"] == "before upgrade"  # 空白被压平
    assert meta["package_count"] == 2
    assert meta["created_at"] > 0

    path = tmp_path / deps_api.SNAPSHOT_DIRNAME / f"{meta['id']}.json"
    assert path.is_file(), "快照必须落在 <data_dir>/deps-snapshots/<id>.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["packages"] == CURRENT
    assert doc["created_at"] == meta["created_at"]
    assert doc["id"] == meta["id"]


async def test_install_creates_snapshot_before_pip(monkeypatch, tmp_path) -> None:
    """install 之前必须已有快照，且 trigger 记录了这次动作。"""
    calls: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        calls.append(args)
        if args[0] == "list":
            return _pip_ok(CURRENT)
        return deps_api.PipResult(ok=True, returncode=0, output="Successfully installed\n")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/install", json={"spec": "requests==2.31.0"})
        job_id = resp.json()["job"]["id"]
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{job_id}")
            if polled.json()["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.01)

    assert polled.json()["status"] == "succeeded"
    assert calls[0][0] == "list", "快照必须在 pip install 之前采集"

    async with _client() as client:
        listed = await client.get(f"{BASE}/snapshots")
    snapshots = listed.json()["snapshots"]
    assert len(snapshots) == 1
    assert snapshots[0]["trigger"] == "install:requests==2.31.0"
    assert snapshots[0]["package_count"] == len(CURRENT)


async def test_list_is_newest_first_and_skips_broken_files(tmp_path) -> None:
    first = deps_api.write_snapshot(CURRENT, trigger="manual", label="old", now=1_700_000_000.001)
    second = deps_api.write_snapshot(CURRENT, trigger="manual", label="new", now=1_700_000_100.002)
    broken = tmp_path / deps_api.SNAPSHOT_DIRNAME / "20200101T000000000Z-abcdef.json"
    broken.write_text("{not json", encoding="utf-8")

    async with _client() as client:
        resp = await client.get(f"{BASE}/snapshots")

    body = resp.json()
    assert resp.status_code == 200
    assert [item["id"] for item in body["snapshots"]] == [second["id"], first["id"]]
    assert body["count"] == 2
    assert body["retention"] == MAX_SNAPSHOTS


async def test_retention_keeps_only_latest(monkeypatch, tmp_path) -> None:
    for index in range(MAX_SNAPSHOTS + 3):
        deps_api.write_snapshot(CURRENT, trigger="manual", label=f"#{index}", now=1_700_000_000 + index)

    files = sorted((tmp_path / deps_api.SNAPSHOT_DIRNAME).glob("*.json"))
    assert len(files) == MAX_SNAPSHOTS, "只保留最近 20 份"

    async with _client() as client:
        body = (await client.get(f"{BASE}/snapshots")).json()
    labels = [item["label"] for item in body["snapshots"]]
    assert "#0" not in labels and "#2" not in labels
    assert f"#{MAX_SNAPSHOTS + 2}" in labels


def test_prune_ignores_foreign_files(tmp_path) -> None:
    root = tmp_path / deps_api.SNAPSHOT_DIRNAME
    root.mkdir(parents=True)
    keep = root / "notes.json"
    keep.write_text("{}", encoding="utf-8")
    for index in range(MAX_SNAPSHOTS + 1):
        deps_api.write_snapshot(CURRENT, trigger="manual", now=1_700_000_000 + index)

    assert keep.is_file(), "清理只能删自己生成的快照，不能碰同目录的其它文件"


async def test_create_snapshot_reports_pip_failure(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return deps_api.PipResult(ok=False, returncode=2, output="boom")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots", json={})
    assert resp.status_code == 502
    assert "exited with code 2" in json.dumps(resp.json())


# ── 差异算法 ─────────────────────────────────────────────────────────────


class TestComputeRestoreDiff:
    def test_classifies_extra_missing_and_changed(self) -> None:
        snapshot = [
            {"name": "Requests", "version": "2.31.0"},
            {"name": "fastapi", "version": "0.115.0"},
            {"name": "pillow", "version": "10.0.0"},
        ]
        current = [
            {"name": "requests", "version": "2.32.0"},
            {"name": "extra-pkg", "version": "1.0"},
            {"name": "pillow", "version": "10.0.0"},
        ]
        diff = compute_restore_diff(snapshot, current)

        # 大小写不敏感匹配：requests / Requests 是同一个包
        assert diff["changed"] == [{"name": "requests", "from": "2.32.0", "to": "2.31.0"}]
        assert diff["missing"] == [{"name": "fastapi", "version": "0.115.0"}]
        assert diff["extra"] == [{"name": "extra-pkg", "version": "1.0"}]
        assert diff["unchanged"] == 1  # pillow
        assert diff["install_count"] == 2  # missing + changed
        assert diff["uninstall_count"] == 1
        assert diff["total_changes"] == 3
        assert diff["exceeds_limit"] is False

    def test_identical_environment_has_no_changes(self) -> None:
        diff = compute_restore_diff(CURRENT, list(CURRENT))
        assert diff["total_changes"] == 0
        assert diff["unchanged"] == len(CURRENT)

    def test_exceeds_limit_flag(self) -> None:
        snapshot = [{"name": f"pkg{i}", "version": "1.0"} for i in range(MAX_RESTORE_INSTALLS + 1)]
        diff = compute_restore_diff(snapshot, [])
        assert diff["install_count"] == MAX_RESTORE_INSTALLS + 1
        assert diff["exceeds_limit"] is True

    def test_uninstall_only_is_not_counted_against_limit(self) -> None:
        current = [{"name": f"pkg{i}", "version": "1.0"} for i in range(MAX_RESTORE_INSTALLS + 5)]
        diff = compute_restore_diff([], current)
        assert diff["install_count"] == 0
        assert diff["uninstall_count"] == len(current)
        assert diff["exceeds_limit"] is False


# ── 恢复 ─────────────────────────────────────────────────────────────────


async def test_restore_dry_run_returns_diff_without_job(monkeypatch, tmp_path) -> None:
    called: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        called.append(args)
        return _pip_ok(CURRENT)

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)
    snapshot = deps_api.write_snapshot(
        [{"name": "fastapi", "version": "0.115.0"}, {"name": "requests", "version": "2.31.0"}],
        trigger="manual",
    )

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/{snapshot['id']}/restore", json={"dry_run": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert body["job"] is None
    assert body["diff"]["changed"] == [{"name": "requests", "from": "2.32.0", "to": "2.31.0"}]
    # 预览只读，唯一的上游动作是 pip list
    assert called and all(args[0] == "list" for args in called)
    assert deps_api._jobs == {}


async def test_restore_uninstalls_extras_and_pins_versions(monkeypatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        calls.append(args)
        if args[0] == "list":
            return _pip_ok(CURRENT + [{"name": "extra-pkg", "version": "1.0"}])
        return deps_api.PipResult(ok=True, returncode=0, output="ok\n")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)
    snapshot = deps_api.write_snapshot(
        [
            {"name": "fastapi", "version": "0.115.0"},
            {"name": "requests", "version": "2.31.0"},
            {"name": "pillow", "version": "10.0.0"},
        ],
        trigger="manual",
    )

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/{snapshot['id']}/restore", json={"dry_run": False})
        assert resp.status_code == 200
        job_id = resp.json()["job"]["id"]
        assert resp.json()["job"]["kind"] == "restore"
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{job_id}")
            if polled.json()["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.01)

    assert polled.json()["status"] == "succeeded"
    uninstall = [args for args in calls if args[0] == "uninstall"]
    install = [args for args in calls if args[0] == "install"]
    assert uninstall and uninstall[0][1:] == [
        "--yes",
        "--no-input",
        "--disable-pip-version-check",
        "extra-pkg",
    ], "多出的包必须被卸载"
    assert install and install[0][1:] == [
        "--no-input",
        "--disable-pip-version-check",
        "pillow==10.0.0",
        "requests==2.31.0",
    ], "缺失与版本变化都必须按快照版本钉住"

    # 恢复前也留了一份快照（rollback of rollback），trigger 指明来源
    snapshots = (await _list_snapshots())
    triggers = [item["trigger"] for item in snapshots]
    assert f"restore:{snapshot['id']}" in triggers


async def _list_snapshots() -> list[dict[str, Any]]:
    async with _client() as client:
        return (await client.get(f"{BASE}/snapshots")).json()["snapshots"]


async def test_restore_is_noop_when_environment_already_matches(monkeypatch) -> None:
    snapshot = deps_api.write_snapshot(CURRENT, trigger="manual")

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return _pip_ok(CURRENT)

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/{snapshot['id']}/restore", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert body["job"] is None
    assert body.get("noop") is True
    assert deps_api._jobs == {}


async def test_restore_rejects_unknown_snapshot() -> None:
    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/20200101T000000000Z-abcdef/restore", json={})
    assert resp.status_code == 404


async def test_restore_rejects_malformed_id(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        raise AssertionError("非法 id 不该触达 pip")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/..%2F..%2Fsecret/restore", json={})
    assert resp.status_code in (400, 404)


async def test_restore_rejects_tampered_entries(monkeypatch, tmp_path) -> None:
    """快照文件在磁盘上：里面的条目也必须重新过规格白名单。"""
    snapshot = deps_api.write_snapshot(CURRENT, trigger="manual")
    path = tmp_path / deps_api.SNAPSHOT_DIRNAME / f"{snapshot['id']}.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["packages"] = [{"name": "-r", "version": ".env"}]
    path.write_text(json.dumps(doc), encoding="utf-8")

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        raise AssertionError("非法条目不该触达 pip")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/{snapshot['id']}/restore", json={"dry_run": False})
    assert resp.status_code == 400
    assert deps_api._jobs == {}


async def test_restore_rejects_more_than_limit(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return _pip_ok([])

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)
    snapshot = deps_api.write_snapshot(
        [{"name": f"pkg{i}", "version": "1.0"} for i in range(MAX_RESTORE_INSTALLS + 1)],
        trigger="manual",
    )

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/{snapshot['id']}/restore", json={"dry_run": False})
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "max" in detail["error"]
    assert detail["diff"]["install_count"] == MAX_RESTORE_INSTALLS + 1
    assert deps_api._jobs == {}


async def test_restore_respects_single_task_gate(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return _pip_ok(CURRENT)

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)
    snapshot = deps_api.write_snapshot([{"name": "missing-pkg", "version": "1.0"}], trigger="manual")
    monkeypatch.setattr(deps_api, "_running_job_id", "busy-job")

    async with _client() as client:
        resp = await client.post(f"{BASE}/snapshots/{snapshot['id']}/restore", json={"dry_run": False})
    assert resp.status_code == 409
    assert resp.json()["detail"]["job_id"] == "busy-job"
