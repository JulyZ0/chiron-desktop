"""知识库注入（B 断点）的单测：字段解析 + 检索注入 + 零命中/失败可见。

背景（实测过的故障）：前端 `@` 选库 → `context.kb_ids` → 网关透传 → 引擎
`workbench_context`，字段一路都是通的；但对话实际走的 `AgentRuntime.run()`
从来不读 `kb_ids`，所以回答里一行库内容都没有。本文件钉住修复后的三条语义：

1. **选了库就一定有注入**（命中 → 资料块；零命中 → 明确的"已检索、无命中"）；
2. **失败可见**（单个库失败不拖垮其余库，原因进文本 + 日志）；
3. **没选库 ⇒ 零行为变化**（`KNOWLEDGE` 段不存在）。
"""

from __future__ import annotations

import logging

import pytest

from app.agent import knowledge_context as kb_ctx
from app.agent.prompt_parts import build_system_sections
from app.agent.prompt_sections import KNOWLEDGE, standalone_texts
from app.agent.runtime_types import AgentTask
from app.agent.workbench_context import MAX_CONTEXT_KBS, context_ids


# ── kb_ids 解析（前端提交字段的引擎侧口径）──


def test_context_ids_prefers_multi_value_and_falls_back_to_single():
    """多值优先、单值回退：老前端与手写 URL 只发单值，不能因此丢库。"""
    assert context_ids({"kb_ids": ["a", "b"], "kb_id": "a"}, "kb") == ["a", "b"]
    assert context_ids({"kb_id": "only"}, "kb") == ["only"]
    # 两者同时出现以复数（新契约）为准，不合并 —— 合并会把"已取消的库"又带回来
    assert context_ids({"kb_ids": ["b"], "kb_id": "a"}, "kb") == ["b"]


def test_kb_ids_of_dedupes_trims_and_caps():
    ctx = {"kb_ids": [" a ", "a", "", "b", "c", "d", "e", "f", "g"]}
    ids = kb_ctx.kb_ids_of(ctx)
    assert ids[0] == "a" and "a" in ids and "" not in ids
    assert len(ids) == MAX_CONTEXT_KBS, "手写 URL 也要被后端截断（与前端同值）"
    assert kb_ctx.kb_ids_of(None) == []
    assert kb_ctx.kb_ids_of({"kb_id": "  "}) == []


# ── 检索注入（mock provider）──


async def _hit_retriever(kb_id, query, user_id, tenant_id):
    return {
        "answer": f"{kb_id} 的答案：项目交付周期为 6 周。",
        "citations": [{"doc_name": "计划书.pdf", "page": 3, "snippet": "交付周期 6 周"}],
    }


async def _empty_retriever(kb_id, query, user_id, tenant_id):
    return {"answer": "", "citations": []}


async def _boom_retriever(kb_id, query, user_id, tenant_id):
    raise RuntimeError("检索服务不可达（http://127.0.0.1:8100）")


@pytest.mark.asyncio
async def test_collect_injects_answer_and_citations():
    injection = await kb_ctx.collect(["kb-1"], "交付周期多久", user_id="u1", retriever=_hit_retriever)
    assert injection.hits == 1
    assert injection.kb_ids == ["kb-1"]
    assert "项目交付周期为 6 周" in injection.block
    assert "计划书.pdf" in injection.block and "第 3 页" in injection.block
    assert "没有命中" not in injection.block


@pytest.mark.asyncio
async def test_collect_zero_hits_is_explicit_not_silent():
    """**最关键的一条**：选了库但 0 命中，必须注入"已检索、无命中"，不能返回空串。

    返回空串等于让模型以为"用户没选库" —— 它会照着常识瞎答，用户则以为库是空的。
    """
    injection = await kb_ctx.collect(["kb-1", "kb-2"], "无关问题", user_id="u1", retriever=_empty_retriever)
    assert injection.hits == 0
    assert injection.block, "零命中时注入块不能为空（那正是静默无痕的旧行为）"
    assert "没有命中" in injection.block
    assert "已" in injection.block and "不是未检索" in injection.block
    assert "kb-1" in injection.block or "kb-2" in injection.block


@pytest.mark.asyncio
async def test_collect_partial_failure_keeps_other_kbs(caplog):
    """单库失败不拖垮其余库；失败原因既进注入文本也进日志。"""

    async def _mixed(kb_id, query, user_id, tenant_id):
        if kb_id == "bad":
            raise RuntimeError("无权访问该知识库")
        return {"answer": f"{kb_id} 有内容", "citations": []}

    with caplog.at_level(logging.WARNING):
        injection = await kb_ctx.collect(["bad", "good"], "q", user_id="u1", retriever=_mixed)

    assert [kb for kb, _ in injection.failures] == ["bad"]
    assert "good 有内容" in injection.block
    assert "检索失败" in injection.block and "无权访问该知识库" in injection.block
    assert any("kb context retrieve failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_folder_all_failed_is_visible_in_log(caplog):
    with caplog.at_level(logging.WARNING):
        injection = await kb_ctx.collect(["kb-1"], "q", user_id="u1", retriever=_boom_retriever)
    assert injection.hits == 0
    assert injection.failures and injection.failures[0][0] == "kb-1"
    assert "检索服务不可达" in injection.block, "检索故障必须可读（区分于'库里没资料'）"
    assert any("kb context retrieve failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_collect_without_kb_is_noop():
    """没选库 ⇒ 不注入、不检索（零行为变化）。"""
    calls = {"n": 0}

    async def _counting(*args, **kwargs):
        calls["n"] += 1
        return {"answer": "x"}

    injection = await kb_ctx.collect([], "q", user_id="u1", retriever=_counting)
    assert injection.block == "" and calls["n"] == 0


# ── system 段装配 ──


def test_knowledge_section_is_standalone_and_absent_by_default():
    """空串 = 没选库 ⇒ 段不存在（system 段与引入前逐字相同）。"""
    task = AgentTask(id="t1", tenant_id=1, user_id="u1", session_id="s1", content="hi")
    task.system_prompt = "You are Chiron."
    sections = build_system_sections(task)
    assert sections.standalone.get(KNOWLEDGE, "") == ""
    assert all(KNOWLEDGE not in text for text in standalone_texts(sections))


def test_knowledge_section_renders_as_own_message():
    task = AgentTask(id="t1", tenant_id=1, user_id="u1", session_id="s1", content="hi")
    task.system_prompt = "You are Chiron."
    task.knowledge_context = "【知识库检索结果】\n交付周期 6 周"
    sections = build_system_sections(task)
    texts = standalone_texts(sections)
    assert any("交付周期 6 周" in text for text in texts)
    # 不能拼进稳定前缀（那会让整段前缀缓存失效）
    inline_text = "\n".join(sections.inline.values())
    assert "交付周期 6 周" not in inline_text
