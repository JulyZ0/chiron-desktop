"""工具参数的**校验与轻量归一**（G-T1）。

对照实现：pi `packages/ai/src/utils/validation.ts:54` 的 `validateToolArguments`
（编译 schema + coerce + 按点路径报错）。

## 为什么需要这一层

此前 `ToolRegistry.execute` 直接 `handler(**params)` —— 模型给什么就传什么。
模型把 `{"limit": 50}` 写成 `{"limit": "50"}` 时，`limit` 是字符串，切片运算要么
抛 `TypeError`、要么切出诡异结果；漏掉必填字段时抛的是 `TypeError: read_file()
missing 1 required positional argument`。两者都被 `except Exception` 兜成一句
`str(e)` 回给模型 —— 模型**读不出该改哪个字段**，只能原样重试，于是变成
"工具失败后不会自动修复"。

这一层要做三件事，缺一不可：

1. **coerce（归一）**：`"50"` → `50`、`"true"` → `True`、JSON 字符串 → dict。
   模型给错类型是高频且**可自动修复**的，直接报错等于把可救的调用判死。
2. **校验**：coerce 之后仍不合 schema 才是真错误。用 JSON Schema 判，而不是靠
   把 `params` 强塞进 `handler` 的签名、让它抛 `TypeError` —— 后者的错误信息
   对模型毫无指向性。
3. **点路径报错**：`limit: expected integer, got string "abc"`。模型据此知道
   改哪一个字段。pi 的同类实现同样按点路径报错，这是它能自我修复的前提。

## 边界（刻意不做的事）

- **不禁止额外字段**：除非 schema 显式写了 `additionalProperties: false`。
  很多工具 handler 接受 `**kwargs` 或忽略多余键，一律拒绝会把能跑的调用判死。
- **只 coerce「字符串 → 声明类型」这一个方向**。`{"limit": 50}` 遇到 `type:
  string` 时不转成 `"50"` —— 那会把"传错了"掩盖成"传对了"，而模型本可以用
  正确的类型重发。
- **schema 自身不可用时不阻断执行**：注册表里 95 个工具，任何一个手写 schema 有
  笔误都不该让该工具彻底不可用。编译失败只记日志并放行。
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单条参数错误的上限。一次性把几十条错误塞回上下文只会把真正要改的那条淹掉。
MAX_REPORTED_ERRORS = 5


def _pointer(error: Any) -> str:
    """把 jsonschema 的 `absolute_path` 转成 `a.b[0].c` 形状的点路径。"""
    parts: list[str] = []
    for segment in error.absolute_path:
        if isinstance(segment, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{segment}]"
            else:
                parts.append(f"[{segment}]")
        else:
            parts.append(str(segment))
    return ".".join(parts)


def _missing_properties(error: Any) -> list[str]:
    """`required` 校验失败时取出缺了哪些字段。

    jsonschema 的 `required` 错误把 `absolute_path` 指向**父对象**，字段名只在
    消息文本里。不抽出来就没法拼成 `path.content` 这种可操作的路径 —— 而模型要
    的正是"缺哪个字段"。
    """
    names = getattr(error, "validator_value", None)
    instance = getattr(error, "instance", None)
    if not isinstance(names, list) or not isinstance(instance, dict):
        return []
    return [str(n) for n in names if isinstance(str(n), str) and n not in instance]


def describe_error(error: Any) -> str:
    """把一条 jsonschema 错误转成模型可操作的一句话。"""
    parent = _pointer(error)
    if error.validator == "required":
        missing = _missing_properties(error)
        if missing:
            return ", ".join(
                f"{parent + '.' if parent else ''}{name}: required field is missing"
                for name in missing
            )
    location = parent or "<arguments>"
    return f"{location}: {error.message}"


def _coerce_value(value: Any, target: Any) -> Any:
    """按 schema 声明的类型做**单向**归一：字符串 → 声明类型。

    只处理"模型把 JSON 标量写成字符串"这一种高频偏差。反方向（数字 → 字符串）
    不做 —— 那会掩盖真实错误。
    """
    if not isinstance(value, str):
        return value
    if target == "integer":
        text = value.strip()
        # 拒绝 "50.0" / "1e3" 这类：`int("50.0")` 会抛，而静默走 float 又会把
        # 声明为 integer 的字段变成浮点。留着让它报类型错误更诚实。
        if text and (text.lstrip("+-").isdigit()):
            try:
                return int(text)
            except ValueError:
                return value
        return value
    if target == "number":
        text = value.strip()
        if not text:
            return value
        try:
            return float(text)
        except ValueError:
            return value
    if target == "boolean":
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
        return value
    if target in ("object", "array"):
        text = value.strip()
        # 模型偶尔把结构化参数整个序列化成字符串再塞进一个字段。
        if not text or text[0] not in ("{", "["):
            return value
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return value
        expected = dict if target == "object" else list
        return parsed if isinstance(parsed, expected) else value
    return value


def _declared_type(schema: Any) -> Any:
    """取 `type` 声明；`["integer", "null"]` 这种取第一个非 null 的。"""
    if not isinstance(schema, dict):
        return None
    declared = schema.get("type")
    if isinstance(declared, list):
        for item in declared:
            if item != "null":
                return item
        return None
    return declared if isinstance(declared, str) else None


def coerce_params(params: dict[str, Any], schema: Any) -> dict[str, Any]:
    """按 schema 递归归一 `params`。不修改入参，返回新 dict。"""
    if not isinstance(schema, dict) or not isinstance(params, dict):
        return params
    properties = schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return params
    # JSON Schema 的 `additionalProperties: {schema}` 形式：未声明字段按它归一。
    additional = schema.get("additionalProperties")
    coerced: dict[str, Any] = {}
    for key, value in params.items():
        spec = properties.get(key)
        if spec is None:
            coerced[key] = _coerce_value(value, _declared_type(additional))
            continue
        expected = _declared_type(spec)
        value = _coerce_value(value, expected)
        # 只在目标确实是 object 时才下钻，避免把本该报错的嵌套类型悄悄改掉。
        if expected == "object" and isinstance(value, dict):
            value = coerce_params(value, spec)
        elif expected == "array" and isinstance(value, list):
            items = spec.get("items")
            if isinstance(items, dict):
                item_type = _declared_type(items)
                value = [
                    coerce_params(item, items)
                    if item_type == "object" and isinstance(item, dict)
                    else _coerce_value(item, item_type)
                    for item in value
                ]
        coerced[key] = value
    return coerced


def validate_params(params: dict[str, Any], schema: Any) -> list[str]:
    """校验已归一的 `params`，返回可读错误列表（空 = 通过）。

    schema 不可用时返回空列表（放行）并记日志 —— 见模块文档"边界"一节。
    """
    if not isinstance(schema, dict) or not schema:
        return []
    if schema.get("type") not in ("object", None):
        # 非 object 的工具参数形状不受支持，放行而不是判死。
        return []
    try:
        from jsonschema import Draft7Validator
        from jsonschema.exceptions import SchemaError

        validator = Draft7Validator(schema)
    except SchemaError as exc:
        logger.warning("tool parameter schema is invalid, skipping validation: %s", exc)
        return []
    except Exception as exc:  # noqa: BLE001 - 校验器不可用不该让工具不可用
        logger.warning("could not build schema validator, skipping validation: %s", exc)
        return []

    errors: list[str] = []
    # `iter_errors` 是生成器：schema 里的**未定义类型名**（`"type": "not-a-type"`）要等到
    # 真正迭代到那一条时才抛 `UnknownType`，构造 validator 时完全看不出来。因此迭代本身
    # 也必须包在 try 里 —— 漏了这一层，一个手写 schema 的笔误会让该工具在调用时崩掉，
    # 而不是像模块文档承诺的那样"放行"。
    try:
        for error in sorted(
            validator.iter_errors(params), key=lambda e: list(e.absolute_path)
        ):
            described = describe_error(error)
            if described and described not in errors:
                errors.append(described)
            if len(errors) >= MAX_REPORTED_ERRORS:
                break
    except Exception as exc:  # noqa: BLE001 - 校验器不可用不该让工具不可用
        logger.warning("schema validation aborted, skipping validation: %s", exc)
        return []
    return errors


def prepare_params(
    tool_name: str, params: dict[str, Any], schema: Any
) -> tuple[dict[str, Any], list[str]]:
    """归一 + 校验一步到位，返回 `(可用参数, 错误列表)`。

    错误列表非空时调用方**不应**执行 handler —— 但应当把 `errors` 按结构化形式
    回给模型（见 `ToolRegistry.execute`），让它知道改哪个字段。
    """
    if not isinstance(params, dict):
        return {}, ["arguments: expected a JSON object"]
    normalized = coerce_params(params, schema)
    return normalized, validate_params(normalized, schema)


def format_errors(tool_name: str, errors: list[str]) -> str:
    """把错误列表拼成回给模型的一句话。带工具名，便于模型确认改对了对象。"""
    if not errors:
        return ""
    joined = "; ".join(errors)
    return (
        f"Invalid arguments for tool '{tool_name}': {joined}. "
        "Fix these fields and call the tool again."
    )
