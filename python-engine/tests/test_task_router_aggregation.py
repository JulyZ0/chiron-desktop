"""TaskRouter 结果聚合与 NER 实体抽取测试。

测试覆盖:
- _aggregate_results: 按意图类型 (search/analyze/generate_code/general) 定制聚合
- _extract_entities: 规则式 NER 提取 URL/邮箱/手机号/文件路径/日期/金额/IP/代码引用
"""
from __future__ import annotations

import pytest

from app.core.task_router import ExecutedTask, TaskRouter


def _make_router() -> TaskRouter:
    return TaskRouter()


def _make_result(
    subtask_id: str = "sub_0",
    capability_id: str = "cap_0",
    output=None,
    error: str = "",
    status: str = "completed",
) -> ExecutedTask:
    return ExecutedTask(
        task_id="",
        subtask_id=subtask_id,
        capability_id=capability_id,
        input_params={},
        output=output,
        error=error,
        duration_ms=10,
        status=status,
    )


# ── NER 实体抽取 ─────────────────────────────────────────────────


class TestExtractEntities:
    def setup_method(self):
        self.router = _make_router()

    def test_extracts_urls(self):
        text = "Visit https://example.com and http://foo.org/page?q=1"
        entities = self.router._extract_entities(text)
        assert "urls" in entities
        assert "https://example.com" in entities["urls"]
        assert "http://foo.org/page?q=1" in entities["urls"]

    def test_extracts_emails(self):
        text = "Contact alice@test.com or bob@example.org"
        entities = self.router._extract_entities(text)
        assert "emails" in entities
        assert "alice@test.com" in entities["emails"]
        assert "bob@example.org" in entities["emails"]

    def test_extracts_phones(self):
        text = "Call 13812345678 or 19988776655"
        entities = self.router._extract_entities(text)
        assert "phones" in entities
        assert "13812345678" in entities["phones"]
        assert "19988776655" in entities["phones"]

    def test_extracts_unix_file_paths(self):
        text = "Check /usr/local/bin/python and ~/projects/main.py"
        entities = self.router._extract_entities(text)
        assert "file_paths" in entities
        paths = entities["file_paths"]
        assert any("/usr/local/bin/python" in p for p in paths)
        assert any("~/projects/main.py" in p for p in paths)

    def test_extracts_windows_file_paths(self):
        text = "Open C:\\Users\\test\\file.txt"
        entities = self.router._extract_entities(text)
        assert "file_paths" in entities
        assert any("C:\\Users\\test\\file.txt" in p for p in entities["file_paths"])

    def test_extracts_dates(self):
        text = "Deadline is 2026-08-21 or 2026/09/15 or 2026年8月21日"
        entities = self.router._extract_entities(text)
        assert "dates" in entities
        assert "2026-08-21" in entities["dates"]
        assert "2026/09/15" in entities["dates"]
        assert "2026年8月21日" in entities["dates"]

    def test_extracts_amounts(self):
        text = "Price: ¥1,234.50 or $99.99 or 500元 or 3万元"
        entities = self.router._extract_entities(text)
        assert "amounts" in entities
        amounts = entities["amounts"]
        assert any("1,234.50" in a for a in amounts)
        assert any("99.99" in a for a in amounts)
        assert any("500元" in a for a in amounts)

    def test_extracts_ip_addresses(self):
        text = "Server at 192.168.1.1 and 10.0.0.255"
        entities = self.router._extract_entities(text)
        assert "ip_addresses" in entities
        assert "192.168.1.1" in entities["ip_addresses"]
        assert "10.0.0.255" in entities["ip_addresses"]

    def test_extracts_code_refs(self):
        text = "Use os.path.join() or str.split()"
        entities = self.router._extract_entities(text)
        assert "code_refs" in entities
        assert any("os.path.join" in c for c in entities["code_refs"])

    def test_returns_empty_for_plain_text(self):
        entities = self.router._extract_entities("just some plain text")
        assert entities == {}

    def test_extracts_multiple_entity_types(self):
        text = (
            "Email admin@site.com at 13800138000, "
            "visit https://site.com on 2026-08-21, "
            "file at /etc/hosts, cost ¥100"
        )
        entities = self.router._extract_entities(text)
        assert "emails" in entities
        assert "phones" in entities
        assert "urls" in entities
        assert "dates" in entities
        assert "file_paths" in entities
        assert "amounts" in entities


