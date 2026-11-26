"""Tests for SSRF 防护（S4）与 skill_install 校验（S5）。"""
from __future__ import annotations

import json

import pytest

from app.skill.store import SkillStore
from app.tools.skill import skill_install
from app.tools.ssrf import assert_safe_ip, assert_safe_url, command_allowed


class TestSSRF:
    def test_public_url_allowed(self):
        assert_safe_url("https://example.com/page")  # 不抛错

    def test_private_ip_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://127.0.0.1:8000/admin")

    def test_link_local_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://169.254.169.254/latest/meta-data/")

    def test_localhost_hostname_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://localhost:5432")

    def test_private_hostname_blocked(self):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url("http://10.0.0.5/internal")

    def test_invalid_url_rejected(self):
        with pytest.raises(ValueError):
            assert_safe_url("not-a-url")

    # ── 同族绕过：IPv4 藏在 IPv6 里（Python 的 ipaddress 会把它判成 IPv6Address）──
    # 这类写法此前**整体放行**（含云元数据 169.254.169.254），是真正的 SSRF 洞。
    @pytest.mark.parametrize("url", [
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped 回环
        "http://[::ffff:192.168.0.1]:8080/",  # IPv4-mapped 内网
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped 云元数据
        "http://[0:0:0:0:0:ffff:127.0.0.1]/",  # 展开写法（同一个地址）
        "http://[2002:7f00:1::]/",  # 6to4 内嵌 127.0.0.1
    ])
    def test_embedded_ipv4_in_ipv6_blocked(self, url):
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_url(url)

    # 归一化不能过度：公网 IPv6 与"映射到公网 IPv4"的写法必须照常放行。
    @pytest.mark.parametrize("url", [
        "https://[2606:4700:4700::1111]/",
        "http://[::ffff:8.8.8.8]/",
    ])
    def test_public_ipv6_still_allowed(self, url):
        assert_safe_url(url)

    @pytest.mark.parametrize("ip", ["::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1"])
    def test_assert_safe_ip_blocks_embedded_ipv4(self, ip):
        """连接时复检（防 DNS rebinding）走的是同一个判据，同样不能漏。"""
        with pytest.raises(ValueError, match="blocked"):
            assert_safe_ip(ip)


class TestPluginCommandPathRule:
    """插件命令白名单的**路径规则**（与 Go `pluginCommandPathAllowed` 同一条规则）。

    此前只比 basename：`../../tmp/npx`、`..\\..\\tmp\\npx`（相对穿越）与
    `\\\\evil-host\\share\\npx`（UNC）都会命中白名单 ⇒ 宿主机会执行**攻击者放置或远程共享**
    上的同名二进制。本模块的 docstring 一直声称拦这些写法，代码却从未实现（实测三种全放行）。
    """

    @pytest.mark.parametrize("command", ["npx", "/usr/local/bin/npx", "C:/tools/npx"])
    def test_bare_name_and_absolute_paths_allowed(self, monkeypatch, command):
        monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")
        assert command_allowed(command) is True, f"{command} 属合法写法（裸名 / 绝对路径）"

    @pytest.mark.parametrize("command", [
        "../../tmp/npx",  # 相对穿越
        "..\\..\\tmp\\npx",  # Windows 穿越
        "\\\\evil-host\\share\\npx",  # UNC 共享
        "./npx",  # 相对路径带分隔符
    ])
    def test_traversal_and_unc_rejected(self, monkeypatch, command):
        monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")
        assert command_allowed(command) is False, f"{command} 必须被拒"

    def test_non_allowlisted_basename_rejected(self, monkeypatch):
        monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")
        assert command_allowed("/usr/bin/bash") is False

    def test_symlinked_executable_is_allowed(self, tmp_path, monkeypatch):
        """⚠ **符号链接不能**被判成「basename 不一致」。

        真现场（CI）：`/usr/local/bin/npx` 与 `/opt/…/x64/bin/python` 都是**符号链接**
        （分别指到 `…/npx-cli.js` 与 `python3.11`），而实现曾用 `os.path.realpath` 做穿越
        检查 ⇒ 解析后 basename 变了 ⇒ **合法命令被拒**（CI 上 `test_ssrf` 1 条 +
        `test_skill_manager` 5 条全红；本机 Windows 没有这两个路径，realpath 原样返回，
        所以一直绿）。穿越与 UNC 已由 `_command_path_ok` 拒绝 ⇒ 这里只需纯字符串归一化。

        本用例是本机（Windows）**唯一**能钉住它的形态：造一个名字命中白名单、而指向
        另一个名字的真实符号链接。
        """
        target = tmp_path / "npx-real"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        link = tmp_path / "npx"  # 名字命中白名单，realpath 后 basename 会变成 npx-real
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("该平台不允许创建符号链接（Windows 需开发者模式 / 管理员）")
        monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "npx")
        assert command_allowed(str(link)) is True, "符号链接指向的真实 basename 不该影响判定"

    def test_disabled_when_allowlist_empty(self, monkeypatch):
        monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)
        assert command_allowed("npx") is False, "白名单未配置 = 禁止所有自定义插件命令"


