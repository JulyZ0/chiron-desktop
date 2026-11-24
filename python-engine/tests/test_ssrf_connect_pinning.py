"""H39：SSRF 连接级钉住 —— 默认拒绝非全球 unicast + connect 时只解析一次。

为什么需要这一组回归（PRD 9.7 / H39）：

* 旧判据是**黑名单**（"没被列出来就放行"）：`100.64.0.0/10`（CGNAT）、`192.0.0.0/24`、
  `198.18.0.0/15`、`192.0.2.0/24`、`198.51.100.0/24`、`203.0.113.0/24` 都不在表里，
  等于放行。新判据反过来：**只放行全球 unicast**。
* `assert_safe_url()` 校验完到 httpx 真正建连之间还有一次 DNS 解析 —— 攻击者让第一次
  返回公网、第二次返回 `127.0.0.1`，检查就白做了。收口在连接后端：自己解析、自己校验、
  把**已校验的 IP** 递下去连接。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import httpx
import pytest

from app.tools import ssrf


def _fake_getaddrinfo(mapping: dict[str, list[str]]):
    """按 host 返回预设地址；未登记的 host 一律解析失败（测试不许联网）。"""

    def _inner(host, port, *args, **kwargs):  # noqa: ANN001, ANN202 - 复刻 stdlib 签名
        if host not in mapping:
            raise socket.gaierror(f"unexpected host in test: {host}")
        out = []
        for raw in mapping[host]:
            ip = ipaddress.ip_address(raw)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (raw, port or 0)))
        return out

    return _inner


# ── 1. 默认拒绝：以前漏掉的保留段现在必须被拦 ────────────────────────────────
@pytest.mark.parametrize(
    "raw",
    [
        "100.64.0.1",  # CGNAT（云上内网服务密集段）
        "100.127.255.254",  # CGNAT 上界
        "192.0.0.1",  # IETF 协议分配
        "198.18.0.1",  # 基准测试段
        "192.0.2.10",  # TEST-NET-1
        "198.51.100.10",  # TEST-NET-2
        "203.0.113.10",  # TEST-NET-3
        "240.0.0.1",  # 保留
        "255.255.255.255",  # 广播
        "0.0.0.0",
        "::1",
        "::",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
        "100::1",  # 丢弃段
        "64:ff9b::7f00:1",  # NAT64 映射到 127.0.0.1
        "::ffff:169.254.169.254",  # IPv4-mapped 云元数据
    ],
)
def test_reserved_and_internal_ranges_are_blocked(raw: str) -> None:
    with pytest.raises(ValueError, match="blocked"):
        ssrf.assert_safe_ip(raw)


@pytest.mark.parametrize("raw", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111",
                                "::ffff:8.8.8.8"])
def test_global_unicast_still_allowed(raw: str) -> None:
    ssrf.assert_safe_ip(raw)  # 不抛错


# ── 2. 解析：混有内网地址的记录整体拒绝（fail-closed）────────────────────────
def test_mixed_resolution_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"mixed.test": ["93.184.216.34", "10.0.0.7"]})
    )
    with pytest.raises(ValueError, match="blocked"):
        ssrf.resolve_validated_ips("mixed.test", 443)


def test_resolution_returns_literal_ips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"ok.test": ["93.184.216.34"]})
    )
    assert ssrf.resolve_validated_ips("ok.test", 443) == ["93.184.216.34"]


# ── 3. 连接级钉住：connect 用的是**已校验的 IP**，不是 hostname ──────────────
class _RecordingDelegate:
    """记录下层真正被要求连接的 host，用来证明 hostname 没有流到 socket 层。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port))
        return "stream"

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return "unix-stream"

    async def sleep(self, seconds):  # pragma: no cover - 不被本测试触发
        return None


@pytest.mark.asyncio
async def test_connect_pins_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"pin.test": ["93.184.216.34"]})
    )
    delegate = _RecordingDelegate()
    backend = ssrf._PinnedNetworkBackend(delegate)

    await backend.connect_tcp("pin.test", 443)

    assert delegate.calls == [("93.184.216.34", 443)], "连接目标必须是已校验的 IP 字面量"


