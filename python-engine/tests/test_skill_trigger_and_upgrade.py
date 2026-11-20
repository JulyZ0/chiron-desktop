"""P1-2：技能触发策略（auto_inject）、来源（origin）与版本升级。

锁死四类行为：

* **注入过滤**：`auto_inject=false` 的技能不进每轮 system 上下文，但被显式 @（selected）
  时仍进 —— 否则"关掉开关"要么等于"这个技能彻底不存在"，要么等于"开关没用"。
* **元数据落盘**：`auto_inject` / `origin` / `version` 写回**技能自己的载体**
  （目录型技能写同目录 `.skill.json` 旁挂文件），而不是在写目录里造一份覆盖 ——
  覆盖会让列表出现两份定义。
* **版本比较**：数字段按数值比（1.10 > 1.9），相同/更旧算"已最新"，不做任何写入。
* **原子升级**：升级成功才替换文件；取清单失败 / 写入失败时**旧版本完好**。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.skill.store import (
    ORIGIN_GENERATED,
    ORIGIN_LOCAL,
    SkillDef,
    SkillStore,
    market_origin,
    parse_market_origin,
)
from app.skill.upgrade import (
    REASON_ALREADY_LATEST,
    REASON_FETCH_FAILED,
    REASON_NO_SOURCE,
    REASON_NOT_MARKET,
    compare_versions,
    is_newer,
    pick_definition,
    upgrade_skill,
    version_key,
)
from app.tools import skill as skill_tool
from app.tools.registry import registry
from app.tools.skill_catalog import build_skill_catalog

# ── 注入过滤（skill catalog）────────────────────────────────────────────


def _patch_skill_list(monkeypatch: pytest.MonkeyPatch, skills: list[dict[str, Any]]) -> None:
    payload = {"skills": skills, "load_warnings": []}

    async def fake_handler(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(registry, "get", lambda name: type("T", (), {"handler": fake_handler})())


TWO_SKILLS = [
    {"name": "always-on", "description": "每轮注入", "auto_inject": True,
     "exec": {"type": "prompt", "source": "always body"}},
    {"name": "on-demand", "description": "仅在 @ 时注入", "auto_inject": False,
     "exec": {"type": "prompt", "source": "on demand body"}},
]


async def test_catalog_skips_auto_inject_off(monkeypatch):
    _patch_skill_list(monkeypatch, TWO_SKILLS)

    catalog = await build_skill_catalog()

    assert "always-on" in catalog
    assert "on-demand" not in catalog, "auto_inject=false 的技能不该进每轮 system 上下文"


async def test_catalog_keeps_auto_inject_off_when_selected(monkeypatch):
    """显式 @（selected）时必须照常注入 —— 这正是"关掉自动注入"的可用性前提。"""
    _patch_skill_list(monkeypatch, TWO_SKILLS)

    catalog = await build_skill_catalog(selected=["on-demand"], with_bodies=True)

    assert "on-demand" in catalog
    assert "on demand body" in catalog, "显式选中的技能连正文都要注入"
    assert "always-on" not in catalog, "给了 selected 就只列选中的"


async def test_catalog_legacy_rows_default_to_auto_inject(monkeypatch):
    """老数据没有 auto_inject 字段 ⇒ 视为 true（不能因为字段缺失就把技能藏起来）。"""
    _patch_skill_list(
        monkeypatch, [{"name": "legacy", "description": "老技能", "exec": {"type": "prompt"}}]
    )

    catalog = await build_skill_catalog()

    assert "legacy" in catalog


# ── 元数据落盘 ──────────────────────────────────────────────────────────


def test_to_dict_exposes_new_fields():
    payload = SkillDef(name="x", description="d").to_dict()

    assert payload["auto_inject"] is True, "缺省必须为 true"
    assert payload["origin"] == "", "老数据缺省视为空（展示层按本地处理）"


def test_store_roundtrip_new_fields(tmp_path):
    store = SkillStore(root=tmp_path)
    store.save(
        SkillDef(
            name="m1",
            description="d",
            version="2.0.0",
            auto_inject=False,
            origin=market_origin("builtin", "skill:demo"),
        )
    )

    reloaded = SkillStore(root=tmp_path).get("m1")

    assert reloaded is not None
    assert reloaded.auto_inject is False
    assert reloaded.origin == "market:builtin/skill:demo"
    assert reloaded.version == "2.0.0"


def test_update_meta_updates_flat_carrier_in_place(tmp_path):
    store = SkillStore(root=tmp_path)
    store.save(SkillDef(name="flat", description="d", origin=ORIGIN_LOCAL))

    store.update_meta("flat", auto_inject=False)

    # 只改元数据 ⇒ 不新增覆盖文件，改的就是它自己那份
    assert [p.name for p in tmp_path.glob("*.skill.json")] == ["flat.skill.json"]
    on_disk = json.loads((tmp_path / "flat.skill.json").read_text(encoding="utf-8"))
    assert on_disk["auto_inject"] is False
    assert on_disk["description"] == "d", "元数据更新不能丢掉已有字段"
    assert SkillStore(root=tmp_path).get("flat").auto_inject is False


def test_update_meta_writes_sidecar_for_directory_skill(tmp_path):
    skill_dir = tmp_path / "doc-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: doc-skill\ndescription: 目录型技能\n---\n\nbody\n", encoding="utf-8"
    )
    store = SkillStore(root=tmp_path)

    store.update_meta("doc-skill", auto_inject=False, origin=ORIGIN_GENERATED)

    sidecar = skill_dir / ".skill.json"
    assert sidecar.is_file(), "目录型技能的元数据写同目录旁挂文件"
    # 旁挂文件不能变成"第二份技能定义"
    names = [s.name for s in SkillStore(root=tmp_path).list()]
    assert names == ["doc-skill"]
    reloaded = SkillStore(root=tmp_path).get("doc-skill")
    assert reloaded.auto_inject is False
    assert reloaded.origin == ORIGIN_GENERATED


def test_update_meta_missing_skill_returns_none(tmp_path):
    assert SkillStore(root=tmp_path).update_meta("ghost", auto_inject=False) is None


def test_market_origin_helpers():
    assert market_origin("builtin", "skill:x") == "market:builtin/skill:x"
    assert parse_market_origin("market:team/skill:a/b") == ("team", "skill:a/b")
    assert parse_market_origin("local") is None
    assert parse_market_origin("") is None


# ── 版本比较 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "want"),
    [
        ("1.0.0", "1.0.0", 0),
        ("1.10.0", "1.9.2", 1),
        ("1.9.2", "1.10.0", -1),
        ("v2.0", "1.9.9", 1),
        ("1.0.0-beta", "1.0.0", -1),
        ("", "0.1.0", -1),
    ],
)
def test_compare_versions(a, b, want):
    assert compare_versions(a, b) == want


def test_version_key_is_monotonic_for_numeric_segments():
    assert version_key("1.10") > version_key("1.9")
    assert is_newer("1.10.0", "1.9.0") is True
    assert is_newer("1.9.0", "1.9.0") is False, "相同版本不算更新"
    assert is_newer("1.8.0", "1.9.0") is False


def test_pick_definition_from_single_and_catalog():
    assert pick_definition({"name": "a"}, "a") == {"name": "a"}
    catalog = [{"name": "a", "version": "1"}, {"name": "b", "version": "2"}]
    assert pick_definition(catalog, "b") == {"name": "b", "version": "2"}
    assert pick_definition(catalog, "c") is None


# ── 升级：原子覆盖 + 失败不破坏旧版本 ────────────────────────────────────


def _market_store(tmp_path: Path) -> SkillStore:
    store = SkillStore(root=tmp_path)
    store.save(
        SkillDef(
            name="m_skill",
            description="市场技能 v1",
            version="1.0.0",
            exec_type="prompt",
            source="old body",
            origin=market_origin("builtin", "skill:m"),
            auto_inject=False,
            enabled=True,
            metadata={"source_url": "https://catalog.test/skills.json"},
        )
    )
    return store


def _remote(version: str, *, name: str = "m_skill") -> dict[str, Any]:
    return {
        "name": name,
        "description": f"市场技能 v{version}",
        "version": version,
        "exec": {"type": "prompt", "source": f"body {version}"},
    }


async def _with_fetch(monkeypatch, payload: Any):
    async def fake_fetch(_url: str) -> Any:
        return payload

    monkeypatch.setattr(skill_tool, "_fetch_skill_json", fake_fetch)


async def test_upgrade_success_replaces_definition(tmp_path, monkeypatch):
    store = _market_store(tmp_path)
    await _with_fetch(monkeypatch, _remote("1.2.0"))

    outcome = await upgrade_skill(store, "m_skill")

    assert outcome.ok and outcome.updated
    assert (outcome.current, outcome.latest) == ("1.0.0", "1.2.0")
    reloaded = SkillStore(root=tmp_path).get("m_skill")
    assert reloaded.version == "1.2.0"
    assert reloaded.exec_type == "prompt" and reloaded.source == "body 1.2.0"
    # 用户侧状态与来源必须保留，否则升级后就再也升不了级 / 用户开关被重置
    assert reloaded.origin == "market:builtin/skill:m"
    assert reloaded.auto_inject is False
    assert reloaded.enabled is True


async def test_upgrade_already_latest_does_not_touch_files(tmp_path, monkeypatch):
    store = _market_store(tmp_path)
    before = (tmp_path / "m_skill.skill.json").read_text(encoding="utf-8")
    await _with_fetch(monkeypatch, _remote("1.0.0"))

    outcome = await upgrade_skill(store, "m_skill")

    assert outcome.ok and not outcome.updated
    assert outcome.reason == REASON_ALREADY_LATEST
    assert (tmp_path / "m_skill.skill.json").read_text(encoding="utf-8") == before


async def test_upgrade_rejects_non_market_origin(tmp_path):
    store = SkillStore(root=tmp_path)
    store.save(SkillDef(name="local_one", description="d", origin=ORIGIN_LOCAL))

    outcome = await upgrade_skill(store, "local_one", source_url="https://catalog.test/x.json")

    assert not outcome.ok
    assert outcome.reason == REASON_NOT_MARKET


async def test_upgrade_without_source_is_explicit(tmp_path):
    store = SkillStore(root=tmp_path)
    store.save(
        SkillDef(name="m2", description="d", origin=market_origin("builtin", "skill:m2"))
    )

    outcome = await upgrade_skill(store, "m2")

    assert not outcome.ok
    assert outcome.reason == REASON_NO_SOURCE


async def test_upgrade_fetch_failure_keeps_old_version(tmp_path, monkeypatch):
    store = _market_store(tmp_path)
    before = (tmp_path / "m_skill.skill.json").read_text(encoding="utf-8")

    async def boom(_url: str) -> Any:
        raise RuntimeError("network down")

    monkeypatch.setattr(skill_tool, "_fetch_skill_json", boom)

    outcome = await upgrade_skill(store, "m_skill")

    assert not outcome.ok
    assert outcome.reason == REASON_FETCH_FAILED
    assert (tmp_path / "m_skill.skill.json").read_text(encoding="utf-8") == before
    assert SkillStore(root=tmp_path).get("m_skill").version == "1.0.0"


async def test_upgrade_write_failure_keeps_old_version(tmp_path, monkeypatch):
    """原子替换失败（这里模拟 `os.replace` 抛错）⇒ 旧文件必须完好、临时文件不残留。"""
    import app.skill.store as store_mod

    store = _market_store(tmp_path)
    before = (tmp_path / "m_skill.skill.json").read_text(encoding="utf-8")
    await _with_fetch(monkeypatch, _remote("3.0.0"))

    def boom(_src, _dst):  # noqa: ANN001
        raise OSError("disk full")

    monkeypatch.setattr(store_mod.os, "replace", boom)

    outcome = await upgrade_skill(store, "m_skill")

    assert not outcome.ok
    assert (tmp_path / "m_skill.skill.json").read_text(encoding="utf-8") == before
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == [], f"失败后不该留下临时文件：{leftovers}"


async def test_upgrade_rejects_name_mismatch(tmp_path, monkeypatch):
    store = _market_store(tmp_path)
    await _with_fetch(monkeypatch, _remote("9.0.0", name="someone_else"))

    outcome = await upgrade_skill(store, "m_skill")

    assert not outcome.ok
    assert SkillStore(root=tmp_path).get("m_skill").version == "1.0.0"


# ── API 接线：PATCH meta + POST upgrade ─────────────────────────────────


@pytest.fixture()
def skill_root(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))
    return tmp_path


async def _client():
    from app.main import create_app

    return httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    )


async def test_api_patch_meta_and_list(skill_root):
    SkillStore(tenant_id="1", user_id="u1").save(
        SkillDef(name="api_skill", description="d", origin=ORIGIN_LOCAL)
    )
    params = {"user_id": "u1", "tenant_id": "1"}

    async with await _client() as client:
        patched = await client.patch(
            "/v1/skills/api_skill/meta", json={"auto_inject": False}, params=params
        )
        listed = await client.get("/v1/skills", params=params)

    assert patched.status_code == 200
    assert patched.json()["skill"]["auto_inject"] is False
    entry = [s for s in listed.json()["skills"] if s["name"] == "api_skill"][0]
    assert entry["auto_inject"] is False
    assert entry["origin"] == ORIGIN_LOCAL, "清单 API 必须返回 origin"


async def test_api_upgrade_conflict_for_non_market(skill_root):
    SkillStore(tenant_id="1", user_id="u1").save(
        SkillDef(name="local_api", description="d", origin=ORIGIN_LOCAL)
    )

    async with await _client() as client:
        resp = await client.post(
            "/v1/skills/local_api/upgrade",
            json={},
            params={"user_id": "u1", "tenant_id": "1"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["reason"] == REASON_NOT_MARKET


async def test_api_upgrade_already_latest_is_200(skill_root, monkeypatch):
    SkillStore(tenant_id="1", user_id="u1").save(
        SkillDef(
            name="mkt_api",
            description="d",
            version="1.1.0",
            origin=market_origin("builtin", "skill:api"),
        )
    )
    await _with_fetch(monkeypatch, _remote("1.1.0", name="mkt_api"))

    async with await _client() as client:
        resp = await client.post(
            "/v1/skills/mkt_api/upgrade",
            json={"source_url": "https://catalog.test/skills.json"},
            params={"user_id": "u1", "tenant_id": "1"},
        )

    body = resp.json()
    assert resp.status_code == 200
    assert body["upgraded"] is False
    assert body["reason"] == REASON_ALREADY_LATEST


async def test_api_install_records_origin(skill_root):
    inline = json.dumps(
        {"name": "from_market", "description": "d", "version": "0.3.0",
         "exec": {"type": "prompt", "source": "b"}}
    )

    async with await _client() as client:
        resp = await client.post(
            "/v1/skills/install",
            json={"inline": inline, "origin": "market:builtin/skill:fm"},
            params={"user_id": "u1", "tenant_id": "1"},
        )

    assert resp.status_code == 200
    assert resp.json()["skill"]["origin"] == "market:builtin/skill:fm"
    assert resp.json()["skill"]["auto_inject"] is True