class TestPluginAllowlistFile:
    """**后台可编辑**白名单（`{PLUGIN_DATA_DIR}/command_allowlist.json`）。

    背景：装完市场插件、点「测试连接」却被告知命令未放行时，用户只能去改部署 env
    并重启网关 + 引擎。现在后台把 basename 写进这个文件，网关（Go）与引擎（本模块）
    读**同一份**（两侧共享 PLUGIN_DATA_DIR），改动即时生效。

    口径必须与 Go `internal/api/plugin_allowlist.go` 一致：**env ∪ 文件**。
    """

    @staticmethod
    def _write(tmp_path, payload):
        from app.plugins.store import plugin_data_dir

        path = plugin_data_dir() / "command_allowlist.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_file_alone_enables_command(self, tmp_path, monkeypatch):
        """env 为空时，后台文件是**唯一**来源也必须生效（否则后台加白毫无用处）。"""
        monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)
        self._write(tmp_path, {"commands": ["npx"]})

        assert command_allowed("npx") is True, "后台加白后应放行"
        assert command_allowed("/usr/local/bin/npx") is True, "绝对路径同样命中"
        assert command_allowed("curl") is False, "不在名单里的命令仍须拒绝"

    def test_env_and_file_are_merged(self, tmp_path, monkeypatch):
        """并集：后台加白不会把 env 里已有的挤掉，反之亦然。"""
        monkeypatch.setenv("PLUGIN_COMMAND_ALLOWLIST", "uvx")
        self._write(tmp_path, {"commands": ["npx"]})

        assert command_allowed("uvx") is True
        assert command_allowed("npx") is True

    @pytest.mark.parametrize("bad", ["../npx", "/usr/bin/npx", "C:\\tools\\npx", "", "  ", "np\nx"])
    def test_path_like_entries_are_ignored(self, tmp_path, monkeypatch, bad):
        """条目只接受**裸可执行名**：带分隔符的条目等于开一条任意路径放行通道。"""
        monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)
        self._write(tmp_path, {"commands": [bad]})
        assert command_allowed("npx") is False, f"{bad!r} 不该被当成有效条目"

    def test_corrupt_file_fails_closed(self, tmp_path, monkeypatch):
        """文件损坏**不能**变成放行：读失败只让这一来源失效，最终仍是拒绝。"""
        from app.plugins.store import plugin_data_dir

        monkeypatch.delenv("PLUGIN_COMMAND_ALLOWLIST", raising=False)
        path = plugin_data_dir() / "command_allowlist.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        assert command_allowed("npx") is False


class TestSkillInstallValidation:
    @pytest.fixture(autouse=True)
    def _isolate_store(self, tmp_path, monkeypatch):
        from app.tools import skill as skill_mod
        monkeypatch.setattr(skill_mod, "_store", SkillStore(str(tmp_path)))
        monkeypatch.setattr(skill_mod, "_skill_root", str(tmp_path))
        yield

    @pytest.mark.asyncio
    async def test_valid_inline_installs(self):
        out = await skill_install(inline=json.dumps({
            "name": "git-helper", "description": "Git 辅助", "exec": {"type": "prompt", "source": "help {cmd}"},
        }))
        assert out.get("skill") == "git-helper"

    @pytest.mark.asyncio
    async def test_missing_name_rejected(self):
        out = await skill_install(inline=json.dumps({"description": "no name"}))
        assert "error" in out and "name" in out["error"]

    @pytest.mark.asyncio
    async def test_invalid_name_rejected(self):
        out = await skill_install(inline=json.dumps({"name": "../evil", "description": "x"}))
        assert "error" in out and "name" in out["error"]

    @pytest.mark.asyncio
    async def test_missing_description_rejected(self):
        out = await skill_install(inline=json.dumps({"name": "ok-name"}))
        assert "error" in out and "description" in out["error"]

    @pytest.mark.asyncio
    async def test_oversize_inline_rejected(self):
        big = json.dumps({"name": "big", "description": "x" * 2_000_000})
        out = await skill_install(inline=big)
        assert "error" in out and "too large" in out["error"]

    @pytest.mark.asyncio
    async def test_ssrf_blocked_url(self):
        out = await skill_install(url="http://169.254.169.254/latest/meta-data/")
        assert "error" in out and "blocked" in out["error"]


class TestFetchUrlSafeLimit:
    """L17：附件下载必须边读边截断，不能先把整包读进内存。"""

    async def test_max_bytes_truncates_streaming_body(self):
        import httpx

        from app.tools.ssrf import fetch_url_safe

        big = b"x" * (1024 * 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/plain"}, content=big)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            resp = await fetch_url_safe(client, "https://1.1.1.1/big.txt", max_bytes=1024)

        assert resp.status_code == 200
        assert len(resp.content) == 1024

    async def test_max_bytes_follows_redirects(self):
        import httpx

        from app.tools.ssrf import fetch_url_safe

        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if str(request.url).endswith("/start"):
                return httpx.Response(302, headers={"location": "https://1.1.1.1/final"})
            return httpx.Response(200, content=b"done")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=False
        ) as client:
            resp = await fetch_url_safe(client, "https://1.1.1.1/start", max_bytes=1024)

        assert resp.content == b"done"
        assert len(seen) == 2
