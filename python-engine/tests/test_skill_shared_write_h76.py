"""H76（剩余半）：租户共享层写入需 tenant-admin；成员修改只产生私有 override。

缺陷形态：
  `PATCH /v1/skills/{name}/meta` 走 `store.update_meta()`，而它把元数据**写回技能原本
  所在的载体**。对来自租户共享层（`base/{tid}/_shared/`）的技能，那就是**就地改共享
  文件**：任何成员点一下 auto_inject 开关，全租户看到的那一份都变了；
  全局共享层（`base/_shared/`，连 tenant 边界都没有）同理。

修法：
  · admin/owner → 允许就地更新共享层；
  · 普通成员 + 租户共享技能 → 写**私有 override**（共享文件字节不变）；
  · 普通成员 + 全局共享技能 → 403（它不属于任何租户，成员无从"覆盖"）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.skills import router
from app.skill import store as store_mod
from app.skill.store import SCOPE_SHARED, SCOPE_USER, SkillDef, SkillStore


@pytest.fixture()
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    # 所有 store 都落到临时目录：`resolve_skill_roots` 从 SKILL_STORE_PATH 取 base，
    # API 层的 `_store_for` 也走同一条路径，所以设环境变量即可全局生效。
    monkeypatch.setenv("SKILL_STORE_PATH", str(root))
    return root


def _tenant_shared_skill(base: Path, tenant: int, name: str) -> Path:
    """在租户共享层放一份技能，返回文件路径。"""
    shared = base / str(tenant) / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    path = shared / f"{name}.skill.json"
    path.write_text(
        json.dumps(
            {
                "name": name,
                "description": "团队共享技能",
                "version": "1.0.0",
                "exec_type": "prompt",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _global_shared_skill(base: Path, name: str) -> Path:
    shared = base / "_shared"
    shared.mkdir(parents=True, exist_ok=True)
    path = shared / f"{name}.skill.json"
    path.write_text(
        json.dumps({"name": name, "description": "全局共享", "version": "1.0.0"}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _store(tenant: int, user: str) -> SkillStore:
    return SkillStore(tenant_id=tenant, user_id=user)


def test_member_edit_of_tenant_shared_skill_writes_private_override(base: Path) -> None:
    shared = _tenant_shared_skill(base, 1, "team-lint")
    before = shared.read_bytes()

    store = _store(1, "u-member")
    assert store.get("team-lint") is not None
    assert store.get("team-lint").scope == store_mod.SCOPE_TENANT

    updated = store.write_private_override("team-lint", {"auto_inject": False})
    assert updated is not None

    # 共享层字节不变 —— 这正是本条的核心
    assert shared.read_bytes() == before, "成员修改不得就地改写共享层"

    private = base / "1" / "u-member" / "team-lint.skill.json"
    assert private.is_file(), "应生成私有 override 文件"
    payload = json.loads(private.read_text(encoding="utf-8"))
    assert payload["auto_inject"] is False
    assert payload["override_of"] == store_mod.SCOPE_TENANT
    assert payload["scope"] == SCOPE_USER


def test_admin_can_still_update_shared_skill_in_place(base: Path) -> None:
    shared = _tenant_shared_skill(base, 1, "team-audit")
    store = _store(1, "u-admin")
    store.update_meta("team-audit", auto_inject=False)
    payload = json.loads(shared.read_text(encoding="utf-8"))
    assert payload["auto_inject"] is False, "管理员就地更新共享层是允许的"


def test_patch_meta_returns_override_flag_for_member(base: Path) -> None:
    shared = _tenant_shared_skill(base, 1, "team-build")
    before = shared.read_bytes()
    client = _client()

    r = client.patch(
        "/v1/skills/team-build/meta",
        json={"auto_inject": False},
        params={"user_id": "u-member", "tenant_id": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["override"] is True
    assert shared.read_bytes() == before


def test_patch_meta_admin_updates_in_place(base: Path) -> None:
    shared = _tenant_shared_skill(base, 1, "team-doc")
    client = _client()

    r = client.patch(
        "/v1/skills/team-doc/meta",
        json={"auto_inject": False},
        headers={"X-User-Role": "admin"},
        params={"user_id": "u-admin", "tenant_id": 1},
    )
    assert r.status_code == 200, r.text
    assert r.json()["override"] is False
    assert json.loads(shared.read_text(encoding="utf-8"))["auto_inject"] is False


def test_member_cannot_modify_global_shared_skill(base: Path) -> None:
    shared = _global_shared_skill(base, "global-style")
    before = shared.read_bytes()
    client = _client()

    r = client.patch(
        "/v1/skills/global-style/meta",
        json={"auto_inject": True},
        params={"user_id": "u-member", "tenant_id": 1},
    )
    assert r.status_code == 403, r.text
    assert shared.read_bytes() == before


def test_missing_role_header_is_treated_as_member(base: Path) -> None:
    """角色头缺失时**不得**放行共享层写入（缺省即非管理员）。"""
    shared = _tenant_shared_skill(base, 1, "team-nohdr")
    before = shared.read_bytes()
    client = _client()

    r = client.patch(
        "/v1/skills/team-nohdr/meta",
        json={"auto_inject": False},
        params={"user_id": "u-x", "tenant_id": 1},
    )
    assert r.status_code == 200
    assert r.json()["override"] is True
    assert shared.read_bytes() == before


def test_private_skill_update_stays_in_place(base: Path) -> None:
    """私有技能照旧就地改（不要把所有人都推去生成 override）。"""
    store = _store(1, "u-own")
    store.save(SkillDef(name="mine", description="私有", exec_type="prompt"))
    updated = store.update_meta("mine", auto_inject=False)
    assert updated is not None
    assert updated.scope in (SCOPE_USER, store_mod.SCOPE_ROOT)


def test_override_of_only_appears_on_overrides(base: Path) -> None:
    _tenant_shared_skill(base, 1, "team-x")
    store = _store(1, "u-y")
    store.write_private_override("team-x", {"auto_inject": False})
    private = json.loads((base / "1" / "u-y" / "team-x.skill.json").read_text(encoding="utf-8"))
    assert private.get("override_of") == store_mod.SCOPE_TENANT
    # 共享层那份不带 override_of
    assert "override_of" not in json.loads(
        (base / "1" / "_shared" / "team-x.skill.json").read_text(encoding="utf-8")
    )


def test_global_scope_constant_is_distinct() -> None:
    assert SCOPE_SHARED != SCOPE_USER
