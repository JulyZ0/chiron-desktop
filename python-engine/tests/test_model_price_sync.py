"""M28：按模型的公开价格 —— 解析、归一化、交叉验证。

三条不变量：

1. **单位统一到 USD / 1000 tokens**：各源单位不同（portkey = 美分/token；llmrates = 每百万
   token + 原生货币），换算只在各自的解析函数里做一次；
2. **至少两个源一致（相对差 ≤ 5%）才收**：单源不写入、分歧不写入 —— 宁可缺数据，也不要
   一个来源不明或自相矛盾的数字；
3. **非 USD 跳过**（本仓不做汇率换算，价格表也不参与计费，没有必须换算的压力）。

payload 片段取自两个源的真实响应（2026-10-22 实测）。
"""
from __future__ import annotations

import pytest

from app.pricing import sync as s

# ── 解析：真实 payload 的形状 ──


def test_portkey_cents_per_token_becomes_usd_per_1k() -> None:
    """portkey 的单位是**美分 / token**（其文档自陈）⇒ USD/1k = 美分 × 10。"""
    payload = {
        "gpt-4o": {
            "pricing_config": {
                "pay_as_you_go": {
                    "request_token": {"price": 0.00025},   # 美分/token ⇒ $2.5/1M
                    "response_token": {"price": 0.001},
                }
            }
        }
    }

    prices = s.parse_portkey(payload)

    assert prices["gpt-4o"].input_per_1k == pytest.approx(0.0025)
    assert prices["gpt-4o"].output_per_1k == pytest.approx(0.01)


def test_llmrates_per_million_becomes_usd_per_1k() -> None:
    payload = {
        "models": [
            {
                "name": "gpt-4o",
                "provider": {"slug": "openai"},
                "prices": [{"inputPricePerMillion": 2.5, "outputPricePerMillion": 10, "priceUnit": "USD"}],
            }
        ]
    }

    prices = s.parse_llmrates(payload)

    assert prices["gpt-4o"].input_per_1k == pytest.approx(0.0025)
    assert prices["gpt-4o"].output_per_1k == pytest.approx(0.01)


def test_llmrates_skips_non_usd() -> None:
    """非 USD 一律跳过（不做汇率换算）—— 否则价格表里会混进"看起来是美元"的人民币。"""
    payload = {
        "models": [
            {
                "name": "glm-4v",
                "provider": {"slug": "zhipu"},
                "prices": [{"inputPricePerMillion": 10, "outputPricePerMillion": 10, "priceUnit": "CNY"}],
            }
        ]
    }

    assert s.parse_llmrates(payload) == {}


def test_model_name_normalization_drops_date_suffix() -> None:
    assert s.normalize_model("GPT-4o-2024-11-20") == "gpt-4o"
    assert s.normalize_model("claude_3.5_sonnet") == "claude-3-5-sonnet"


# ── 交叉验证 ──


def _price(model: str, inp: float, outp: float) -> s.ModelPrice:
    return s.ModelPrice(model=model, provider="openai", input_per_1k=inp, output_per_1k=outp)


