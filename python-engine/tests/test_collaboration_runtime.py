"""协同 Agent 运行时创建/任务构造回归测试。

核心意图（历史缺陷回归）:
- _get_or_create_runtime 必须用 AgentRuntime 的真实签名构造
  （此前传 mode_config/compaction_config 无效参数 → TypeError）
- 协同任务的 LLM 调用走 runtime.run()（此前调用不存在的 run_single_turn
  → AttributeError）
- 传给 runtime.run() 的必须是完整 AgentTask（此前用伪造对象缺
  llm_config/user_id 等属性 → run() 内部 AttributeError）
- spec 的 mode/model/compaction_config 必须真正注入任务并被 runtime 消费
"""
from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

import app.main  # noqa: F401 — 初始化 app 包，避免循环导入
from app.agent.collaboration import AgentHub, AgentRole, AgentSpec, CollaborativeTask
from app.agent.runtime import AgentRuntime, CompactionConfig


def _hub() -> AgentHub:
    return AgentHub(gateway=MagicMock())


# ── 运行时池 ──────────────────────────────────────────────

def test_get_or_create_runtime_uses_real_signature():
    """运行时构造不再传无效参数（TypeError 回归）。"""
    hub = _hub()
    spec = hub.AGENT_ROLES[AgentRole.PLANNER]
    runtime = hub._get_or_create_runtime(spec)  # 不应抛 TypeError
    assert isinstance(runtime, AgentRuntime)


def test_runtime_pool_reuses_instance_per_role():
    """同角色的 runtime 复用，不同角色各自创建。"""
    hub = _hub()
    r1 = hub._get_or_create_runtime(hub.AGENT_ROLES[AgentRole.PLANNER])
    r2 = hub._get_or_create_runtime(hub.AGENT_ROLES[AgentRole.PLANNER])
    r3 = hub._get_or_create_runtime(hub.AGENT_ROLES[AgentRole.ORCHESTRATOR])
    assert r1 is r2
    assert r1 is not r3


# ── 任务构造 ──────────────────────────────────────────────

def test_make_agent_task_builds_complete_task():
    """构造的 AgentTask 必须带全 runtime.run() 所需属性。"""
    hub = _hub()
    spec = hub.AGENT_ROLES[AgentRole.PLANNER]
    task = hub._make_agent_task(
        spec=spec,
        task_id="planning_t1",
        content="拆解任务",
        tenant_id="tenant-a",
    )
    # runtime.run() 访问的全部字段
    assert task.id == "planning_t1"
    assert task.tenant_id == "tenant-a"
    assert task.user_id
    assert task.session_id
    assert task.content == "拆解任务"
    assert task.system_prompt == spec.system_prompt
    assert isinstance(task.llm_config, dict)
    assert task.max_turns == spec.max_turns
    assert task.subagent_depth == 0


def test_make_agent_task_injects_mode_and_model():
    """spec.mode / spec.model 经 llm_config 注入，被 runtime 的
    get_mode_config(task.llm_config["mode"]) 消费。"""
    spec = AgentSpec(
        role=AgentRole.RESEARCHER,
        description="研究",
        system_prompt="sys",
        model="test-model-x",
        mode="minimal",
    )
    task = _hub()._make_agent_task(
        spec=spec, task_id="t", content="c", tenant_id="tn",
    )
    assert task.llm_config["mode"] == "minimal"
    assert task.llm_config["model"] == "test-model-x"


def test_make_agent_task_serializes_compaction_override():
    """spec.compaction_config → llm_config["compaction"]（dict 形式，
    runtime._resolve_compaction 按字典重建）。"""
    spec = AgentSpec(
        role=AgentRole.CODER,
        description="编码",
        system_prompt="sys",
        compaction_config=CompactionConfig(strategy="snipe", max_messages=5),
    )
    task = _hub()._make_agent_task(
        spec=spec, task_id="t", content="c", tenant_id="tn",
    )
    comp = task.llm_config["compaction"]
    assert comp["strategy"] == "snipe"
    assert comp["max_messages"] == 5
    # 完整 dict（runtime 按 CompactionConfig(**override) 重建）
    assert comp == asdict(spec.compaction_config)


# ── runtime 侧 compaction 覆盖解析 ───────────────────────