@pytest.mark.asyncio
async def test_connect_refuses_internal_before_touching_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ssrf.socket, "getaddrinfo", _fake_getaddrinfo({"evil.test": ["127.0.0.1"]})
    )
    delegate = _RecordingDelegate()
    backend = ssrf._PinnedNetworkBackend(delegate)

    with pytest.raises(ValueError, match="blocked"):
        await backend.connect_tcp("evil.test", 80)
    assert delegate.calls == [], "被拦下的目标绝不能触达 socket 层"


@pytest.mark.asyncio
async def test_dns_rebinding_window_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一次解析公网（解析级检查放行）→ 第二次解析内网（连接期必须拒绝）。

    这正是旧实现失效的场景：`assert_safe_url()` 看到 93.184.216.34 就放行，随后 httpx
    自己再解析一次拿到 127.0.0.1 并连接。现在连接后端自己解析、自己校验，不允许这种
    两次解析之间的切换。
    """
    answers = {"rebind.test": ["93.184.216.34", "127.0.0.1"]}
    state = {"n": 0}

    def _changing(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
        if host != "rebind.test":
            raise socket.gaierror(host)
        raw = answers[host][min(state["n"], 1)]
        state["n"] += 1
        ip = ipaddress.ip_address(raw)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (raw, port or 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", _changing)

    ssrf.assert_safe_url("https://rebind.test/page")  # 第一次：公网 ⇒ 放行
    delegate = _RecordingDelegate()
    backend = ssrf._PinnedNetworkBackend(delegate)
    with pytest.raises(ValueError, match="blocked"):
        await backend.connect_tcp("rebind.test", 443)  # 第二次：内网 ⇒ 拒绝
    assert delegate.calls == []


@pytest.mark.asyncio
async def test_explicit_allowlist_host_skips_pinning(monkeypatch: pytest.MonkeyPatch) -> None:
    """部署内网服务名（`SSRF_ALLOW_PRIVATE_HOSTS`）在连接期同样要认。"""
    monkeypatch.setenv("SSRF_ALLOW_PRIVATE_HOSTS", "gateway")
    delegate = _RecordingDelegate()
    backend = ssrf._PinnedNetworkBackend(delegate)

    await backend.connect_tcp("gateway", 8080)

    assert delegate.calls == [("gateway", 8080)], "显式放行的服务名不该被解析/改写"


# ── 4. 客户端加固：幂等，且认不出连接后端时 fail-closed ──────────────────────
def test_harden_async_client_is_idempotent() -> None:
    client = httpx.AsyncClient(follow_redirects=False)
    try:
        ssrf.harden_async_client(client)
        first = client._transport._pool._network_backend
        ssrf.harden_async_client(client)
        assert client._transport._pool._network_backend is first, "重复加固不应层层包裹"
        assert getattr(first, ssrf._PINNED_MARKER, False) is True
    finally:
        asyncio.run(client.aclose())


def test_harden_fails_closed_for_unknown_transport() -> None:
    class _Opaque(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):  # pragma: no cover - 不被调用
            raise AssertionError("should not be called")

    client = httpx.AsyncClient(transport=_Opaque())
    with pytest.raises(RuntimeError, match="SSRF hardening failed"):
        ssrf.harden_async_client(client)


def test_make_safe_async_client_installs_pinning() -> None:
    client = ssrf.make_safe_async_client(timeout=1.0)
    try:
        assert client.follow_redirects is False
        backend = client._transport._pool._network_backend
        assert getattr(backend, ssrf._PINNED_MARKER, False) is True
    finally:
        asyncio.run(client.aclose())


def test_mock_transport_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """桩传输不是网络传输：加固必须放它过去，否则所有用桩的测试都会红。"""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    client = httpx.AsyncClient(transport=transport)
    try:
        ssrf.harden_async_client(client)  # 不抛错
    finally:
        asyncio.run(client.aclose())
