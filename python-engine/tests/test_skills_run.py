"""skill_run 执行链测试：四种 exec 类型 + 模板渲染 + 状态校验。"""
from __future__ import annotations

import pytest

from app.skill.store import SkillDef, SkillStore
from app.tools import skill as skill_mod


def _make_store(tmp_path) -> SkillStore:
    return SkillStore(str(tmp_path / "skills"))


def _save(store: SkillStore, s: SkillDef) -> None:
    store.save(s)
    skill_mod._store = store  # noqa: SLF001 — 测试注入临时 store


class FakeGateway:
    def __init__(self, reply: str = "llm reply") -> None:
        self._reply = reply
        self.calls: list[list[dict]] = []

    async def chat_stream(self, messages=None, model=""):
        self.calls.append(messages or [])
        class _C:
            content = self._reply
        yield _C()


@pytest.mark.asyncio
async def test_render_template_defaults_and_params():
    src = "Summary: {topic} / language {lang}"
    rendered = skill_mod._render_template(src, {"topic": "AI"}, [
        {"name": "topic", "type": "string"},
        {"name": "lang", "default": "Chinese"},
    ])
    assert rendered == "Summary: AI / language Chinese"
    # 显式参数覆盖默认值
    rendered2 = skill_mod._render_template(src, {"topic": "AI", "lang": "English"}, [{"name": "lang", "default": "Chinese"}])
    assert rendered2 == "Summary: AI / language English"


@pytest.mark.asyncio
async def test_skill_run_prompt_calls_llm(tmp_path, monkeypatch):
    store = _make_store(tmp_path)
    _save(store, SkillDef(name="greet", description="greet", exec_type="prompt",
                          source="Hello {name}, welcome!", parameters=[{"name": "name", "default": "world"}]))

    gw = FakeGateway("hi there")
    async def _fake_get_gateway():
        return gw
    monkeypatch.setattr("app.main.get_gateway", _fake_get_gateway)

    result = await skill_mod.skill_run("greet", {"name": "Alice"})
    assert result["output"] == "hi there"
    assert result["skill"] == "greet"
    # LLM 收到的 prompt 是渲染后的模板
    assert gw.calls and gw.calls[0][0]["content"] == "Hello Alice, welcome!"


@pytest.mark.asyncio
async def test_skill_run_shell_in_sandbox(tmp_path):
    store = _make_store(tmp_path)
    # 使用 python -c 替代 echo（echo 是 shell 内建命令，create_subprocess_exec 无法直接执行）
    _save(store, SkillDef(name="echo-skill", description="echo", exec_type="shell",
                          source="python -c \"print('hi-{who}')\""))
    result = await skill_mod.skill_run("echo-skill", {"who": "skill"})
    assert "hi-skill" in result["output"]


@pytest.mark.asyncio
async def test_skill_run_python_in_sandbox(tmp_path):
    store = _make_store(tmp_path)
    _save(store, SkillDef(name="calc", description="calc", exec_type="python",
                          source="return f'sum={ {a} + {b} }'"))
    result = await skill_mod.skill_run("calc", {"a": 2, "b": 3})
    assert "sum=5" in result["output"]


@pytest.mark.asyncio
async def test_skill_run_disabled_and_missing(tmp_path):
    store = _make_store(tmp_path)
    _save(store, SkillDef(name="off", description="off", exec_type="prompt", source="x", enabled=False))
    result = await skill_mod.skill_run("off", {})
    assert "disabled" in result["error"]

    result2 = await skill_mod.skill_run("nope", {})
    assert "not found" in result2["error"]


@pytest.mark.asyncio
async def test_skill_run_http_requires_url(tmp_path):
    store = _make_store(tmp_path)
    _save(store, SkillDef(name="fetch", description="fetch", exec_type="http", source="not-a-url"))
    result = await skill_mod.skill_run("fetch", {})
    assert "output" in result and "invalid url" in result["output"]