def test_two_sources_within_tolerance_agree() -> None:
    agreed, disagreements = s.cross_validate(
        {"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}, "b": {"gpt-4o": _price("gpt-4o", 0.0026, 0.0101)}}
    )

    assert [p.model for p in agreed] == ["gpt-4o"]
    assert disagreements == []


def test_disagreement_is_not_written_but_is_reported() -> None:
    """分歧**不写入**，但**不静默** —— 否则"价格表一直是空的"没人知道为什么。"""
    agreed, disagreements = s.cross_validate(
        {"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}, "b": {"gpt-4o": _price("gpt-4o", 0.05, 0.01)}}
    )

    assert agreed == []
    assert len(disagreements) == 1 and "gpt-4o" in disagreements[0]


def test_output_axis_alone_is_enough_to_disagree() -> None:
    """两轴都要比：只比输入会让"输出价差一倍"溜过去。"""
    agreed, _ = s.cross_validate(
        {"a": {"m": _price("m", 0.0025, 0.01)}, "b": {"m": _price("m", 0.0025, 0.5)}}
    )

    assert agreed == []


def test_single_source_is_never_written() -> None:
    """只有一个源 ⇒ 判未验证、不写入（单源无法交叉验证）。"""
    agreed, disagreements = s.cross_validate({"a": {"gpt-4o": _price("gpt-4o", 0.0025, 0.01)}})

    assert agreed == []
    assert disagreements == []


def test_model_names_that_do_not_align_become_single_source() -> None:
    """两个源对同一个模型的叫法差得远 ⇒ 对齐不上 ⇒ 各自单源 ⇒ 都不写入。

    这是**有意的保守**：拿两个不同模型的价格互相"验证"比没有数据更糟。
    """
    agreed, _ = s.cross_validate(
        {"a": {"glm-4v": _price("glm-4v", 0.01, 0.01)}, "b": {"glm-4-vision": _price("glm-4-vision", 0.01, 0.01)}}
    )

    assert agreed == []


# ── 开关 ──


def test_sync_is_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认**开**：默认不产生任何数据的机制等于没接（价格表会一直空）。"""
    monkeypatch.delenv("MODEL_PRICE_SYNC", raising=False)

    assert s.sync_enabled() is True


def test_sync_can_be_disabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """需要彻底禁用的部署显式设 off/0/false/no 才关。"""
    for raw in ("0", "off", "false", "no", "OFF", " False "):
        monkeypatch.setenv("MODEL_PRICE_SYNC", raw)
        assert s.sync_enabled() is False, raw


def test_interval_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS", "abc")
    assert s.sync_interval_seconds() == s.DEFAULT_INTERVAL_SECONDS

    monkeypatch.setenv("MODEL_PRICE_SYNC_INTERVAL_SECONDS", "5")  # 太小 ⇒ 回落
    assert s.sync_interval_seconds() == s.DEFAULT_INTERVAL_SECONDS


async def test_start_price_sync_returns_immediately_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显式关闭 ⇒ 调用方可以无条件 await 它，不必自己判开关。"""
    monkeypatch.setenv("MODEL_PRICE_SYNC", "off")

    await s.start_price_sync()  # 不会挂起


async def test_sync_once_writes_nothing_when_no_source_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所有源都拉不到 ⇒ 一行都不写，且**不抛**（一轮失败不该让循环死掉）。"""

    async def _dead(_name: str, _url: str, **_kw: object) -> None:
        return None

    monkeypatch.setattr(s, "_fetch_source", _dead)
    written, disagreements = await s.sync_once()

    assert written == 0 and disagreements == []


# ── PriceToken（2026-11-06 实测响应形状）──


def test_pricetoken_per_million_becomes_usd_per_1k() -> None:
    """字段名是 ``*PerMTok`` ⇒ USD/1k = per_million / 1000。"""
    payload = {
        "data": [
            {
                "modelId": "claude-haiku-4-5-20251001",
                "provider": "anthropic",
                "inputPerMTok": 1,
                "outputPerMTok": 5,
                "status": "active",
            }
        ]
    }

    prices = s.parse_pricetoken(payload)

    key = s.normalize_model("claude-haiku-4-5-20251001")
    assert prices[key].input_per_1k == pytest.approx(0.001)
    assert prices[key].output_per_1k == pytest.approx(0.005)
    assert prices[key].provider == "anthropic"


def test_pricetoken_skips_non_active_models() -> None:
    """退役/预览模型不写进价格表：否则界面会显示一个已经不能用的模型的价格。"""
    payload = {
        "data": [
            {"modelId": "old-model", "provider": "openai", "inputPerMTok": 1,
             "outputPerMTok": 2, "status": "deprecated"},
            {"modelId": "new-model", "provider": "openai", "inputPerMTok": 1,
             "outputPerMTok": 2, "status": "active"},
        ]
    }

    prices = s.parse_pricetoken(payload)

    assert s.normalize_model("old-model") not in prices
    assert s.normalize_model("new-model") in prices


# ── Portkey 多厂商文件：provider 必须跟着文件走 ──


def test_portkey_provider_follows_the_file() -> None:
    """同一个解析器跑 4 份厂商文件；provider 若写死 "openai"，
    写进 `model_prices` 的 anthropic 模型会全部被标成 openai。"""
    payload = {
        "claude-3-5-sonnet-20241022": {
            "pricing_config": {
                "pay_as_you_go": {
                    "request_token": {"price": 0.0003},
                    "response_token": {"price": 0.0015},
                }
            }
        }
    }

    prices = s.parse_portkey(payload, provider="anthropic")

    key = s.normalize_model("claude-3-5-sonnet-20241022")
    assert prices[key].provider == "anthropic"


def test_portkey_skips_all_zero_placeholder() -> None:
    """各厂商文件都有一个 ``"default"`` 全零占位键，必须被跳过。"""
    payload = {
        "default": {
            "pricing_config": {
                "pay_as_you_go": {"request_token": {"price": 0}, "response_token": {"price": 0}}
            }
        }
    }

    assert s.parse_portkey(payload) == {}


# ── 源目录：同一解析器的多份文件不能互相覆盖 ──


async def test_sync_once_keeps_every_portkey_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """`portkey` 在源目录里出现多次（按厂商分文件）。

    若用解析器名做 `fetched` 的键，四次拉取会互相覆盖 —— 表现是"价格表里只剩最后一个
    厂商"。这里断言各厂商的模型都进到了交叉验证的输入里。
    """
    seen_urls: list[str] = []

    async def _fake(name: str, url: str, *, provider: str = "", **_kw: object):
        seen_urls.append(url)
        if name != "portkey" or not provider:
            return None
        model = f"{provider}-only"
        return {model: _price(model, 0.001, 0.002)}

    captured: dict[str, object] = {}

    def _fake_cross(per_source, **_kw):
        captured["keys"] = sorted(per_source)
        return [], []

    monkeypatch.setattr(s, "_fetch_source", _fake)
    monkeypatch.setattr(s, "cross_validate", _fake_cross)

    await s.sync_once()

    keys = captured["keys"]
    # 4 个 portkey 文件各自一个唯一键（解析器名:provider）
    assert keys == ["portkey:anthropic", "portkey:deepseek", "portkey:google", "portkey:openai"]


# ── 写库：`get_pool()` 是同步函数，不能 await ──


async def test_upsert_writes_rows_with_a_synchronous_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ `_upsert` 必须能真的写进去 —— `get_pool()` 是**同步**函数。

    缺陷形态（实测）：曾写成 `await get_pool()`。`app.db.get_pool` 的签名是
    ``def get_pool() -> asyncpg.Pool``，await 一个 Pool 必然抛
    ``TypeError: object Pool can't be used in 'await' expression``，随后被
    `_upsert` 的 ``except Exception`` 记成 ``write_failed`` 并返回 0。

    后果：**每一轮同步都写 0 行**，`model_prices` 永远是空表 ⇒ 计费页的「花费」
    恒为 0（`usageCostCents` 取不到单价就写 NULL）。而那条告警文案说的是
    "请执行迁移 0029"，把排查方向引向根本不存在的问题。

    这条用例只钉"同步池能被正常使用"：不连真库，用一个鸭子类型的假池。
    """
    executed: list[str] = []

    class _FakeConn:
        async def execute(self, *_a: object, **_kw: object) -> str:
            executed.append("insert")
            return "INSERT 0 1"

    class _FakeAcquire:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    class _FakePool:
        def acquire(self) -> _FakeAcquire:
            return _FakeAcquire()

    # 关键：**同步**函数，与 app.db.get_pool 的签名一致（不是 coroutine function）。
    def _sync_get_pool() -> _FakePool:
        return _FakePool()

    import app.db as db_module

    monkeypatch.setattr(db_module, "get_pool", _sync_get_pool)

    written = await s._upsert(
        [_price("deepseek-chat", 0.0001, 0.0003)], sources=["portkey", "llmrates"]
    )

    assert written == 1, (
        "写入行数为 0 —— 说明池被当成 awaitable 用了（await get_pool() 是 bug），"
        "或写入路径被异常吞掉"
    )
    assert executed, "conn.execute 从没被调用"


async def test_upsert_tolerates_uninitialized_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """池还没建好时（引擎刚启动、同步循环先跑）返回 0，**不抛**。

    `get_pool()` 在未初始化时是 ``raise RuntimeError``，不是返回 None。
    不接住它就会让这一轮同步以未捕获异常结束 —— 循环虽被上层兜住，但日志会
    变成一条看不懂的 traceback，把"池没就绪"这个真实原因埋掉。
    """
    import app.db as db_module

    def _raise() -> object:
        raise RuntimeError("PostgreSQL pool not initialized")

    monkeypatch.setattr(db_module, "get_pool", _raise)

    written = await s._upsert([_price("m", 0.001, 0.002)], sources=["a", "b"])

    assert written == 0



# ── 源健康：失败要**脱敏**且可展示 ──


def test_sanitize_reason_strips_query_secrets_and_keeps_host() -> None:
    """异常文本里的 URL 只保留 scheme://host：路径/查询串可能含一次性凭证。

    实测的失败形态是 httpx 把完整 URL 带出来（`?api_key=...`），而这张表是**所有人
    可见**的运营面板 —— 直接把 `str(exc)` 落库等于把凭证复制到更松的地方。
    """
    raw = (
        "HTTPStatusError: 401 for url 'https://pricetoken.ai/api/v1/text"
        "?api_key=sk-live-123&x=1'"
    )

    cleaned = s.sanitize_reason(raw)

    assert "sk-live-123" not in cleaned
    assert "api_key" not in cleaned
    assert "pricetoken.ai" in cleaned
    # 保留错误类型，否则面板只剩一句"失败了"
    assert "401" in cleaned


def test_sanitize_reason_truncates_to_200() -> None:
    """截到 200 字符：这里是展示字段，不是排障全文（完整堆栈在进程日志）。"""
    cleaned = s.sanitize_reason("x" * 1000)

    assert len(cleaned) == s.ERROR_MAX_CHARS
    assert cleaned.endswith("…")


def test_build_source_status_three_states() -> None:
    """``ok`` / ``empty`` / ``failed`` 必须可分：把"源活着但没匹配"报成"不可达"会误导排查。"""
    ok = s.build_source_status(
        parser="pricetoken", provider="", url="https://pricetoken.ai/api/v1/text",
        rows=12, duration_ms=340,
    )
    empty = s.build_source_status(
        parser="llmrates", provider="", url="https://www.llmrates.ai/api/dataset",
        rows=0, duration_ms=90,
    )
    failed = s.build_source_status(
        parser="pricetoken", provider="", url="https://pricetoken.ai/api/v1/text",
        rows=0, duration_ms=800, error="ReadTimeout: timed out",
    )

    assert (ok.status, ok.source_key, ok.url_host) == ("ok", "pricetoken", "pricetoken.ai")
    assert empty.status == "empty"
    assert failed.status == "failed" and "timed out" in failed.error


def test_build_source_status_keeps_portkey_files_apart() -> None:
    """同一解析器的多份厂商文件必须是**不同的键**，否则健康面板只剩最后一个源。"""
    a = s.build_source_status(
        parser="portkey", provider="openai", url="https://configs.portkey.ai/pricing/openai.json",
        rows=3, duration_ms=10,
    )
    b = s.build_source_status(
        parser="portkey", provider="anthropic", url="https://configs.portkey.ai/pricing/anthropic.json",
        rows=4, duration_ms=10,
    )

    assert a.source_key == "portkey:openai"
    assert b.source_key == "portkey:anthropic"


async def test_write_source_status_upserts_and_prunes_removed_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """诊断写入：一源一行 upsert，并删除已不在源目录里的旧键。

    不删旧键的后果是面板永远显示一个"上次失败"的源 —— 而它已经不在目录里了。
    """
    executed: list[tuple[str, tuple[object, ...]]] = []

    class _FakeConn:
        async def execute(self, sql: str, *args: object) -> str:
            executed.append((sql, args))
            return "OK"

    class _FakeAcquire:
        async def __aenter__(self) -> _FakeConn:
            return _FakeConn()

        async def __aexit__(self, *_exc: object) -> bool:
            return False

    class _FakePool:
        def acquire(self) -> _FakeAcquire:
            return _FakeAcquire()

    import app.db as db_module

    monkeypatch.setattr(db_module, "get_pool", lambda: _FakePool())

    await s._write_source_status([
        s.build_source_status(
            parser="pricetoken", provider="", url="https://pricetoken.ai/api/v1/text",
            rows=0, duration_ms=800, error="ReadTimeout: timed out",
        )
    ])

    inserts = [e for e in executed if "INSERT INTO price_source_status" in e[0]]
    deletes = [e for e in executed if e[0].startswith("DELETE FROM price_source_status")]
    assert len(inserts) == 1
    # INSERT 参数顺序：source_key, parser, provider, url_host, status, rows, duration_ms, error
    assert inserts[0][1][0] == "pricetoken"
    assert inserts[0][1][4] == "failed"
    assert len(deletes) == 1 and deletes[0][1][0] == ["pricetoken"]


async def test_write_source_status_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """诊断写入是旁路：池坏了也只记 debug，不能把同步结果变成异常。"""
    import app.db as db_module

    def _raise() -> object:
        raise RuntimeError("PostgreSQL pool not initialized")

    monkeypatch.setattr(db_module, "get_pool", _raise)

    # 不抛即通过
    await s._write_source_status([
        s.build_source_status(
            parser="llmrates", provider="", url="https://www.llmrates.ai/api/dataset",
            rows=0, duration_ms=1,
        )
    ])
