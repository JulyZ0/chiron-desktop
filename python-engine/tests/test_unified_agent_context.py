"""统一链路的 context 补全测试。

背景：``ChatView.buildContext`` 只发 ``agent_id``（不发完整 agent 配置，见
``ChatView.vue`` 的 buildContext 注释 —— 配置由后端按 id 补全，避免两端各映射一份）。
SSE 链路走 Go 网关的 ``resolveAgentContext``（internal/api/agents.go:722）补全，
但统一链路的 ``/v1/chat/submit`` 是 ``chatP`` 直通代理，**不经**那道补全，
于是"带 Agent 进对话"在统一模式下静默退化成"没带 Agent"。

这里锁住三件事：
1. ``agent_payload_from_row`` 的字段口径与 Go 侧 ``agentContextPayload`` 一致；
2. ``apply_agent_bindings`` 只补空缺，用户显式选择优先；
3. ``load_agent_payload`` 在查不到 / DB 不可用时返回 None，不阻断对话。
"""

from __future__ import annotations

import pytest

from app.api import unified_executor
from app.api.unified_executor import (
    agent_payload_from_row,
    apply_agent_bindings,
    load_agent_payload,
)


class FakePool:
    """只实现 fetchrow；返回预置行。"""

    def __init__(self, row):
        self._row = row

    async def fetchrow(self, *args, **kwargs):
        return self._row


class TestAgentPayloadFromRow:
    """字段口径必须与 Go 的 agentContextPayload 对齐。"""

    def test_full_row_maps_every_field(self):
        payload = agent_payload_from_row(
            {
                "name": "评审助手",
                "system_prompt": "你负责代码评审",
                "max_turns": 7,
                "llm_config": {"model": "gpt-4o"},
                "tools": [{"type": "function", "function": {"name": "read_file"}}],
                "kb_id": "kb-1",
                "skills": ["pdf", "web"],
                "plugins": ["fs"],
            }
        )
        assert payload == {
            "name": "评审助手",
            "system_prompt": "你负责代码评审",
            "max_turns": 7,
            "model": "gpt-4o",
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
            "kb_id": "kb-1",
            "skills": ["pdf", "web"],
            "plugins": ["fs"],
        }

    def test_jsonb_columns_may_arrive_as_strings(self):
        # pgx 对 jsonb 列可能给 str（取决于注册的解码器），两种形态都要认
        payload = agent_payload_from_row(
            {
                "name": "a",
                "llm_config": '{"model": "m"}',
                "tools": '[{"type": "function"}]',
                "skills": '["pdf"]',
                "plugins": "[]",
            }
        )
        assert payload["model"] == "m"
        assert payload["tools"] == [{"type": "function"}]
        assert payload["skills"] == ["pdf"]
        # 空数组等价于"不绑定"，不传该字段（与 Go 侧 len(...) > 0 判断一致）
        assert "plugins" not in payload

    def test_empty_fields_are_omitted(self):
        payload = agent_payload_from_row(
            {"name": "  ", "system_prompt": None, "max_turns": 0, "kb_id": "", "skills": []}
        )
        assert payload == {}

    def test_non_string_entries_are_dropped(self):
        payload = agent_payload_from_row({"name": "a", "skills": ["pdf", 42, "", " web "]})
        assert payload["skills"] == ["pdf", "web"]

    def test_malformed_json_is_ignored_not_raised(self):
        payload = agent_payload_from_row({"name": "a", "tools": "{not json", "skills": 5})
        assert "tools" not in payload
        assert "skills" not in payload


class TestApplyAgentBindings:
    """Agent 的绑定只补空缺 —— 用户显式选择优先（与 Go applyAgentBindings 同口径）。"""

    def test_fills_missing_bindings(self):
        ctx: dict = {}
        apply_agent_bindings(ctx, {"kb_id": "kb-1", "skills": ["pdf"], "plugins": ["fs"]})
        assert ctx == {"kb_id": "kb-1", "skill_names": ["pdf"], "plugin_names": ["fs"]}

    def test_user_selection_wins(self):
        ctx = {"kb_id": "user-kb", "kb_ids": ["user-kb"], "skill_names": ["mine"]}
        apply_agent_bindings(ctx, {"kb_id": "agent-kb", "skills": ["pdf"], "plugins": ["fs"]})
        assert ctx["kb_id"] == "user-kb"
        assert ctx["skill_names"] == ["mine"]
        # 用户没带插件 → 由 Agent 补
        assert ctx["plugin_names"] == ["fs"]

    def test_no_bindings_is_a_noop(self):
        ctx = {"kb_id": "x"}
        apply_agent_bindings(ctx, {})
        assert ctx == {"kb_id": "x"}


class TestLoadAgentPayload:
    @pytest.mark.asyncio
    async def test_returns_payload_when_found(self, monkeypatch):
        monkeypatch.setattr(
            unified_executor,
            "get_pool",
            lambda: FakePool({"name": "评审助手", "kb_id": "kb-1"}),
        )
        payload = await load_agent_payload("ag-1", "t1", "u1")
        assert payload is not None
        assert payload["name"] == "评审助手"
        assert payload["kb_id"] == "kb-1"

    @pytest.mark.asyncio
    async def test_blank_id_short_circuits(self):
        assert await load_agent_payload("   ", "t1", "u1") is None

    @pytest.mark.asyncio
    async def test_missing_row_returns_none(self, monkeypatch):
        monkeypatch.setattr(unified_executor, "get_pool", lambda: FakePool(None))
        assert await load_agent_payload("ag-x", "t1", "u1") is None

    @pytest.mark.asyncio
    async def test_db_failure_degrades_instead_of_raising(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(unified_executor, "get_pool", boom)
        # 坏 agent_id / DB 不可用不该让整条对话失败，只表现为"这次没带 Agent"
        assert await load_agent_payload("ag-1", "t1", "u1") is None
