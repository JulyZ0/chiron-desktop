"""H16：Python 侧跑与 Go 侧**同一组** conformance 夹具。

契约是"两端都同意的判据"，所以测试必须读同一份文件：
`contracts/fixtures/conformance.json`（Go 侧 `internal/contract` 也读它）。
"""

from __future__ import annotations

import pytest

from app import contract


def test_version_matches_fixtures() -> None:
    assert contract.load_fixtures()["version"] == contract.VERSION


def test_fixtures_exist() -> None:
    cases = contract.load_fixtures()["cases"]
    assert cases, "夹具为空"
    families = {case["family"] for case in cases}
    assert families == {
        "error",
        "terminal",
        "run_event",
        # v2（H16b）纳入的四族
        "tool_approval",
        "checkpoint",
        "queue",
        "mcp",
    }, f"夹具覆盖的族不完整: {families}"


@pytest.mark.parametrize(
    "case",
    contract.load_fixtures()["cases"],
    ids=lambda case: f"{case['family']}/{case['name']}",
)
def test_conformance_case(case: dict) -> None:
    payload = dict(case["payload"])
    if case["name"] == "message-too-long":
        # 夹具用占位串，这里换成真实超长消息（避免文件里塞 4KB 空白）
        payload["message"] = "x" * 4097
    violations = contract.validate(case["family"], payload)
    if case["valid"]:
        assert violations == [], f"应通过却被判违约: {violations}"
    else:
        assert violations, f"应违约却通过了（{case.get('reason', '')}）"


def test_unknown_family_raises() -> None:
    with pytest.raises(ValueError):
        contract.validate("nope", {})


def test_additive_fields_still_valid() -> None:
    """additive-only：同版本内新增字段不该让旧校验失败。"""
    assert contract.validate_error({"message": "ok", "brand_new": [1, 2]}) == []
    assert contract.validate_terminal(
        {"type": "error", "error": {"message": "boom", "extra": True}, "future": "x"}
    ) == []


def test_terminal_sequence_rejects_duplicates() -> None:
    events = [{"type": "delta"}, {"type": "done"}, {"type": "done"}]
    assert contract.terminal_sequence(events), "两个终态事件必须判违约"


def test_terminal_sequence_accepts_single() -> None:
    events = [{"type": "delta"}, {"type": "error", "error": {"message": "上游挂了"}}]
    assert contract.terminal_sequence(events) == []


def test_terminal_sequence_zero_is_reported_by_callers_not_here() -> None:
    """零个终态事件由调用方（收尾检查）判定；本函数只管"不重复、自身合法"。"""
    assert contract.terminal_sequence([{"type": "delta"}]) == []


def test_message_length_counts_bytes_not_chars() -> None:
    """4096 是**字节**上限：中文三字节，1366 个汉字就超了。"""
    ok = "汉" * 1365
    assert contract.validate_error({"message": ok}) == []
    too_long = "汉" * 1367
    assert contract.validate_error({"message": too_long}), "字节数超限必须判违约"


# ── v2（H16b）：tool_approval / checkpoint / queue / mcp ──────────────────────
#
# 夹具驱动那一条（test_conformance_case）已经自动覆盖新族；下面补的是"夹具之外"的
# 判据：每族的覆盖面达标、契约文件与实现同步、以及几条**形状语义**的独立断言
# （它们比夹具更早、更精确地指出是实现坏了还是夹具坏了）。

_V2_FAMILIES = ("tool_approval", "checkpoint", "queue", "mcp")


def test_fixture_coverage_per_family() -> None:
    """每族都要有"能区分对错"的最小用例集：≥4 条、至少 1 条反例、至少 1 条兼容例。"""
    cases = contract.load_fixtures()["cases"]
    for family in _V2_FAMILIES:
        subset = [case for case in cases if case["family"] == family]
        assert len(subset) >= 4, f"族 {family} 只有 {len(subset)} 条夹具（要求 ≥4）"
        assert [case for case in subset if not case["valid"]], f"族 {family} 缺非法例"
        assert [case for case in subset if case.get("forward_compat")], (
            f"族 {family} 缺 forward-compat 例：未知字段必须被忽略"
        )


def test_versioned_contract_covers_implementation() -> None:
    """契约文件里出现的族必须都有实现（否则"纳入契约"只是写了个字）。"""
    import json
    from pathlib import Path

    path = Path(contract.__file__).resolve().parents[3] / "contracts" / "versioned.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == contract.VERSION
    assert data["deferred"]["families"] == [], "v2 应已把 deferred 清空"
    for family in data["families"]:
        contract.validate(family, {})  # 未知族会 raise，这里即断言"已注册"
    for family in _V2_FAMILIES:
        assert family in data["families"], f"v2 缺族 {family}"


