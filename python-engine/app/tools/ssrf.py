"""SSRF 防护 — 只放行全球 unicast + scheme/端口白名单 + 连接时钉住已校验 IP

web_fetch/web_search/skill_install 等出站 HTTP 的目标 host 解析后，
**只允许**全球 unicast 地址；回环/私有/链路本地/保留段（含云元数据
169.254.169.254、CGNAT 100.64.0.0/10、TEST-NET、基准测试段等）一律拒绝。

强化项：
- scheme 白名单：仅允许 http/https
- 端口白名单：仅允许 80/443/8080/8443（防访问内网 6379/5432/27017 等服务端口）
- **默认拒绝**：判据从"黑名单命不命中"改为"是不是全球 unicast"（PRD 9.7 / H39）。
  黑名单永远漏段：`100.64.0.0/10`、`192.0.0.0/24`、`198.18.0.0/15`、`192.0.2.0/24`、
  `198.51.100.0/24`、`203.0.113.0/24` 此前都不在表里，等于"只要不在名单上就放行"。
- **连接级钉住**：`harden_async_client()` 把 httpx 的连接后端换成
  :class:`_PinnedNetworkBackend` —— 真正 `connect()` 的那一刻自己解析、自己校验、
  再连**已校验那个 IP**，于是 `assert_safe_url()` 与 TCP 握手之间不再有第二次解析，
  DNS rebinding 的窗口被关掉（PRD 9.7 / H39 要求的"连接级 hook 复检"）。
  重定向仍逐跳重新校验（`fetch_url_safe`）。
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import socket
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# 允许的 scheme
_ALLOWED_SCHEMES = {"http", "https"}
# 允许的端口（防访问内网数据库/缓存/消息队列服务端口）
# 已包含 443（HTTPS 标准端口），确保 `https://github.com` 等外部站点不被误拦。
# 注意：第2轮检查发现 443 已在列表中，此注释为确认，无需修改。
# 扩展：添加 3000/5000/9000 以支持常见外部 API 服务端口。
_ALLOWED_PORTS = {80, 443, 3000, 5000, 8080, 8443, 9000}

# 被禁止的 IP 段（IPv4）——**保留作显式说明与回归断言的目标**。
#
# 判据已改为"只放行全球 unicast"（见 `_is_blocked`），所以这张表不再决定放行与否。
# 留着的理由：① 它记录了每一段"曾经漏过 / 特别危险"的地址；② 测试可以逐段断言；
# ③ 新增段时不必再靠人肉记忆 —— 只要 `is_global` 说得对，默认就是拒绝。
_BLOCKED_IPV4 = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT（云上大量内网服务就在这一段）
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # 云元数据/链路本地
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),  # IETF 协议分配
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),  # 基准测试
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
]
# 被禁止的 IPv6 段
_BLOCKED_IPV6 = [
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("64:ff9b:1::/48"),  # 本地 NAT64（可映射任意 IPv4）
    ipaddress.ip_network("100::/64"),  # 丢弃段
    ipaddress.ip_network("2001:db8::/32"),  # 文档段
    ipaddress.ip_network("fc00::/7"),  # ULA
    ipaddress.ip_network("fe80::/10"),  # 链路本地
]


def _is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """判定 IP 是否**不允许**出站。判据是"**只放行全球 unicast**"（PRD 9.7 / H39）。

    ⚠ 必须处理"**IPv4 藏在 IPv6 里**"的写法：地址族是 IPv6，但落点是 IPv4。
    只比对 IPv6 判据会整体漏掉 —— 实测（2026-10-08）以下写法曾被**放行**：

        http://[::ffff:127.0.0.1]/          （回环）
        http://[::ffff:192.168.0.1]:8080/   （内网）
        http://[::ffff:169.254.169.254]/    （云元数据 ⇒ 可窃取实例凭据）

    覆盖三种嵌入形式：IPv4-mapped（`::ffff:a.b.c.d`）、6to4（`2002::/16`）、Teredo。
    注意：**不要**直接把 `::ffff:0:0/96` 整段拉黑 —— 映射到公网 IPv4 的写法是合法的；
    正确做法是把内嵌的 IPv4 取出，用**同一套 IPv4 策略**判定（本函数递归）。

    为什么用 `is_global` 做**唯一**放行依据：黑名单是"没被列出来就放行"，漏一个段就
    是一条洞（CGNAT / TEST-NET / 基准测试段都曾漏）。反过来"只有全球 unicast 才放行"
    是默认拒绝，新出现的保留段自动被拦。
    """
    if isinstance(ip, ipaddress.IPv6Address):
        embedded: list[ipaddress.IPv4Address] = []
        if ip.ipv4_mapped is not None:
            embedded.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            embedded.append(ip.sixtofour)
        teredo = ip.teredo
        if teredo is not None:
            embedded.extend(teredo)  # (server, client)
        if embedded:
            # 内嵌形式统一交给 IPv4 策略判定：映射到公网 IPv4 的写法照常放行。
            return any(_is_blocked(inner) for inner in embedded)
    # `is_reserved` 覆盖 NAT64 `64:ff9b::/96`（`is_global` 会误报 True，但 `is_reserved` 为真）。
    if ip.is_multicast or ip.is_reserved or not ip.is_global:
        return True
    return False



def assert_safe_url(url: str) -> None:
    """解析 url 的 host，若指向内网/保留地址则抛 ValueError。

    校验顺序：scheme → 端口 → host → DNS 解析 → IP 黑名单。
    调用方在 connect 时应调用 assert_safe_ip 再次复检防 DNS rebinding。
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"blocked scheme: {parsed.scheme} (only http/https allowed)")
    host = parsed.hostname
    if not host:
        raise ValueError(f"invalid url: {url}")

    # 显式放行名单（SSRF_ALLOW_PRIVATE_HOSTS，逗号分隔）。引擎需要访问**集群内服务名**
    # （如 gateway:8080），而那是 Docker 内网地址，会被下面的私网/保留地址检查拦下 ——
    # 实测 app/tools/browser.py 的 _init_default_hub() 就因此启动即崩。
    # 默认空 = 严格模式不变（生产无需改动）；只有这类部署才显式放行。
    allowed = {
        h.strip().lower()
        for h in os.getenv("SSRF_ALLOW_PRIVATE_HOSTS", "").split(",")
        if h.strip()
    }
    if host.lower() in allowed:
        return

    # 端口白名单：未显式指定端口时按 scheme 默认值（80/443）
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if port not in _ALLOWED_PORTS:
        raise ValueError(f"blocked port: {port} (not in allowlist)")

    # 已是 IP 字面量 → 直接检查
    try:
        ip = ipaddress.ip_address(host)
        if _is_blocked(ip):
            raise ValueError(f"blocked address (internal/private): {host}")
        return
    except ValueError as e:
        if isinstance(e, ValueError) and str(e).startswith("blocked"):
            raise
        # 非 IP 字面量 → 继续 DNS 解析

    # DNS 解析，逐条检查（任何一条不合规即拒绝；与连接期用的是同一个判据）
    resolve_validated_ips(host)


