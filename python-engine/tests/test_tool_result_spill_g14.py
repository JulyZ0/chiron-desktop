"""G14：工具结果的**统一 spill 出口**（`registry.execute` 唯一出口）。

## 修前会怎样

`ToolRegistry.execute` 把 handler 的结果**原样**返回，"大结果怎么处理"因此散落在各工具
实现里：`read_file` 自己截行、`grep_files` 自己卡命中数、`shell_exec` 整段回吐……
同一个"结果过大"在不同工具上有不同行为，模型无从预期，测试也没法统一断言。

## 修后是什么

出口统一过一遍 `app.tools.spill.apply_spill`：

1. 小结果原样返回，**不落盘**（小结果不该为"可能用得上"付存储成本）；
2. 超过阈值 → 原文落进**既有** `app/agent/result_store`（与 runtime 的
   `_compact_tool_result` 同一套存储），回给模型的是**头 + 尾 + locator**；
3. locator 必须能被**既有** `read_tool_result` 分段取回（不另造取回机制）；
4. 落盘失败时**保留原文** —— 存储是锦上添花，不能因为它挂了就把工具结果变成空或半截。

本文件按"修前失败"的顺序写的：`apply_spill` 与出口接线都不存在时，四条用例全红。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tools.registry import ToolRegistry

#: 大结果：首尾各有一个可断言的标记，中段 40000 个 M 是"必须被裁掉"的部分。
BIG = "HEAD|" + ("M" * 40000) + "|TAIL"


def _register(reg: ToolRegistry, name: str, handler) -> None:  # type: ignore[no-untyped-def]
    reg.register(
        name=name,
        description="",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )


class _FakeStore:
    """内存版 result_store（真实实现在 Redis 上，单测不应依赖 Redis 可用）。"""

    def __init__(self) -> None:
        self.saved: dict[str, str] = {}
        self.store_calls = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.agent import result_store

        async def _store(session_id: str, ref: str, text: str) -> bool:
            self.store_calls += 1
            self.saved[ref] = text
            return True

        async def _load(
            session_id: str, ref: str, *, offset: int = 0, limit: int = 0
        ) -> str | None:
            text = self.saved.get(ref)
            if text is None:
                return None
            if offset or limit:
                return text[offset : offset + limit if limit else None]
            return text

        monkeypatch.setattr(result_store, "store", _store)
        monkeypatch.setattr(result_store, "load", _load)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    fake.install(monkeypatch)
    return fake


@pytest.mark.asyncio
async def test_small_result_passes_through_and_is_not_persisted(store: _FakeStore) -> None:
    """① 小结果：原样返回，且**一次都没有落盘**。"""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"output": "ok", "items": [1, 2, 3], "exit_code": 0}

    reg = ToolRegistry()
    _register(reg, "fixture_small", handler)

    out = await reg.execute("fixture_small", {})

    assert out == {"output": "ok", "items": [1, 2, 3], "exit_code": 0}
    assert "result_ref" not in out
    assert store.store_calls == 0, "小结果不该落盘"


@pytest.mark.asyncio
async def test_large_result_is_spilled_and_clipped_to_head_tail(store: _FakeStore) -> None:
    """② 大结果：落盘 + 回给模型的内容被裁成头尾 + locator。"""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        # 结构信息（exit_code）必须保留：裁的只能是过大的**内容**
        return {"output": BIG, "exit_code": 0}

    reg = ToolRegistry()
    _register(reg, "fixture_big", handler)

    out = await reg.execute("fixture_big", {})

    clipped = out["output"]
    assert store.store_calls == 1
    assert out["exit_code"] == 0, "spill 不得抹掉同级的结构化字段"
    assert out["result_ref"].startswith("sp_")
    assert store.saved[out["result_ref"]] == BIG, "落盘的必须是完整原文"

    assert len(clipped) < len(BIG)
    assert "HEAD|" in clipped, "头部必须保留"
    assert "|TAIL" in clipped, "尾部必须保留（错误行往往在末尾）"
    assert "M" * 20000 not in clipped, "中段必须被裁掉"
    assert 'result_ref="' + out["result_ref"] in clipped, "被裁处必须带 locator"
    assert "read_tool_result" in clipped, "必须告诉模型怎么取回原文"


@pytest.mark.asyncio
async def test_locator_is_readable_through_existing_read_tool_result(
    store: _FakeStore,
) -> None:
    """③ locator 能被**既有** `read_tool_result` 分段取回全文（不另造取回机制）。"""

    from app.tools.tool_result import DEFAULT_LIMIT, read_tool_result

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"output": BIG}

    reg = ToolRegistry()
    _register(reg, "fixture_readback", handler)
    out = await reg.execute("fixture_readback", {})
    ref = out["result_ref"]

    first = await read_tool_result(result_ref=ref)
    assert first["content"] == BIG[:DEFAULT_LIMIT]
    assert first["returned_chars"] == DEFAULT_LIMIT

    second = await read_tool_result(result_ref=ref, offset=DEFAULT_LIMIT)
    assert second["content"] == BIG[DEFAULT_LIMIT : 2 * DEFAULT_LIMIT]

    # 拼回去等于原文 —— "分段取回"确实能还原全部内容
    assert first["content"] + second["content"] == BIG[: 2 * DEFAULT_LIMIT]


@pytest.mark.asyncio
async def test_store_failure_keeps_the_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """④ 落盘失败（返回 False）→ **保留原文**，不能变成空/半截。"""

    from app.agent import result_store

    attempts = {"n": 0}

    async def _store_refusing(session_id: str, ref: str, text: str) -> bool:
        attempts["n"] += 1
        return False

    monkeypatch.setattr(result_store, "store", _store_refusing)

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"output": BIG, "exit_code": 0}

    reg = ToolRegistry()
    _register(reg, "fixture_store_down", handler)

    out = await reg.execute("fixture_store_down", {})

    assert attempts["n"] == 1, "超过阈值就该尝试落盘（修前根本没这条路径）"
    assert out["output"] == BIG, "存储不可用时必须原样回吐，细节一个字都不能丢"
    assert "result_ref" not in out, "没有落盘就不该给一个取不回的 locator"
    assert out["exit_code"] == 0


@pytest.mark.asyncio
async def test_store_exception_keeps_the_original_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """④-补：落盘**抛异常**同样不能吃掉结果（出口 fail-open 到原文）。"""

    from app.agent import result_store

    attempts = {"n": 0}

    async def _store_boom(session_id: str, ref: str, text: str) -> bool:
        attempts["n"] += 1
        raise RuntimeError("redis down")

    monkeypatch.setattr(result_store, "store", _store_boom)

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"output": BIG}

    reg = ToolRegistry()
    _register(reg, "fixture_store_boom", handler)

    out = await reg.execute("fixture_store_boom", {})

    assert attempts["n"] == 1
    assert out["output"] == BIG
    assert "result_ref" not in out


@pytest.mark.asyncio
async def test_error_results_are_never_spilled(store: _FakeStore) -> None:
    """错误文案是**给模型的指令**，裁掉中段等于让它照着半句话去改。"""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"error": BIG}

    reg = ToolRegistry()
    _register(reg, "fixture_error", handler)

    out = await reg.execute("fixture_error", {})

    assert out["error"] == BIG
    assert store.store_calls == 0


@pytest.mark.asyncio
async def test_exit_guard_keeps_result_when_spill_itself_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """出口层的最后保险：spill 自己抛异常时也**原样回吐**结果，而不是变成 error。"""

    import importlib

    # 注意：`import app.tools.registry as m` 拿到的是**包属性** registry（ToolRegistry 实例），
    # 不是模块 —— 必须显式取模块对象才能 monkeypatch 模块级名字。
    registry_mod = importlib.import_module("app.tools.registry")

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("spill exploded")

    monkeypatch.setattr(registry_mod, "apply_spill", _boom)

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"output": BIG}

    reg = ToolRegistry()
    _register(reg, "fixture_spill_boom", handler)

    out = await reg.execute("fixture_spill_boom", {})

    assert out == {"output": BIG}
    assert "error" not in out


@pytest.mark.asyncio
async def test_tool_owned_result_ref_is_not_overwritten(store: _FakeStore) -> None:
    """工具自己的 `result_ref`（如 subagent 的 run_id）不能被 spill 覆盖。

    顶部 `result_ref` 是既有约定名，有些工具自己就在用。覆盖它等于把另一个工具的引用
    悄悄改坏 —— locator 因此另有去处（`spill.result_ref` 与被裁正文的尾注）。
    """

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {"result_ref": "run_abc123", "output": BIG}

    reg = ToolRegistry()
    _register(reg, "fixture_owner_ref", handler)

    out = await reg.execute("fixture_owner_ref", {})

    assert out["result_ref"] == "run_abc123", "工具自带的引用不得被改写"
    assert out["spill"]["result_ref"].startswith("sp_")
    assert store.saved[out["spill"]["result_ref"]] == BIG
    assert out["spill"]["count"] == 1
    assert out["spill"]["fields"][0]["field"] == "output"


@pytest.mark.asyncio
async def test_nested_payload_strings_are_spilled_by_field(store: _FakeStore) -> None:
    """嵌套结构只裁"过大的那个字符串"，同级的其它字段（含小字符串）保持原样。"""

    async def handler(**kwargs: Any) -> dict[str, Any]:
        return {
            "stdout": "ok",
            "details": {"log": BIG, "exit_code": 0},
        }

    reg = ToolRegistry()
    _register(reg, "fixture_nested", handler)

    out = await reg.execute("fixture_nested", {})

    assert out["stdout"] == "ok"
    assert out["details"]["exit_code"] == 0
    assert len(out["details"]["log"]) < len(BIG)
    assert out["spill"]["fields"][0]["field"] == "details.log"

