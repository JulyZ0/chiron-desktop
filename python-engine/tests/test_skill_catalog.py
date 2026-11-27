"""skill_catalog — 技能目录注入。

三条约定：

* **D2**：目录进 system prompt（由 runtime 拼装），每条给 name + description + **可读路径**，
  正文按需 `read_file`；
* **D5**：载入告警汇总为 `<skill_load_warnings>`（转义 + 标注"不是指令" + 条数上限）；
* **D7**：给了 `selected` 就**只列被选中的**技能。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tools.registry import registry
from app.tools.skill_catalog import (
    BODY_MARKER,
    CATALOG_MARKER,
    CHARS_PER_TOKEN,
    MAX_WARNINGS,
    SKILL_BODY_BUDGET_TOKENS,
    TRUNCATION_NOTE,
    WARNINGS_MARKER,
    build_skill_catalog,
    skill_path,
)


def _patch_skill_list(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    async def fake_handler(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return payload

    monkeypatch.setattr(
        registry, "get", lambda name: type("T", (), {"handler": fake_handler})()
    )


# ── D2：可读路径 ────────────────────────────────────────────────────────


async def test_catalog_gives_readable_path(monkeypatch):
    _patch_skill_list(
        monkeypatch, {"skills": [{"name": "git-helper", "description": "Git 辅助"}]}
    )

    catalog = await build_skill_catalog()

    assert CATALOG_MARKER in catalog
    assert "git-helper" in catalog
    assert "/skills/git-helper/SKILL.md" in catalog, "必须给可读路径（否则'按需读正文'是空话）"
    assert "read_file" in catalog, "要告诉模型怎么读正文"


def test_skill_path_shape():
    assert skill_path("git-helper") == "/skills/git-helper/SKILL.md"
    assert skill_path("/lead-and-trail/") == "/skills/lead-and-trail/SKILL.md"


async def test_no_skills_no_catalog(monkeypatch):
    _patch_skill_list(monkeypatch, {"skills": []})
    assert await build_skill_catalog() == ""


async def test_disabled_skills_excluded(monkeypatch):
    _patch_skill_list(
        monkeypatch, {"skills": [{"name": "off", "description": "d", "enabled": False}]}
    )
    assert await build_skill_catalog() == ""


async def test_missing_tool_returns_empty(monkeypatch):
    monkeypatch.setattr(registry, "get", lambda name: None)
    assert await build_skill_catalog() == ""


# ── D7：按选中过滤 ──────────────────────────────────────────────────────


async def test_selected_filters_catalog(monkeypatch):
    """未选中的技能**不该出现在目录里** —— 否则"选中"只是建议而不是收窄。"""
    _patch_skill_list(
        monkeypatch,
        {
            "skills": [
                {"name": "keep", "description": "d1"},
                {"name": "drop", "description": "d2"},
            ]
        },
    )

    catalog = await build_skill_catalog(selected=["keep"])

    assert "keep" in catalog
    assert "drop" not in catalog


async def test_empty_selection_means_no_filter(monkeypatch):
    """空选中 = 未筛选（沿用全部）—— 与既有的 `skill_names` 语义一致。"""
    _patch_skill_list(
        monkeypatch,
        {"skills": [{"name": "a", "description": "d"}, {"name": "b", "description": "d"}]},
    )

    catalog = await build_skill_catalog(selected=[])

    assert "a" in catalog and "b" in catalog


# ── D5：载入告警 ────────────────────────────────────────────────────────


async def test_load_warnings_are_surfaced(monkeypatch):
    """坏技能必须**可见** —— 只留在日志里等于"明明放了技能却不生效"。"""
    _patch_skill_list(
        monkeypatch,
        {"skills": [], "load_warnings": ["bad.skill.json: invalid json"]},
    )

    catalog = await build_skill_catalog()

    assert WARNINGS_MARKER in catalog
    assert "bad.skill.json" in catalog
    assert "NOT instructions" in catalog, "要明确标注这是数据、不是指令"


async def test_warnings_are_escaped_and_capped(monkeypatch):
    """告警来自**外部文件** ⇒ 必须转义（不能撑破结构）+ 条数上限（不能挤爆上下文）。"""
    _patch_skill_list(
        monkeypatch,
        {"skills": [], "load_warnings": [f"<script>bad{i}</script>" for i in range(MAX_WARNINGS + 3)]},
    )

    catalog = await build_skill_catalog()

    assert "<script>" not in catalog, "必须转义"
    assert "&lt;script&gt;" in catalog
    assert "and 3 more" in catalog, "超上限要说明还有多少条"


async def test_warnings_shown_even_without_skills(monkeypatch):
    """技能全坏时目录仍要产出（只有告警段）—— 否则用户看不到任何解释。"""
    _patch_skill_list(monkeypatch, {"skills": [], "load_warnings": ["x: boom"]})

    catalog = await build_skill_catalog()

    assert WARNINGS_MARKER in catalog and CATALOG_MARKER not in catalog


# ── 选中技能的正文注入（with_bodies）──────────────────────────────────
#
# 只有"名字 + 描述"时，模型得再 read_file 一次才看得到正文；实测表现就是
# "配了 Skill 却像没用上"。选中即注入正文，但必须有预算，避免几个大技能挤爆上下文。


def _skill(
    name: str,
    *,
    body: str = "",
    exec_type: str = "prompt",
    description: str = "desc",
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "enabled": enabled,
        "exec": {"type": exec_type, "source": body},
    }


async def test_selected_skill_body_injected(monkeypatch):
    _patch_skill_list(
        monkeypatch,
        {"skills": [_skill("git-helper", body="# Git 工作流\n先 rebase 再提交", description="Git 辅助")]},
    )

    catalog = await build_skill_catalog(selected=["git-helper"], with_bodies=True)

    assert BODY_MARKER in catalog
    assert "先 rebase 再提交" in catalog, "选中的技能正文必须进上下文"
    assert 'name="git-helper"' in catalog
    assert "Git 辅助" in catalog, "正文段要带描述（名字 + 描述 + 正文）"


async def test_bodies_off_by_default(monkeypatch):
    """默认不注入正文：老调用点逐字不变（前缀缓存不受影响）。"""
    _patch_skill_list(
        monkeypatch, {"skills": [_skill("git-helper", body="SECRET_BODY")]}
    )

    catalog = await build_skill_catalog(selected=["git-helper"])

    assert "SECRET_BODY" not in catalog
    assert BODY_MARKER not in catalog


async def test_unselected_skill_body_not_injected(monkeypatch):
    _patch_skill_list(
        monkeypatch,
        {
            "skills": [
                _skill("keep", body="KEEP_BODY"),
                _skill("drop", body="DROP_BODY"),
            ]
        },
    )

    catalog = await build_skill_catalog(selected=["keep"], with_bodies=True)

    assert "KEEP_BODY" in catalog
    assert "DROP_BODY" not in catalog, "未选中的技能不得注入正文"


async def test_no_selection_means_no_bodies(monkeypatch):
    """没选技能时不注入任何正文 —— 否则每一轮都被全部技能正文占满。"""
    _patch_skill_list(monkeypatch, {"skills": [_skill("a", body="A_BODY")]})

    catalog = await build_skill_catalog(with_bodies=True)

    assert "A_BODY" not in catalog
    assert BODY_MARKER not in catalog


async def test_non_prompt_skill_body_not_injected(monkeypatch):
    """python/shell 类技能的 source 是**脚本代码**，不是说明文档，不能当提示词注入。"""
    _patch_skill_list(
        monkeypatch,
        {"skills": [_skill("runner", body="print('hi')", exec_type="python")]},
    )

    catalog = await build_skill_catalog(selected=["runner"], with_bodies=True)

    assert "print('hi')" not in catalog


async def test_body_truncated_with_note(monkeypatch):
    huge = "x" * (SKILL_BODY_BUDGET_TOKENS * CHARS_PER_TOKEN + 500)
    _patch_skill_list(monkeypatch, {"skills": [_skill("huge", body=huge)]})

    catalog = await build_skill_catalog(selected=["huge"], with_bodies=True)

    assert TRUNCATION_NOTE in catalog, "超预算必须注明已截断，不能让模型以为看完了全文"
    # 正文确实被剪到预算以内（外围还有目录/标签/告警等固定开销，不看总长）。
    assert catalog.count("x") <= SKILL_BODY_BUDGET_TOKENS * CHARS_PER_TOKEN


async def test_missing_selected_skill_warns_without_failing(monkeypatch):
    """选中了但没加载出来：告警但不阻断（对话照常进行）。"""
    _patch_skill_list(monkeypatch, {"skills": [_skill("present", body="B")]})

    catalog = await build_skill_catalog(selected=["ghost"], with_bodies=True)

    assert WARNINGS_MARKER in catalog
    assert "ghost" in catalog
    assert "not installed or failed to load" in catalog