def test_tool_approval_trinary_decision() -> None:
    """三态是 approve/reject/edit（不是 approved/denied/edited）。"""
    base = {"session_id": "s", "tool_call_id": "c"}
    assert contract.validate_tool_approval({**base, "decision": "approve"}) == []
    assert contract.validate_tool_approval({**base, "decision": "reject"}) == []
    assert contract.validate_tool_approval(
        {**base, "decision": "edit", "arguments": '{"command":"ls"}'}
    ) == []
    assert contract.validate_tool_approval({**base, "decision": "edit"}), "edit 必须带参数"
    for wrong in ("approved", "denied", "edited", "allow"):
        assert contract.validate_tool_approval({**base, "decision": wrong}), f"{wrong} 不是合法取值"


def test_tool_approval_requires_a_decision() -> None:
    """两个字段都缺必须判违约：缺字段不得等同于放行。"""
    assert contract.validate_tool_approval({"session_id": "s", "tool_call_id": "c"})
    assert contract.validate_tool_approval(
        {"session_id": "s", "tool_call_id": "c", "approved": False}
    ) == [], "显式 false 是拒绝，不是'没发'"


def test_checkpoint_ok_requires_rollback_target() -> None:
    """state=ok 时 commit/tree/ref 必须齐；失败态不要求（为空正是失败原因）。"""
    full = {
        "session_id": "s",
        "seq": 1,
        "kind": "pre_turn",
        "commit_sha": "c",
        "tree_sha": "t",
        "ref_name": "r",
    }
    assert contract.validate_checkpoint(full) == []
    for missing in ("commit_sha", "tree_sha", "ref_name"):
        payload = {k: v for k, v in full.items() if k != missing}
        assert contract.validate_checkpoint(payload), f"{missing} 缺失必须判违约"
    assert contract.validate_checkpoint(
        {"session_id": "s", "seq": 2, "kind": "post_turn", "state": "failed", "error_code": "git"}
    ) == []


def test_checkpoint_untracked_manifest() -> None:
    base = {
        "session_id": "s",
        "seq": 1,
        "kind": "post_turn",
        "commit_sha": "c",
        "tree_sha": "t",
        "ref_name": "r",
    }
    assert contract.validate_checkpoint(
        {**base, "untracked": [{"path": "a.txt", "sha256": "aa", "size": 3}]}
    ) == []
    assert contract.validate_checkpoint({**base, "untracked": [{"sha256": "aa"}]})
    assert contract.validate_checkpoint({**base, "untracked": [{"path": "a.txt", "sha256": ""}]})
    assert contract.validate_checkpoint({**base, "untracked": "a.txt"})


def test_queue_accepts_redis_string_encodings() -> None:
    """Redis 字段一律是字符串：数字串必须收，否则真实消息全被判违约。"""
    assert contract.validate_queue(
        {
            "task_id": "t1",
            "task_type": "memory_save",
            "tenant_id": "7",
            "payload": '{"a":1}',
            "retry_count": "1",
            "deadline": "2026-11-26T00:00:00Z",
        }
    ) == []


def test_queue_rejects_dirty_fields() -> None:
    base = {"task_id": "t1", "task_type": "memory_save", "tenant_id": 7, "payload": "{}"}
    assert contract.validate_queue({**base, "tenant_id": "acme"})
    assert contract.validate_queue({**base, "payload": "[1,2]"})
    assert contract.validate_queue({**base, "retry_count": -1})
    assert contract.validate_queue({**base, "deadline": "2026-11-26 00:00:00"})
    # task_type 不设枚举：新增任务类型是 additive 的，未知类型由消费方送 DLQ。
    assert contract.validate_queue({**base, "task_type": "brand_new_task"}) == []


def test_mcp_approval_requires_digest() -> None:
    """approved=true 必须有摘要：没有摘要的批准等于"不知道批了什么"。"""
    assert contract.validate_mcp(
        {"name": "xerj", "command": "npx", "approved": True, "approval_digest": "abc123"}
    ) == []
    assert contract.validate_mcp({"name": "xerj", "command": "npx", "approved": True})
    assert contract.validate_mcp({"name": "xerj", "command": "npx"}) == []


def test_v2_families_ignore_unknown_fields() -> None:
    """兼容承诺：同版本内新增字段不得让旧校验失败。"""
    assert contract.validate_tool_approval(
        {"session_id": "s", "tool_call_id": "c", "decision": "approve", "question": "?", "risk": "high"}
    ) == []
    assert contract.validate_checkpoint(
        {
            "session_id": "s",
            "seq": 1,
            "kind": "pre_turn",
            "commit_sha": "c",
            "tree_sha": "t",
            "ref_name": "r",
            "label": "",
            "future": {"x": 1},
        }
    ) == []
    assert contract.validate_queue(
        {
            "task_id": "t",
            "task_type": "memory_save",
            "tenant_id": 7,
            "payload": "{}",
            "created_at": "2026-11-25T00:00:00Z",
            "priority": "1",
        }
    ) == []
    assert contract.validate_mcp(
        {"name": "x", "command": "npx", "description": "d", "version": "1.0.0", "source": "market"}
    ) == []
