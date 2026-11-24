"""H5：技能安装的**完整性复核**（inline 字节的 sha256）。

市场安装流水线（Go 侧 `internal/api/market_install.go`）在抓取并校验 manifest 之后，
会把摘要一起传下来（`sha256` + `integrity_required=true`）。引擎这一层的作用是挡住
**网关与引擎之间**（或任何直接调引擎的调用方）的内容替换：

* `integrity_required=true` 且缺摘要 → 400，**不落盘**；
* 声明了摘要但对不上 → 400，**不落盘**（无论是否强制）；
* 摘要对上 → 正常安装；
* 老调用方（不带这两个字段）行为不变 —— 引入前它们根本不传摘要。

每条失败用例都额外断言"磁盘上没有这个技能"：只检查状态码发现不了
"先 save 再报错"的实现。
"""

from __future__ import annotations

import hashlib
import json

import httpx
from httpx import ASGITransport

from app.skill.store import SkillStore


def _inline(name: str = "market_skill") -> str:
    return json.dumps(
        {
            "name": name,
            "description": "d",
            "version": "0.1.0",
            "exec": {"type": "prompt", "source": "body"},
        }
    )


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _post_install(payload: dict) -> httpx.Response:
    from app.main import create_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    ) as client:
        return await client.post(
            "/v1/skills/install",
            json=payload,
            params={"user_id": "u1", "tenant_id": "1"},
        )


def _saved(name: str) -> bool:
    """该技能是否真的落盘（用与运行时相同的 store 解析）。"""
    store = SkillStore(tenant_id="1", user_id="u1")
    return store.get(name) is not None


def _skill_root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))


# ── 缺摘要 ──


async def test_install_requires_digest_when_integrity_required(tmp_path, monkeypatch):
    """网关路径（integrity_required）缺摘要 → 400 且不落盘。"""
    _skill_root(tmp_path, monkeypatch)
    resp = await _post_install(
        {
            "inline": _inline("no_digest"),
            "origin": "market:team/skill:no_digest",
            "integrity_required": True,
        }
    )

    assert resp.status_code == 400, resp.text
    assert "sha256" in resp.json()["detail"]
    assert not _saved("no_digest"), "缺摘要时不得落盘"


async def test_install_rejects_malformed_digest(tmp_path, monkeypatch):
    """摘要形状不对（不是 64 位 hex）→ 400 且不落盘。"""
    _skill_root(tmp_path, monkeypatch)
    resp = await _post_install(
        {
            "inline": _inline("bad_shape"),
            "origin": "market:team/skill:bad_shape",
            "integrity_required": True,
            "sha256": "XYZ",
        }
    )

    assert resp.status_code == 400, resp.text
    assert not _saved("bad_shape")


# ── 摘要不匹配（攻击探针）──


async def test_install_rejects_digest_mismatch(tmp_path, monkeypatch):
    """声明的是 A 的摘要、传的是 B 的内容 → 400 且不落盘。"""
    _skill_root(tmp_path, monkeypatch)
    original = _inline("tampered_skill")
    attacker = _inline("tampered_skill").replace("body", "rm -rf /")

    resp = await _post_install(
        {
            "inline": attacker,
            "origin": "market:team/skill:tampered_skill",
            "integrity_required": True,
            "sha256": _digest(original),
        }
    )

    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "sha256 mismatch" in detail
    assert not _saved("tampered_skill"), "摘要不匹配时不得落盘"


async def test_install_rejects_digest_mismatch_even_without_requirement(
    tmp_path, monkeypatch
):
    """即使调用方没声明 integrity_required，只要给了摘要就必须对得上。"""
    _skill_root(tmp_path, monkeypatch)
    resp = await _post_install(
        {
            "inline": _inline("mismatch_optin"),
            "origin": "local",
            "sha256": _digest("别的字节"),
        }
    )

    assert resp.status_code == 400, resp.text
    assert not _saved("mismatch_optin")


# ── 正常路径 ──


async def test_install_accepts_matching_digest(tmp_path, monkeypatch):
    """摘要对上 → 200，并且落盘的就是这份内容。"""
    _skill_root(tmp_path, monkeypatch)
    inline = _inline("verified_skill")

    resp = await _post_install(
        {
            "inline": inline,
            "origin": "market:team/skill:verified_skill",
            "integrity_required": True,
            "sha256": _digest(inline),
        }
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skill"]["name"] == "verified_skill"
    assert body["skill"]["origin"] == "market:team/skill:verified_skill"
    assert _saved("verified_skill")


async def test_install_legacy_inline_without_digest_still_works(tmp_path, monkeypatch):
    """老调用方（不带摘要、不强制）行为不变 —— 兼容性由这两个可选字段保证。"""
    _skill_root(tmp_path, monkeypatch)
    resp = await _post_install({"inline": _inline("legacy_skill"), "origin": "local"})

    assert resp.status_code == 200, resp.text
    assert _saved("legacy_skill")
