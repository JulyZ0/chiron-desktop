"""工具参数归一与校验（G-T1）的回归测试。

背景：`registry.execute` 此前直接
`handler(**params)`，模型把类型写错或漏字段时只会拿到一句没有指向性的 `str(TypeError)`，
无法自我修复。这里守住三条契约：**能自动修的要修**（coerce）、**真错的要点名到字段**、
**不确定的不要误伤**（额外字段 / 坏 schema 都放行）。
"""

import pytest

from app.tools.arg_validation import (
    coerce_params,
    format_errors,
    prepare_params,
    validate_params,
)
from app.tools.registry import ToolRegistry

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer"},
        "limit": {"type": "integer"},
    },
    "required": ["path"],
}


# ── coerce：能自动修的要修 ─────────────────────────────────────────────


def test_string_integer_is_coerced():
    """模型常把数字写成字符串 —— 这是可自动修复的偏差，不该判死。

    判死的后果是模型看到 "limit: expected integer, got string"，但它**本来给的就是
    数字的字符串形式**，重试大概率还是同一个写法，于是陷入"失败→重试→失败"。
    """
    params, errors = prepare_params("read_file", {"path": "a.py", "limit": "50"}, READ_SCHEMA)
    assert errors == []
    assert params["limit"] == 50
    assert isinstance(params["limit"], int)


def test_string_boolean_is_coerced():
    schema = {
        "type": "object",
        "properties": {"append": {"type": "boolean"}},
        "required": [],
    }
    params, errors = prepare_params("write_file", {"append": "true"}, schema)
    assert errors == []
    assert params["append"] is True


def test_non_integer_numeric_string_is_not_coerced():
    """"50.0" 不该被悄悄变成 50 —— 声明是 integer，静默转浮点/截断都是掩盖错误。"""
    params, errors = prepare_params("read_file", {"path": "a.py", "limit": "50.0"}, READ_SCHEMA)
    assert params["limit"] == "50.0"
    assert errors, "浮点字符串对 integer 字段应当报错而不是被强转"


def test_nested_object_is_coerced():
    schema = {
        "type": "object",
        "properties": {
            "opts": {
                "type": "object",
                "properties": {"retries": {"type": "integer"}},
                "required": ["retries"],
            }
        },
        "required": ["opts"],
    }
    params, errors = prepare_params("demo", {"opts": {"retries": "3"}}, schema)
    assert errors == []
    assert params["opts"]["retries"] == 3


