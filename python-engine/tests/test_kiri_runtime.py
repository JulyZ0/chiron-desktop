"""Kiri 运行时的检测与**按需获取**（不内置）。

判据：
* 资产表内嵌的是完整 SHA-256（下载校验不依赖远端校验和文件）；
* socket 可用环境变量覆盖（团队部署 / 测试都能指定）；
* 没有运行时 ⇒ `probe()` 返回 None，`unavailable_reason()` 给可操作原因；
* 下载后哈希不符 ⇒ 删除文件并报错（不留未校验的包）；
* 安装命令是**给人执行**的（Linux 需要 root），不会静默提权。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.isolation import kiri


@pytest.fixture(autouse=True)
def _clean_probe():
    kiri.reset_probe()
    yield
    kiri.reset_probe()


def test_asset_table_has_full_sha256():
    assert kiri._ASSETS, "资产表不能为空"
    for (system, arch, kind), (name, digest) in kiri._ASSETS.items():
        assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), (
            system,
            arch,
            kind,
            name,
        )


def test_asset_selection_for_linux_deb(monkeypatch):
    monkeypatch.setattr(kiri, "_platform_key", lambda: ("linux", "amd64"))
    monkeypatch.setattr(kiri, "_distro_kind", lambda: "deb")
    asset = kiri.asset_for_platform()
    assert asset is not None
    kind, name, digest, url = asset
    assert kind == "deb"
    assert name.startswith("kiri-lite_0.2.2_amd64")
    assert digest == kiri._ASSETS[("linux", "amd64", "deb")][1]
    assert url.endswith(name)


def test_unsupported_platform_has_no_asset(monkeypatch):
    monkeypatch.setattr(kiri, "_platform_key", lambda: ("windows", "amd64"))
    assert kiri.asset_for_platform() is None
    assert "命名管道" in kiri.unavailable_reason()


def test_socket_env_override(tmp_path: Path, monkeypatch):
    sock = tmp_path / "docker.sock"
    sock.write_text("", encoding="utf-8")
    monkeypatch.setenv("SANDBOX_CONTAINER_SOCKET", str(sock))
    assert kiri.socket_path() == str(sock)


def test_probe_without_socket_is_unavailable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(kiri, "_platform_key", lambda: ("linux", "amd64"))
    monkeypatch.setattr(kiri, "_distro_kind", lambda: "deb")
    monkeypatch.setenv("SANDBOX_CONTAINER_SOCKET", str(tmp_path / "missing.sock"))
    monkeypatch.setattr(kiri, "_SOCKET_CANDIDATES", ())
    assert kiri.probe(force=True) is None
    assert not kiri.available()
    assert "未检测到 Kiri" in kiri.unavailable_reason()


def test_prepare_rejects_hash_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHIRON_KIRI_CACHE_DIR", str(tmp_path))
    fake = ("deb", "kiri-lite_0.2.2_amd64.deb", "0" * 64, "https://example.invalid/pkg.deb")
    monkeypatch.setattr(kiri, "asset_for_platform", lambda: fake)

    def _write_bad(url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not the package")

    monkeypatch.setattr(kiri, "_download", _write_bad)
    result = kiri.prepare()
    assert result.ok is False
    assert "校验失败" in result.error
    assert not (tmp_path / "kiri" / kiri.KIRI_VERSION / fake[1]).exists(), "未校验的文件必须删除"


def test_prepare_returns_cached_artifact_and_install_hint(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHIRON_KIRI_CACHE_DIR", str(tmp_path))
    payload = b"fake-deb"
    digest = hashlib.sha256(payload).hexdigest()
    fake = ("deb", "kiri-lite_fake.deb", digest, "https://example.invalid/pkg.deb")
    monkeypatch.setattr(kiri, "asset_for_platform", lambda: fake)
    monkeypatch.setattr(kiri, "_distro_kind", lambda: "deb")
    monkeypatch.setattr(kiri, "_platform_key", lambda: ("linux", "amd64"))
    target = tmp_path / "kiri" / kiri.KIRI_VERSION / fake[1]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)

    # 没有 socket ⇒ 仍然"未就绪"，但安装包已缓存，install_hint 指向它。
    monkeypatch.setattr(kiri, "_SOCKET_CANDIDATES", ())
    result = kiri.prepare()
    assert result.ok is False
    assert result.downloaded == str(target)
    assert "dpkg -i" in result.error
    assert "host-access" in kiri.install_hint(target)


def test_failure_message_names_the_runtime_and_no_fallback():
    text = kiri.failure_message()
    assert "容器隔离挡位不可用" in text
    assert kiri.KIRI_VERSION in text
    assert "不回退" in text
