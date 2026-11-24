from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.skills import SkillInstallRequest, install_skill
from app.skill.store import SkillDef, SkillStore, validate_skill_name

INVALID_NAMES = [
    "",
    ".",
    "..",
    ".hidden",
    "-leading",
    "../escape",
    "..\\escape",
    "nested/name",
    "nested\\name",
    "/absolute",
    "C:\\absolute",
    "\\\\server\\share",
    "has space",
    "name:stream",
    "a" * 65,
]


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_rejects_non_filename_skill_names(name: str):
    with pytest.raises(ValueError, match="invalid skill name"):
        validate_skill_name(name)


@pytest.mark.parametrize("name", ["a", "skill_name", "skill-name", "skill.name", "_private", "a" * 64])
def test_accepts_safe_skill_names(name: str):
    assert validate_skill_name(name) == name


@pytest.mark.parametrize("name", INVALID_NAMES)
def test_store_write_and_delete_paths_reject_traversal(tmp_path: Path, name: str):
    store = SkillStore(root=tmp_path / "skills")
    with pytest.raises(ValueError, match="invalid skill name"):
        store.save(SkillDef(name=name, description="bad"))
    with pytest.raises(ValueError, match="invalid skill name"):
        store.save_skill_md(name, "body")
    with pytest.raises(ValueError, match="invalid skill name"):
        store.delete(name)
    assert store.delete_skill_dir(name) is False
    assert list(tmp_path.iterdir()) == [tmp_path / "skills"]


def test_symlink_cannot_redirect_flat_or_directory_writes(tmp_path: Path):
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    try:
        (root / "escape.skill.json").symlink_to(outside / "escape.skill.json")
        (root / "escape-dir").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    store = SkillStore(root=root)
    with pytest.raises(ValueError, match="escapes store"):
        store.save(SkillDef(name="escape", description="bad"))
    with pytest.raises(ValueError, match="escapes store"):
        store.save_skill_md("escape-dir", "bad")
    assert list(outside.iterdir()) == []


def test_skill_md_symlink_cannot_redirect_write(tmp_path: Path):
    root = tmp_path / "skills"
    target = root / "escape"
    outside = tmp_path / "outside.md"
    target.mkdir(parents=True)
    outside.write_text("outside", encoding="utf-8")
    try:
        (target / "SKILL.md").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    store = SkillStore(root=root)
    with pytest.raises(ValueError, match="escapes store"):
        store.save_skill_md("escape", "bad")
    assert outside.read_text(encoding="utf-8") == "outside"


async def test_install_api_returns_400_and_writes_nothing_outside_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))
    payload = {"name": "../escaped", "description": "bad", "exec": {"type": "prompt", "source": "x"}}

    with pytest.raises(HTTPException) as exc:
        await install_skill(
            SkillInstallRequest(inline=json.dumps(payload)), user_id="u1", tenant_id=1
        )

    assert exc.value.status_code == 400
    assert not (tmp_path / "escaped.skill.json").exists()


async def test_install_api_still_accepts_valid_skill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))
    payload = {"name": "safe_skill", "description": "ok", "exec": {"type": "prompt", "source": "x"}}

    result = await install_skill(
        SkillInstallRequest(inline=json.dumps(payload)), user_id="u1", tenant_id=1
    )

    assert result["skill"]["name"] == "safe_skill"
    assert (tmp_path / "1" / "u1" / "safe_skill.skill.json").is_file()
