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
    assert families == {"error", "terminal"}, f"夹具覆盖的族不完整: {families}"


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