def test_resolve_compaction_task_override_wins():
    """逐任务覆盖优先于模式配置。"""
    from app.agent.modes import get_mode_config

    mode_cfg = get_mode_config("normal")
    override = asdict(CompactionConfig(strategy="prune", max_messages=3))
    cfg = AgentRuntime._resolve_compaction(mode_cfg, {"compaction": override})
    assert cfg is not None
    assert cfg.strategy == "prune"
    assert cfg.max_messages == 3


def test_resolve_compaction_falls_back_to_mode_then_default():
    """无任务覆盖：模式配置（若有）→ None（默认策略）。"""
    from app.agent.modes import get_mode_config

    mode_cfg = get_mode_config("normal")
    # normal 模式无 compaction 字段 → None
    assert AgentRuntime._resolve_compaction(mode_cfg, {}) is None
    # 无效覆盖类型被忽略
    assert AgentRuntime._resolve_compaction(mode_cfg, {"compaction": "bad"}) is None


# ── 端到端: run_collaborative_task 走真实 run() 路径 ──────

@pytest.mark.asyncio
async def test_run_collaborative_task_executes_full_flow(monkeypatch):
    """预置 subtasks 的协同任务应完整走 DAG 执行 + 聚合（不再
    run_single_turn / 伪造任务）。"""
    hub = _hub()

    # mock runtime.run: 每个 task 产出一个 text 事件后结束
    async def fake_run(self, task):
        from app.agent.runtime import AgentEvent
        yield AgentEvent(type="text", content=f"done:{task.id}")

    monkeypatch.setattr(AgentRuntime, "run", fake_run)

    # record_span 走内存（无 Redis 时不应炸）
    import app.agent.collaboration as collab_mod
    async def fake_record_span(**kwargs):
        return None
    monkeypatch.setattr(collab_mod, "record_span", fake_record_span)

    task = CollaborativeTask(
        task_id="ct-1",
        original_query="写一个加法函数并审查",
        tenant_id="tenant-a",
        trace_id="trace-1",
        subtasks=[
            {"role": "coder", "description": "实现加法", "dependencies": []},
            {"role": "reviewer", "description": "审查", "dependencies": ["subtask_0"]},
        ],
    )

    events = [e async for e in hub.run_collaborative_task(task)]

    # 子任务执行 + 聚合 + done 事件全部到达
    text_events = [e for e in events if e.type == "text"]
    assert any("subtask_0" in e.content for e in text_events)
    assert any("subtask_1" in e.content for e in text_events)
    assert any("aggregation" in e.content for e in text_events)
    assert any(e.type == "done" for e in events)
    # 所有事件共享 trace_id
    assert all(e.trace_id == "trace-1" for e in events)
    # 共享上下文已写入
    assert "coder" in task.shared_context
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_run_collaborative_task_planner_decomposes(monkeypatch):
    """无预置 subtasks 时 Planner 先拆解，再执行 DAG。"""
    hub = _hub()

    async def fake_run(self, task):
        from app.agent.runtime import AgentEvent
        if task.id.startswith("planning_"):
            yield AgentEvent(type="text", content=(
                '[{"role": "researcher", "description": "查资料", "dependencies": []}]'
            ))
        else:
            yield AgentEvent(type="text", content=f"done:{task.id}")

    monkeypatch.setattr(AgentRuntime, "run", fake_run)

    import app.agent.collaboration as collab_mod
    async def fake_record_span(**kwargs):
        return None
    monkeypatch.setattr(collab_mod, "record_span", fake_record_span)

    task = CollaborativeTask(
        task_id="ct-2",
        original_query="调研 FastAPI 性能优化",
        tenant_id="tenant-b",
        trace_id="trace-2",
        subtasks=[],
    )

    events = [e async for e in hub.run_collaborative_task(task)]

    # Planner 输出被解析为子任务并执行
    assert any("subtask_0" in e.content for e in events if e.type == "text")
    assert len(task.subtasks) == 1
    assert task.subtasks[0]["role"] == "researcher"
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_tenant_concurrency_quota_enforced():
    """租户并发配额: 达到上限后新任务被拒绝（fail-loud 事件）。"""
    hub = _hub()
    hub._tenant_running_agents["tenant-c"] = hub._max_concurrent_per_tenant

    task = CollaborativeTask(
        task_id="ct-3",
        original_query="x",
        tenant_id="tenant-c",
        trace_id="trace-3",
        subtasks=[],
    )

    events = [e async for e in hub.run_collaborative_task(task)]
    assert any(e.type == "error" for e in events)
    assert task.status == "pending"  # 未进入执行