# ── 结果聚合 ─────────────────────────────────────────────────────


class TestAggregateResults:
    def setup_method(self):
        self.router = _make_router()

    @pytest.mark.asyncio
    async def test_search_aggregation_collects_sources(self):
        results = [
            _make_result(
                subtask_id="sub_0_list",
                output={"knowledge_bases": [
                    {"id": "kb1", "name": "Python Docs", "description": "Python stuff"},
                    {"id": "kb2", "name": "Go Docs", "description": "Go stuff"},
                ]},
            ),
            _make_result(
                subtask_id="sub_1_search",
                output={"results": [
                    {"title": "Result A", "url": "https://a.com", "snippet": "snip A"},
                    {"title": "Result B", "url": "https://b.com", "snippet": "snip B"},
                ]},
            ),
        ]
        intent = {"action": "search"}
        out = await self.router._aggregate_results(results, intent, "test query")
        assert out["intent"] == "search"
        assert out["subtasks_completed"] == 2
        assert len(out["sources"]) == 4  # 2 kb + 2 results
        assert "summary" in out
        assert "4" in out["summary"]

    @pytest.mark.asyncio
    async def test_search_aggregation_deduplicates(self):
        results = [
            _make_result(
                output={"results": [
                    {"title": "A", "url": "https://a.com", "snippet": "s1"},
                ]},
            ),
            _make_result(
                output={"results": [
                    {"title": "A dup", "url": "https://a.com", "snippet": "s2"},
                ]},
            ),
        ]
        intent = {"action": "search"}
        out = await self.router._aggregate_results(results, intent, "q")
        assert len(out["sources"]) == 1  # deduplicated by url

    @pytest.mark.asyncio
    async def test_analyze_aggregation_synthesizes(self):
        results = [
            _make_result(
                subtask_id="sub_0_read",
                output={"analysis": "Found 3 critical issues"},
            ),
            _make_result(
                subtask_id="sub_1_analyze",
                output={"result": "Recommend refactoring"},
            ),
        ]
        intent = {"action": "analyze"}
        out = await self.router._aggregate_results(results, intent, "analyze this")
        assert out["intent"] == "analyze"
        assert len(out["findings"]) == 2
        assert "critical issues" in out["synthesis"]
        assert "refactoring" in out["synthesis"]

    @pytest.mark.asyncio
    async def test_generate_code_aggregation_combines(self):
        results = [
            _make_result(
                subtask_id="sub_0_parse",
                output={"code": "def hello():\n    print('hi')"},
            ),
            _make_result(
                subtask_id="sub_1_write",
                output={"code": "hello()"},
            ),
        ]
        intent = {"action": "generate_code"}
        out = await self.router._aggregate_results(results, intent, "write code")
        assert out["intent"] == "generate_code"
        assert len(out["code_blocks"]) == 2
        assert "def hello" in out["combined_code"]
        assert "hello()" in out["combined_code"]

    @pytest.mark.asyncio
    async def test_general_aggregation_default(self):
        results = [
            _make_result(output={"data": "ok"}),
            _make_result(output=None, error="failed", status="failed"),
        ]
        intent = {"action": "general"}
        out = await self.router._aggregate_results(results, intent, "do something")
        assert out["intent"] == "general"
        assert out["subtasks_completed"] == 1
        assert out["subtasks_failed"] == 1
        assert len(out["outputs"]) == 2
        assert out["outputs"][0]["output"] == {"data": "ok"}
        assert out["outputs"][1]["error"] == "failed"

    @pytest.mark.asyncio
    async def test_aggregation_with_failed_subtasks(self):
        results = [
            _make_result(output={"results": []}),
            _make_result(output=None, error="kb not found", status="failed"),
        ]
        intent = {"action": "search"}
        out = await self.router._aggregate_results(results, intent, "q")
        assert out["subtasks_failed"] == 1
        assert "errors" in out
        assert "kb not found" in out["errors"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