def assert_safe_ip(ip_str: str) -> None:
    """连接时复检 IP，防 DNS rebinding。

    调用方流程（`harden_async_client()` 之后由 httpx 连接后端自动完成，见
    :class:`_PinnedNetworkBackend`）：
        assert_safe_url(url)
        ip = resolve_validated_ips(host)[0]   # 自己解析
        assert_safe_ip(ip)                    # 校验
        # 连接 ip —— **不要再解析一次**
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError as e:
        raise ValueError(f"invalid ip for rebinding check: {ip_str}") from e
    if _is_blocked(ip):
        raise ValueError(f"blocked address at connect time (DNS rebinding?): {ip_str}")


def resolve_validated_ips(host: str, port: int | None = None) -> list[str]:
    """解析 `host` 并**逐条**校验，返回可用于连接的 IP 字面量列表。

    任何一条解析结果不合规就整体拒绝（fail-closed）：一个域名同时解析到公网与内网
    地址，只可能是攻击者控制的 DNS，挑"看起来合法的那条"去连等于把选择权交回给他。
    """
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if literal:
        assert_safe_ip(host)
        return [host]

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve host: {host}") from e
    except OSError as e:  # 某些平台对空 host / 非法 family 抛 OSError
        raise ValueError(f"cannot resolve host: {host}") from e

    out: list[str] = []
    for info in infos:
        raw = info[4][0]
        ip = ipaddress.ip_address(raw)
        if _is_blocked(ip):
            raise ValueError(f"blocked address (internal/private): {host} -> {ip}")
        if raw not in out:
            out.append(raw)
    if not out:
        raise ValueError(f"cannot resolve host: {host}")
    return out


#: 标记"这个后端已经装过校验"，避免重复包裹（重复包裹会多次解析，反而放大窗口）。
_PINNED_MARKER = "_chiron_ssrf_pinned"


class _PinnedNetworkBackend:
    """httpcore 连接后端代理：**在 connect 那一刻**解析 + 校验 + 钉住 IP。

    为什么必须在这一层：`assert_safe_url()` 与 `httpx` 真正建连之间隔着一整套连接池，
    中间 httpx 自己会再解析一次 DNS —— 攻击者只要让第一次解析返回公网 IP、第二次返回
    `127.0.0.1`，写在 `assert_safe_url()` 里的检查就完全失效（经典 DNS rebinding）。
    这里的做法是：**不把 hostname 交给下层**，而是自己解析、自己校验、把**已校验的 IP
    字面量**递下去连接。于是只有一次解析，且解析结果就是连接目标。

    TLS 不受影响：httpcore 的 `server_hostname`（SNI 与证书校验用的主机名）来自请求的
    origin，与本后端的 `host` 参数无关 —— 所以连 IP、证书仍按原主机名校验。
    """

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate
        setattr(self, _PINNED_MARKER, True)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        # 显式放行名单（部署内网服务名）在这里也要认，否则连接期会把解析级放行的目标再拦一次。
        if _host_explicitly_allowed(host):
            return await self._delegate.connect_tcp(
                host,
                port,
                timeout=timeout,
                local_address=local_address,
                socket_options=socket_options,
            )
        pinned = resolve_validated_ips(host, port)[0]
        return await self._delegate.connect_tcp(
            pinned,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> Any:
        return await self._delegate.connect_unix_socket(
            path, timeout=timeout, socket_options=socket_options
        )

    async def sleep(self, seconds: float) -> None:
        return await self._delegate.sleep(seconds)


def _host_explicitly_allowed(host: str) -> bool:
    allowed = {
        h.strip().lower()
        for h in os.getenv("SSRF_ALLOW_PRIVATE_HOSTS", "").split(",")
        if h.strip()
    }
    return bool(host) and host.lower() in allowed


def _connection_pools(transport: Any) -> list[Any]:
    """把 httpx transport 里的 httpcore pool 逐层挖出来（含代理包装）。"""
    pools: list[Any] = []
    seen: set[int] = set()
    node = getattr(transport, "_pool", None)
    while node is not None and id(node) not in seen:
        seen.add(id(node))
        pools.append(node)
        node = getattr(node, "_pool", None)
    return pools


def harden_async_client(client: httpx.AsyncClient) -> None:
    """给 `client` 装上连接级 IP 钉住（幂等）。传入自己构造的客户端时**必须**调用。

    找不到可包裹的连接后端时抛 :class:`RuntimeError`（fail-closed）：那种情况下
    SSRF 防护会**静默失效**，比直接报错更危险 —— 调用方应改用
    :func:`make_safe_async_client`。

    例外：`httpx.MockTransport`（及其子类）不是网络传输，不做 DNS 也不开 socket，
    必须继续可用 —— 否则所有用桩的测试都会因为"加固不了"而红。判据是**类型**而不是
    "有没有 `_pool`"：任意自定义传输都可能真的发网络请求，认不出就得报错。
    """
    transports: list[Any] = []
    primary = getattr(client, "_transport", None)
    if primary is not None:
        transports.append(primary)
    mounts = getattr(client, "_mounts", None)
    if isinstance(mounts, dict):
        transports.extend(mounts.values())

    hardened = 0
    skipped = 0
    for transport in transports:
        if isinstance(transport, httpx.MockTransport):
            skipped += 1
            continue
        for pool in _connection_pools(transport):
            backend = getattr(pool, "_network_backend", None)
            if backend is None:
                continue
            if getattr(backend, _PINNED_MARKER, False):
                hardened += 1
                continue
            pool._network_backend = _PinnedNetworkBackend(backend)
            hardened += 1
    if not hardened and not skipped:
        raise RuntimeError(
            "SSRF hardening failed: no httpcore connection backend found on this client; "
            "use app.tools.ssrf.make_safe_async_client() instead of a hand-built transport"
        )


def make_safe_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """构造一个**已装好**连接级钉住的 `httpx.AsyncClient`。

    默认 `follow_redirects=False`：重定向必须由 `fetch_url_safe()` 逐跳校验，
    交给 httpx 自动跟随等于让一跳未校验的内网地址通过。
    """
    kwargs.setdefault("follow_redirects", False)
    client = httpx.AsyncClient(**kwargs)
    harden_async_client(client)
    return client



async def fetch_url_safe(
    client: httpx.AsyncClient,
    url: str,
    max_redirects: int = 5,
    *,
    max_bytes: int | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """SSRF 安全的 HTTP GET：每次重定向跳转前重新执行 assert_safe_url，
    防止攻击者用公开 URL 302 到内网/云元数据地址（重定向绕过）。
    客户端应使用 follow_redirects=False 的 AsyncClient 调用本函数。
    max_redirects 上限 20，防止调用方传超大值造成循环攻击。

    进入循环前先 `harden_async_client(client)`：**解析级检查不构成防线**（两次解析之间
    就是 rebinding 窗口），真正的收口是连接期钉住已校验 IP（PRD 9.7 / H39）。

    `max_bytes` 非空时走流式读取并**只保留前 N 字节**：`client.get()` 会先把整个响应
    读进内存，用户贴一个 2GB 的 text/plain 链接就能把引擎 RSS 打爆（SSRF 校验挡的是
    地址，不是体积）。超限的响应返回截断 body，调用方按需标注"已截断"。
    """
    harden_async_client(client)
    max_redirects = min(max_redirects, 20)
    current = url
    for _ in range(max_redirects + 1):
        assert_safe_url(current)
        if max_bytes is not None and max_bytes > 0:
            async with client.stream("GET", current, follow_redirects=False, **kwargs) as resp:
                status = resp.status_code
                headers = resp.headers
                if status in (301, 302, 303, 307, 308):
                    location = headers.get("location")
                    if not location:
                        return httpx.Response(status, headers=headers)
                    current = str(httpx.URL(current).join(location))
                    continue
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    room = max_bytes - len(body)
                    if room <= 0:
                        break
                    body.extend(chunk[:room])
                return httpx.Response(status, headers=headers, content=bytes(body))
        resp = await client.get(current, follow_redirects=False, **kwargs)
        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("location")
            if not location:
                return resp
            current = str(httpx.URL(current).join(location))
            continue
        return resp
    raise ValueError(f"too many redirects: {url}")


def _command_path_ok(command: str) -> bool:
    """命令必须是**裸 basename**或**绝对路径**：拒绝相对路径、`..` 段与 UNC。

    为什么不能只比 basename：白名单管的是"哪个**名字**能跑"，而"**从哪跑**"同样要紧 ——
    `../../tmp/npx`、`..\\..\\tmp\\npx`（相对穿越）与 `\\\\evil-host\\share\\npx`（UNC）
    都会让宿主机执行**攻击者放置或远程共享**上的同名二进制，而名字仍白名单命中。
    本函数实现的正是本模块 docstring 一直声称、但代码此前**并未实现**的那条规则
    （实测这三种写法此前全部返回 True；回归见 `tests/test_ssrf.py`）。
    """
    if not command:
        return False
    # UNC（Windows：\\host\share\…）与协议相对形式（//host/share/…）
    if command.startswith(("\\\\", "//")):
        return False
    parts = [p for p in re.split(r"[\\/]", command) if p != ""]
    if len(parts) <= 1:
        return True  # 裸 basename：交给 PATH 解析
    if ".." in parts:
        return False
    # 绝对路径：POSIX `/…` 或 Windows `X:\…` / `X:/…`
    return command.startswith("/") or bool(re.match(r"^[A-Za-z]:[\\/]", command))


_ALLOWLIST_FILE_NAME = "command_allowlist.json"


def plugin_allowlist_file_entries() -> frozenset[str]:
    """读**后台可编辑**的白名单（`{PLUGIN_DATA_DIR}/command_allowlist.json`）。

    与 Go 网关 `internal/api/plugin_allowlist.go` 读的是**同一份文件**：两侧本就共享
    `PLUGIN_DATA_DIR`（见 app/plugins/store.py 的 plugin_data_dir），所以管理员在后台
    加白后引擎无需重启就能拉起命令 —— 否则会出现"测试连接通过、真正连又报错"。

    文件不存在 / 损坏 / 条目非法一律**跳过**，返回空集：调用方按"该来源未配置"处理，
    绝不能因为读失败而放开（那是 fail-open）。条目只接受**裸 basename**，与 Go 侧
    `normalizePluginCommandName` 同一约束 —— 带分隔符的条目等于开一条任意路径放行通道。
    """
    try:
        from app.plugins.store import plugin_data_dir
    except Exception:  # noqa: BLE001 — 导入失败只该让这一来源失效
        return frozenset()
    try:
        path = plugin_data_dir() / _ALLOWLIST_FILE_NAME
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(doc, dict):
        return frozenset()
    commands = doc.get("commands")
    if not isinstance(commands, list):
        return frozenset()
    out: set[str] = set()
    for raw in commands:
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        # 与 _command_path_ok 的"裸名"口径一致，但这里更严：条目**只**允许裸名。
        if not name or re.search(r"[\\/\x00\r\n]", name) or name in {".", ".."}:
            continue
        out.add(name)
    return frozenset(out)


def command_allowed(command: str) -> bool:
    """插件命令白名单（与 Go 网关保持一致，口径是 **env ∪ 后台文件**）：
    仅允许白名单内的可执行文件 basename 被拉起为 MCP 插件进程；
    两侧都没配置（空）时禁止所有自定义插件命令（安全默认，防任意命令执行）。

    路径规则：只接受**裸 basename**（走 PATH）或**绝对路径**；拒绝相对路径、`..`
    与 UNC —— 见 `_command_path_ok`。
    """
    raw = os.getenv("PLUGIN_COMMAND_ALLOWLIST", "").strip()
    allowed = {part.strip() for part in raw.split(",") if part.strip()}
    allowed |= plugin_allowlist_file_entries()
    if not allowed:
        return False
    if not _command_path_ok(command):
        return False
    base = os.path.basename(command)

    # 防「路径里的 basename 与归一化后的 basename 不同」这一种混淆。
    # ⚠ **不能用 `os.path.realpath`**：它会**解析符号链接**，于是
    # `/usr/local/bin/npx`（→ …/npx-cli.js）与 `/opt/…/bin/python`（→ python3.11）这类
    # **合法**的符号链接会被判成"basename 不一致"而拒绝 —— 本机 Windows 上这两个路径
    # 不存在（realpath 原样返回）所以一直绿，CI（Linux runner 预装 node / python 是
    # 符号链接）上直接红（test_ssrf 1 条 + test_skill_manager 5 条）。
    # 穿越（`..`）与 UNC 已由 `_command_path_ok` 拒绝 ⇒ 这里只需 `normpath`（纯字符串，
    # 不碰文件系统）。
    normalized = os.path.normpath(command)
    _, normalized_base = os.path.split(normalized)

    if normalized_base.lower() != base.lower():
        return False

    return base in allowed