def test_json_string_object_is_parsed():
    """模型偶尔把结构化参数整个序列化成一个字符串再塞进字段。"""
    schema = {
        "type": "object",
        "properties": {"opts": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "required": ["opts"],
    }
    params, errors = prepare_params("demo", {"opts": '{"a": "b"}'}, schema)
    assert errors == []
    assert params["opts"] == {"a": "b"}


def test_coerce_does_not_mutate_input():
    original = {"path": "a.py", "limit": "50"}
    coerce_params(original, READ_SCHEMA)
    assert original["limit"] == "50", "归一必须返回新 dict，不能就地改调用方的参数"


# ── 点路径报错 ───────────────────────────────────────────────────────


def test_missing_required_field_names_the_field():
    _, errors = prepare_params("read_file", {}, READ_SCHEMA)
    assert len(errors) == 1
    assert "path" in errors[0]
    assert "required" in errors[0]


def test_missing_nested_field_uses_dotted_path():
    schema = {
        "type": "object",
        "properties": {
            "opts": {
                "type": "object",
                "properties": {"retries": {"type": "integer"}},
                "required": ["retries"],
            }
        },
        "required": ["opts"],
    }
    _, errors = prepare_params("demo", {"opts": {}}, schema)
    assert errors == ["opts.retries: required field is missing"]


def test_wrong_type_names_the_field():
    _, errors = prepare_params("read_file", {"path": "a.py", "limit": "abc"}, READ_SCHEMA)
    assert len(errors) == 1
    assert errors[0].startswith("limit:")
    assert "integer" in errors[0]


def test_error_count_is_capped():
    """几十条错误塞回上下文只会把该改的那条淹掉。"""
    schema = {
        "type": "object",
        "properties": {f"f{i}": {"type": "integer"} for i in range(20)},
        "required": [f"f{i}" for i in range(20)],
    }
    errors = validate_params({}, schema)
    assert 0 < len(errors) <= 5


def test_format_errors_is_actionable():
    message = format_errors("read_file", ["limit: expected integer"])
    assert "read_file" in message
    assert "limit" in message
    # 必须告诉模型下一步做什么，否则它只会原样重发。
    assert "again" in message


# ── 不确定的不要误伤 ─────────────────────────────────────────────────


def test_extra_properties_are_allowed():
    """很多 handler 接受 **kwargs 或忽略多余键；一律拒绝会把能跑的调用判死。"""
    params, errors = prepare_params(
        "read_file", {"path": "a.py", "unexpected": 1}, READ_SCHEMA
    )
    assert errors == []
    assert params["unexpected"] == 1


def test_additional_properties_false_is_honoured():
    """schema 显式关闭额外字段时才拒绝。"""
    schema = {**READ_SCHEMA, "additionalProperties": False}
    _, errors = prepare_params("read_file", {"path": "a.py", "nope": 1}, schema)
    assert errors


def test_unusable_schema_does_not_block_execution():
    """95 个工具里任何一个手写 schema 有笔误，都不该让该工具彻底不可用。"""
    broken = {"type": "object", "properties": {"a": {"type": "not-a-type"}}}
    params, errors = prepare_params("demo", {"a": 1}, broken)
    assert errors == []
    assert params == {"a": 1}


def test_empty_schema_passes_everything():
    params, errors = prepare_params("demo", {"anything": "goes"}, {})
    assert errors == []
    assert params == {"anything": "goes"}


def test_non_dict_params_are_rejected():
    _, errors = prepare_params("demo", ["not", "a", "dict"], READ_SCHEMA)  # type: ignore[arg-type]
    assert errors


# ── registry.execute 集成 ────────────────────────────────────────────


async def test_execute_coerces_before_calling_handler():
    calls = []

    async def handler(path, limit=10):
        calls.append((path, limit, type(limit).__name__))
        return {"ok": True}

    reg = ToolRegistry()
    reg.register("demo", "d", READ_SCHEMA, handler)
    result = await reg.execute("demo", {"path": "a.py", "limit": "5"})
    assert result == {"ok": True}
    assert calls == [("a.py", 5, "int")]


async def test_execute_does_not_call_handler_on_invalid_args():
    """关键契约：参数无效时 handler **一次都不能被调用**。

    以前 `handler(**params)` 会先执行再抛 TypeError —— 对有副作用的工具（写文件、
    执行命令）来说，这意味着"参数错了但副作用已经发生"。
    """
    calls = []

    async def handler(path):
        calls.append(path)
        return {"ok": True}

    reg = ToolRegistry()
    reg.register("demo", "d", READ_SCHEMA, handler)
    result = await reg.execute("demo", {})
    assert calls == []
    assert "invalid_arguments" in result
    assert "path" in result["error"]


async def test_execute_still_returns_plain_errors_from_handler():
    """handler 自身的业务错误仍按原样返回，不被校验层改写。"""

    async def handler(path):
        return {"error": "file not found"}

    reg = ToolRegistry()
    reg.register("demo", "d", READ_SCHEMA, handler)
    assert await reg.execute("demo", {"path": "nope"}) == {"error": "file not found"}


async def test_execute_unregistered_tool_unchanged():
    reg = ToolRegistry()
    assert "not found" in (await reg.execute("ghost", {}))["error"]


@pytest.mark.parametrize(
    "params, expect_ok",
    [
        ({"path": "a.py"}, True),
        ({"path": "a.py", "limit": 100}, True),
        ({"path": "a.py", "limit": "100"}, True),
        ({"limit": 100}, False),
        ({"path": None}, False),
    ],
)
async def test_execute_accepts_real_world_shapes(params, expect_ok):
    async def handler(path=None, limit=10):
        return {"ok": True}

    reg = ToolRegistry()
    reg.register("demo", "d", READ_SCHEMA, handler)
    result = await reg.execute("demo", params)
    assert ("ok" in result) is expect_ok
