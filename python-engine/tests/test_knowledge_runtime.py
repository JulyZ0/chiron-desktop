"""``app/knowledge/runtime.py`` —— 索引服务地址的运行时解析与热切换。

这一层的两个"会静默断服务"的地方是本文件的重点：

1. **坏配置不能把正在服务的地址打掉**。后台保存的配置可能来自旧版本、或被手工改坏；
   ``apply`` 遇到不认识的内容必须**保持现状**，而不是回落成空地址 —— 那会让所有
   索引/检索请求在一个没有任何配置错误的系统上 500。
2. **本地模式不认识"你填的地址"**。它永远还原到启动时注入的快照。否则"本地模式"
   会变成"本地模式但用你填的地址"，两个概念混成一个，用户再也说不清自己在用谁。

另外钉住 ``RemoteProvider`` 的读取入口：它必须经 ``runtime.current_url()``，这样
后台切换才作用于**下一次请求**，而不是等 provider 重建。
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.knowledge import runtime

BOOTSTRAP = "http://127.0.0.1:8100"
REMOTE = "http://pi.internal:9100"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例拿到干净的模块状态：启动快照与当前模式都可能被上一个用例改过。"""
    monkeypatch.setattr(runtime, "_BOOTSTRAP_URL", BOOTSTRAP)
    monkeypatch.setattr(runtime, "_current_mode", runtime.MODE_LOCAL)
    monkeypatch.setattr(settings, "knowledge_service_url", BOOTSTRAP, raising=False)
    yield


# ── 归一 ──


def test_normalize_mode_falls_back_to_local() -> None:
    # 配置通道不拒收整条：不认识的值回落到 local，由 apply 判定"无效、保持现状"。
    assert runtime.normalize_mode(" REMOTE ") == runtime.MODE_REMOTE
    assert runtime.normalize_mode("local") == runtime.MODE_LOCAL
    assert runtime.normalize_mode("selfhosted") == runtime.MODE_LOCAL
    assert runtime.normalize_mode(None) == runtime.MODE_LOCAL
    assert runtime.is_known_mode("remote") is True
    assert runtime.is_known_mode("selfhosted") is False


# ── apply：坏配置保持现状 ──


def test_apply_ignores_empty_config() -> None:
    assert runtime.apply(None) is False
    assert runtime.apply({}) is False
    assert runtime.current_url() == BOOTSTRAP
    assert runtime.current_mode() == runtime.MODE_LOCAL


def test_apply_keeps_current_on_unknown_mode() -> None:
    # 一次坏配置不能把正在服务的地方打掉 —— 那会让"改配置"变成"断服务"。
    assert runtime.apply({"mode": "milvus", "service_url": REMOTE}) is False
    assert runtime.current_url() == BOOTSTRAP
    assert runtime.current_mode() == runtime.MODE_LOCAL


def test_apply_keeps_current_on_remote_without_url() -> None:
    # remote 却没给地址：保持现状并记 warning，而不是切到一个空地址。
    assert runtime.apply({"mode": runtime.MODE_REMOTE, "service_url": "  "}) is False
    assert runtime.current_url() == BOOTSTRAP
    assert runtime.current_mode() == runtime.MODE_LOCAL


# ── apply：正常切换 ──


def test_apply_switches_to_remote_url() -> None:
    assert runtime.apply({"mode": runtime.MODE_REMOTE, "service_url": REMOTE + "/"}) is True
    assert runtime.current_mode() == runtime.MODE_REMOTE
    # 末尾斜杠去掉：引擎拼 `/v1/answer` 时会变成 `//v1/answer`。
    assert runtime.current_url() == REMOTE


def test_apply_local_restores_bootstrap_and_ignores_given_url() -> None:
    runtime.apply({"mode": runtime.MODE_REMOTE, "service_url": REMOTE})
    assert runtime.current_url() == REMOTE

    # 本地模式**不接受**随配置提交的地址：否则"本地模式"名不副实。
    assert runtime.apply({"mode": runtime.MODE_LOCAL, "service_url": REMOTE}) is True
    assert runtime.current_url() == BOOTSTRAP
    assert runtime.current_mode() == runtime.MODE_LOCAL


def test_apply_is_idempotent() -> None:
    # 广播重投 / 启动补拉与广播重复到达都会再调一次，不该有任何副作用。
    cfg = {"mode": runtime.MODE_REMOTE, "service_url": REMOTE}
    assert runtime.apply(cfg) is True
    assert runtime.apply(cfg) is True
    assert runtime.current_url() == REMOTE


# ── state：页面据此区分"本地"与"其实早已切走" ──


def test_state_reports_bootstrap_usage() -> None:
    state = runtime.state()
    assert state["mode"] == runtime.MODE_LOCAL
    assert state["service_url"] == BOOTSTRAP
    assert state["bootstrap_url"] == BOOTSTRAP
    assert state["using_bootstrap"] is True

    runtime.apply({"mode": runtime.MODE_REMOTE, "service_url": REMOTE})
    state = runtime.state()
    assert state["mode"] == runtime.MODE_REMOTE
    assert state["service_url"] == REMOTE
    # 生效地址已不是启动那个 ⇒ 页面要能看出"你以为在用本地、其实早已切走"。
    assert state["using_bootstrap"] is False


def test_state_without_bootstrap_url(monkeypatch) -> None:
    # 独立部署可能什么都没注入：此时不能用 `using_bootstrap` 撒谎说是本地服务。
    monkeypatch.setattr(runtime, "_BOOTSTRAP_URL", "")
    monkeypatch.setattr(settings, "knowledge_service_url", "", raising=False)
    assert runtime.state()["using_bootstrap"] is False


# ── RemoteProvider 的唯一读取入口 ──


def test_remote_provider_base_url_reads_runtime() -> None:
    from app.knowledge.remote import RemoteProvider

    provider = RemoteProvider(mode="selfhosted")
    assert provider._base_url() == BOOTSTRAP

    # 热切换必须对**下一次请求**生效：provider 实例不重建。
    runtime.apply({"mode": runtime.MODE_REMOTE, "service_url": REMOTE})
    assert provider._base_url() == REMOTE

    runtime.apply({"mode": runtime.MODE_LOCAL, "service_url": ""})
    assert provider._base_url() == BOOTSTRAP


# ── 广播监听：先补拉再订阅，且只认 knowledge 分类 ──


@pytest.mark.asyncio
async def test_listen_hydrates_before_subscribing(monkeypatch) -> None:
    """重启后没有新消息，只能靠 hydrate 把"上次保存的配置"拉回来。

    不连 Redis：让 hydrate 记录顺序，redis 恒不可用（本任务会退避重试），
    然后按超时把它掐掉 —— 断言只看**顺序**，不看重试细节。
    """
    calls: list[str] = []

    async def fake_hydrate() -> bool:
        calls.append("hydrate")
        return True

    async def fake_get_redis():
        calls.append("get_redis")
        return None

    monkeypatch.setattr(runtime, "hydrate", fake_hydrate)
    monkeypatch.setattr(runtime, "RECONNECT_DELAY_SECONDS", 0.01)
    monkeypatch.setattr("app.redis_client.get_redis", fake_get_redis)

    import asyncio

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(runtime.listen_for_changes(), timeout=0.2)

    # 顺序不能反：先订阅后 hydrate 会在两者之间丢一条消息。
    assert calls[0] == "hydrate"
    assert "get_redis" in calls
