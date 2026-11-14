"""`GET /v1/skills/discover` 的返回语义 —— 回归保护。

历史缺陷（本次修复）：
1. 给了 `url` 时，远端条目被塞进 **`local`**，而 `remote` 恒为 `[]` —— 键名在说谎；
   同时本地已装列表整个消失，前端无法回答"远端这个我装没装"。
2. 端点自己调 `fetch_url_safe`，绕过了 `app/tools/skill.py:_fetch_skill_json`
   （`skill_discover` / `skill_install` 的唯一取回入口），那层**流式体积上限**因此缺失 ——
   同一个 `url` 在两个入口上策略不一致。

本文件锁死：`local` 只放已安装、`remote` 只放远端目录、`installed` 按名字对比、
远端目录非法形态报 4xx（不是 500 + 空列表）。
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport

from app.main import create_app
from app.skill.store import SkillDef, SkillStore

TENANT = "1"
USER = "u1"


@pytest.fixture(autouse=True)
def _isolated_skill_root(tmp_path, monkeypatch):
    """技能根目录指到临时目录 —— 绝不污染真实 data/skills。

    内置技能源（`market/skills`）也要隔离：它挂在搜索路径末尾，会把仓库自带的技能
    一并列进 `local`，让"本地只有我 seed 的那一个"这条断言失去意义。指到一个**存在
    的空目录**（而非不存在的路径）才不会触发"内置源缺失"告警。
    """
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    monkeypatch.setenv("CHIRON_BUILTIN_SKILLS_PATH", str(builtin))
    return tmp_path


def _seed(name: str) -> None:
    SkillStore(tenant_id=TENANT, user_id=USER, scope="private").save(
        SkillDef(name=name, description=f"{name} desc", exec_type="prompt")
    )


async def _discover(**params: str):
    app = create_app()
    q = {"user_id": USER, "tenant_id": TENANT, **params}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get("/v1/skills/discover", params=q)


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, payload) -> None:
    """替换唯一取回入口（端点内是函数内 import，所以打在源模块上）。"""

    async def _fake(url: str):
        if isinstance(payload, Exception):
            raise payload
        return payload

    monkeypatch.setattr("app.tools.skill._fetch_skill_json", _fake)


async def test_without_url_only_lists_local():
    _seed("s-local")

    resp = await _discover()

    assert resp.status_code == 200
    body = resp.json()
    assert [s["name"] for s in body["local"]] == ["s-local"]
    assert body["remote"] == []


async def test_remote_catalog_is_separated_from_local(monkeypatch):
    """远端条目必须落在 `remote`，且本地列表同时保留（对比才有意义）。"""
    _seed("already-have")
    _patch_fetch(
        monkeypatch,
        [
            {"name": "already-have", "description": "装过了", "version": "2.0"},
            {"name": "brand-new", "description": "还没装", "version": "1.0"},
        ],
    )

    resp = await _discover(url="https://example.com/catalog.json")

    assert resp.status_code == 200
    body = resp.json()
    assert [s["name"] for s in body["local"]] == ["already-have"]
    by_name = {s["name"]: s for s in body["remote"]}
    assert set(by_name) == {"already-have", "brand-new"}
    assert by_name["already-have"]["installed"] is True
    assert by_name["brand-new"]["installed"] is False
    # `source` 是**安装地址**。目录（数组）本身不能当安装地址，所以缺省留空 ——
    # 否则前端会拿目录 url 去 install，后端只会回 "must be a JSON object"。
    assert by_name["brand-new"]["source"] == ""
    # 目录出处另放 catalog_url，两个概念各归各的键
    assert by_name["brand-new"]["catalog_url"] == "https://example.com/catalog.json"


async def test_entry_source_is_kept_when_catalog_provides_one(monkeypatch):
    """目录条目自带安装地址时要原样透传（前端据此才能真的装上）。"""
    _patch_fetch(
        monkeypatch,
        [{"name": "with-url", "source": "https://example.com/with-url.skill.json"}],
    )

    resp = await _discover(url="https://example.com/catalog.json")

    entry = resp.json()["remote"][0]
    assert entry["source"] == "https://example.com/with-url.skill.json"
    assert entry["catalog_url"] == "https://example.com/catalog.json"


async def test_malformed_remote_catalog_is_400(monkeypatch):
    """目录不是数组 ⇒ 400 且说明原因（不能 500，更不能静默回空列表）。"""
    _patch_fetch(monkeypatch, {"oops": "not a list"})

    resp = await _discover(url="https://example.com/catalog.json")

    assert resp.status_code == 400
    assert "JSON array" in resp.json()["detail"]


async def test_ssrf_rejection_is_400_not_502(monkeypatch):
    """SSRF 校验不通过属于**调用方**给错 url，不是上游故障。"""
    _patch_fetch(monkeypatch, ValueError("url blocked: private address"))

    resp = await _discover(url="http://169.254.169.254/latest/meta-data/")

    assert resp.status_code == 400
    assert "blocked" in resp.json()["detail"]


async def test_entries_missing_name_are_skipped(monkeypatch):
    """目录里混进坏条目时只跳过该条，不毁掉整份目录。"""
    _patch_fetch(
        monkeypatch,
        [
            {"description": "没有 name"},
            "not-an-object",
            {"name": "good-one", "description": "ok"},
        ],
    )

    resp = await _discover(url="https://example.com/catalog.json")

    assert resp.status_code == 200
    assert [s["name"] for s in resp.json()["remote"]] == ["good-one"]
