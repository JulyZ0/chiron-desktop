"""H75：市场内容装成 **inert**，显式启用后才可用。

市场安装流水线对外部技能传 `inert=true`，引擎据此把技能落成
`enabled=false` + `auto_inject=false`：

* 不注入每轮 system 上下文（不给模型凭空看到未授权内容）；
* 不可被 `POST /v1/skills/{name}/run` 运行；
* 用户显式启用（`PUT /v1/skills/{name}` 带 `enabled` + `auto_inject`）后才转为可用。

同时守住兼容性：老的 `PUT`（只带 `enabled`）不得顺手改掉注入策略。
"""

from __future__ import annotations

import json

import httpx
from httpx import ASGITransport

from app.skill.store import SkillStore


def _inline(name: str = "inert_skill") -> str:
    return json.dumps(
        {
            "name": name,
            "description": "d",
            "version": "0.1.0",
            "exec": {"type": "prompt", "source": "body"},
        }
    )


async def _client() -> httpx.AsyncClient:
    from app.main import create_app

    return httpx.AsyncClient(
        transport=ASGITransport(app=create_app()), base_url="http://t"
    )


def _store() -> SkillStore:
    return SkillStore(tenant_id="1", user_id="u1")


def _root(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKILL_STORE_PATH", str(tmp_path))


async def test_inert_install_disables_execution_and_injection(tmp_path, monkeypatch):
    """inert 安装：enabled=false 且 auto_inject=false（既不注入也跑不了）。"""
    _root(tmp_path, monkeypatch)
    async with await _client() as client:
        resp = await client.post(
            "/v1/skills/install",
            json={
                "inline": _inline("inert_choice"),
                "origin": "market:team/skill:inert_choice",
                "inert": True,
            },
            params={"user_id": "u1", "tenant_id": "1"},
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False, "inert 安装必须回 enabled=false"
    skill = _store().get("inert_choice")
    assert skill is not None, "内容仍要落盘（用户可见、可后续启用）"
    assert skill.enabled is False
    assert skill.auto_inject is False, "未启用的内容不得进入每轮注入"


async def test_inert_skill_cannot_run_until_enabled(tmp_path, monkeypatch):
    """未启用时运行 → 400；显式启用后 → 可运行。"""
    _root(tmp_path, monkeypatch)
    async with await _client() as client:
        installed = await client.post(
            "/v1/skills/install",
            json={"inline": _inline("gate_skill"), "origin": "market:team/skill:gate", "inert": True},
            params={"user_id": "u1", "tenant_id": "1"},
        )
        assert installed.status_code == 200, installed.text

        blocked = await client.post(
            "/v1/skills/gate_skill/run",
            json={"params": {}},
            params={"user_id": "u1", "tenant_id": "1"},
        )
        assert blocked.status_code == 400, blocked.text
        assert "停用" in blocked.json()["detail"]

        enabled = await client.put(
            "/v1/skills/gate_skill",
            json={"enabled": True, "auto_inject": True},
            params={"user_id": "u1", "tenant_id": "1"},
        )
        assert enabled.status_code == 200, enabled.text
        payload = enabled.json()["skill"]
        assert payload["enabled"] is True
        assert payload["auto_inject"] is True


async def test_toggle_without_auto_inject_keeps_strategy(tmp_path, monkeypatch):
    """兼容性：只传 enabled 的老调用方不改注入策略。"""
    _root(tmp_path, monkeypatch)
    async with await _client() as client:
        installed = await client.post(
            "/v1/skills/install",
            json={"inline": _inline("legacy_toggle"), "origin": "market:team/skill:legacy", "inert": True},
            params={"user_id": "u1", "tenant_id": "1"},
        )
        assert installed.status_code == 200, installed.text

        # 先把注入策略单独打开，再用老形态的 PUT 只切 enabled。
        patched = await client.patch(
            "/v1/skills/legacy_toggle/meta",
            json={"auto_inject": True},
            params={"user_id": "u1", "tenant_id": "1"},
        )
        assert patched.status_code == 200, patched.text

        toggled = await client.put(
            "/v1/skills/legacy_toggle",
            json={"enabled": True},
            params={"user_id": "u1", "tenant_id": "1"},
        )

    assert toggled.status_code == 200, toggled.text
    payload = toggled.json()["skill"]
    assert payload["enabled"] is True
    assert payload["auto_inject"] is True, "未传 auto_inject 时不得改动注入策略"
