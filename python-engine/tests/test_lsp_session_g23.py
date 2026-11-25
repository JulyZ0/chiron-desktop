"""G23：常驻语言服务器**会话管理**（`app/tools/lsp/session.py`）的用例。

钉住 PRD 里那五条会话语义：

1. 按 **项目根 + 语言** 复用同一实例（否则每查询一次就重建索引）；
2. 查询**串行**（并发上限 1）；
3. 请求超时后 **SIGTERM → SIGKILL 升级**并让会话失效（下次调用重建）；
4. **卸载时先撤路由再关进程**（反过来的话，新请求会打到正在关闭的进程上，
   表现为"偶发 EchoClosed / 请求半路消失"）；
5. 启动失败给出**可读错误**且不留僵尸进程。

修前会怎样：`app/tools/lsp/` 整个包不存在，本文件的 import 全是 ModuleNotFoundError。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.tools.lsp.client import LspTimeoutError
from app.tools.lsp.config import LspServerConfig
from app.tools.lsp.session import (
    LspSession,
    LspSessionClosedError,
    LspSessionManager,
    LspStartError,
    path_to_uri,
    session_key,
    uri_to_path,
)
from tests.test_lsp_fake_server_g23 import (
    FakeProcess,
    direct_spawn,
    mem_spawn,
    protocol_handler,
    read_log,
    wait_for_requests,
    write_fake_server,
)


def _config(command: tuple[str, ...], **kwargs: object) -> LspServerConfig:
    return LspServerConfig(language="python", command=command, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = write_fake_server(tmp_path)
    monkeypatch.setenv("FAKE_LSP_LOG", str(tmp_path / "fake.log"))
    return path


# ---------------------------------------------------------------------------
# 复用同一实例
# ---------------------------------------------------------------------------


async def test_同项目根与语言复用同一实例(project: Path, script: Path) -> None:
    """同 (项目根, 语言) 的两次 acquire 必须落在**同一个进程**上。

    判据用 pid 而不是对象是否为同一个：对象相同也可能是"重建了一个连到旧进程
    的会话"，pid 相同才说明索引没白建。
    """
    manager = LspSessionManager(spawn=direct_spawn)
    config = _config((sys.executable, str(script)))
    try:
        first = await manager.acquire(project, "python", config=config)
        second = await manager.acquire(project, "python", config=config)
        assert first is second
        pid_first = (await first.request("fake/pid", {}))["pid"]
        pid_second = (await second.request("fake/pid", {}))["pid"]
        assert pid_first == pid_second
        starts = [row for row in read_log(Path(str(script.parent / "fake.log"))) if row["event"] == "start"]
        assert len(starts) == 1, f"起了 {len(starts)} 个假服务器进程，应只起 1 个"
    finally:
        await manager.unload_all()


async def test_不同项目根各自一个实例(tmp_path: Path, script: Path) -> None:
    """不同项目根 = 不同进程（语言服务器按根建索引，混用会给出别的项目的符号）。"""
    first_root = tmp_path / "a"
    second_root = tmp_path / "b"
    first_root.mkdir()
    second_root.mkdir()
    manager = LspSessionManager(spawn=direct_spawn)
    config = _config((sys.executable, str(script)))
    try:
        first = await manager.acquire(first_root, "python", config=config)
        second = await manager.acquire(second_root, "python", config=config)
        assert first is not second
        assert (await first.request("fake/pid", {}))["pid"] != (await second.request("fake/pid", {}))["pid"]
        assert sorted(manager.keys()) == sorted(
            [session_key(first_root, "python"), session_key(second_root, "python")]
        )
    finally:
        await manager.unload_all()


# ---------------------------------------------------------------------------
# 串行查询
# ---------------------------------------------------------------------------


async def test_并发查询串行执行(tmp_path: Path) -> None:
    """同时发两条查询时，第二条的字节**在第一条完成前不发出**。

    判据是假服务器只收到 1 条请求，直到我们放行第一条 —— 这比"计时差不多"更硬：
    它同时排除"两个请求都发出去了、只是服务器处理慢"这种情况。

    修前会怎样：没有这把锁时，语言服务器会同时看到两条 didChange/definition，
    它内部状态与请求顺序耦合，结果取决于调度顺序（偶发串错位置）。
    """
    root = tmp_path / "proj"
    root.mkdir()
    gate = asyncio.Event()

    async def custom(message: dict) -> dict:
        if message["method"] == "fake/slow":
            await gate.wait()
            return {"result": "完成"}
        return {"error": {"code": -32601, "message": "未实现"}}

    spawn, servers = mem_spawn(protocol_handler(custom))
    session = LspSession(root=root, language="python", config=_config(("ignored",)), spawn=spawn)
    await session.start()
    tasks: list[asyncio.Task] = []
    try:
        tasks = [
            asyncio.create_task(session.request("fake/slow", {})),
            asyncio.create_task(session.request("fake/slow", {})),
        ]
        # initialize 在 start() 里已经发过一条，因此这里是"再来一条 fake/slow"
        await wait_for_requests(servers[0], 2)
        await asyncio.sleep(0.05)
        slow = [item for item in servers[0].requests if item["method"] == "fake/slow"]
        assert len(slow) == 1, "两条查询被同时放出去了：串行队列没生效"
        gate.set()
        assert await asyncio.wait_for(tasks[0], 2) == "完成"
        assert await asyncio.wait_for(tasks[1], 2) == "完成"
        slow = [item for item in servers[0].requests if item["method"] == "fake/slow"]
        assert len(slow) == 2
    finally:
        gate.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await session.aclose()
        for server in servers:
            await server.aclose()


# ---------------------------------------------------------------------------
# 超时 → 终止 → 升级 kill
# ---------------------------------------------------------------------------


async def test_请求超时终止进程并升级到kill(tmp_path: Path) -> None:
    """超时后先 terminate，进程不响应再 kill，且会话就此失效。

    为什么必须杀掉而不是"记一笔超时接着用"：一个不回应 definition 的语言服务器
    已经把内部状态搞坏了，留着它只会让后面每条查询都超时（用户体感是"整个
    项目上 LSP 全哑"），而重建一个进程的成本只有一次索引。
    """
    root = tmp_path / "proj"
    root.mkdir()
    proc = FakeProcess(ignore_terminate=True)

    async def custom(message: dict) -> None:
        return None  # 永不回应

    spawn, servers = mem_spawn(protocol_handler(custom), proc=proc)
    session = LspSession(
        root=root,
        language="python",
        config=_config(("ignored",), request_timeout=0.05, kill_grace=0.05),
        spawn=spawn,
    )
    await session.start()
    try:
        with pytest.raises(LspTimeoutError) as excinfo:
            await session.request("fake/query", {})
        assert "fake/query" in str(excinfo.value)
        assert proc.events == ["terminate", "kill"], f"信号升级序列不对：{proc.events}"
        assert session.alive is False
        with pytest.raises(LspSessionClosedError):
            await session.request("fake/query", {})
    finally:
        await session.aclose()
        for server in servers:
            await server.aclose()


async def test_超时终止后再取会话会重建(project: Path, script: Path) -> None:
    """超时被终止的会话不能继续交回去，下一次 acquire 必须重建进程。

    这是"超时就杀"这个决策**能成立的前提**：杀掉之后必须能自动恢复，否则
    用户看到的还是"LSP 全哑"，只是从"一直超时"变成"会话已关闭"。
    """
    manager = LspSessionManager(spawn=direct_spawn)
    config = _config((sys.executable, str(script)), request_timeout=0.05, kill_grace=1.0)
    try:
        first = await manager.acquire(project, "python", config=config)
        pid_first = (await first.request("fake/pid", {}))["pid"]
        with pytest.raises(LspTimeoutError):
            await first.request("fake/slow", {"seconds": 1})
        assert first.alive is False

        second = await manager.acquire(project, "python", config=config)
        assert second is not first
        assert second.alive is True
        pid_second = (await second.request("fake/pid", {}))["pid"]
        assert pid_second != pid_first
    finally:
        await manager.unload_all()


async def test_诊断为等待型超时不杀进程(tmp_path: Path) -> None:
    """诊断等不到推送 → 返回空结果 + 说明，**不**杀进程。

    诊断是"服务器最终会推"的异步通知，杀进程的代价（重建索引）远大于再等等；
    而 definition 是同步请求，超时就说明进程坏了 —— 两者处理方式必须不同。
    """
    root = tmp_path / "proj"
    root.mkdir()

    async def custom(message: dict) -> None:
        return None

    spawn, servers = mem_spawn(protocol_handler(custom))
    session = LspSession(root=root, language="python", config=_config(("ignored",)), spawn=spawn)
    await session.start()
    sample = root / "a.py"
    sample.write_text("x = 1\n", encoding="utf-8")
    try:
        result = await session.diagnostics(sample, wait=0.05)
        assert result.items == []
        assert result.stale is True
        assert session.alive is True, "诊断等待超时不该杀掉语言服务器"
    finally:
        await session.aclose()
        for server in servers:
            await server.aclose()


# ---------------------------------------------------------------------------
# 卸载：先撤路由，再关进程
# ---------------------------------------------------------------------------


async def test_卸载先撤路由再关进程(tmp_path: Path) -> None:
    """`unload()` 必须在 terminate **之前**把会话从路由表里摘掉。

    修前会怎样（顺序反了）：关进程期间新的 acquire 仍能从路由表拿到这个正在
    关闭的会话，于是请求打在一个半死的进程上 —— 报错形态是"偶发 EOF"，
    归因成本极高。
    """
    root = tmp_path / "proj"
    root.mkdir()
    proc = FakeProcess(ignore_terminate=False)
    spawn, servers = mem_spawn(protocol_handler(), proc=proc)
    manager = LspSessionManager(spawn=spawn)
    observed: dict[str, object] = {}

    try:
        session = await manager.acquire(root, "python", config=_config(("ignored",)))
        key = session_key(root, "python")
        proc.on_terminate = lambda: observed.setdefault(
            "route_at_terminate", manager.session_for(root, "python")
        )
        assert await manager.unload(key) is True
        assert observed["route_at_terminate"] is None, "terminate 时路由表里还有这个会话"
        assert manager.keys() == []
        assert session.alive is False
        assert proc.events == ["terminate"]
        assert await manager.unload(key) is False  # 幂等：已卸载
    finally:
        await manager.unload_all()
        for server in servers:
            await server.aclose()


# ---------------------------------------------------------------------------
# 启动失败
# ---------------------------------------------------------------------------


async def test_启动失败给可读错误且不留会话(tmp_path: Path) -> None:
    """命令起得来但立刻挂掉（脚本不存在）→ 可读的 LspStartError，路由表保持干净。"""
    root = tmp_path / "proj"
    root.mkdir()
    manager = LspSessionManager(spawn=direct_spawn)
    config = _config((sys.executable, str(tmp_path / "不存在_lsp_server.py")))
    try:
        with pytest.raises(LspStartError) as excinfo:
            await manager.acquire(root, "python", config=config)
        message = str(excinfo.value)
        assert "启动失败" in message or "初始化失败" in message
        assert "python" in message
        assert manager.keys() == [], "启动失败后路由表里留下了会话"
        assert manager.session_for(root, "python") is None
    finally:
        await manager.unload_all()


async def test_默认spawn_经既有隔离入口(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_spawn_in_sandbox` 必须复用仓库既有的子进程入口，而不是自己 Popen。

    三个接线点逐一断言：
    * 命令经 `child_launch_argv`（隔离档位包装）—— 不是裸 argv；
    * 环境经 `sandboxed_env()`（清掉 JWT_SECRET 等宿主凭据、HOME 指向工作区）；
    * 进程经 `attach_job`（Windows Job Object，`KILL_ON_JOB_CLOSE` 兜孤儿）。

    修前会怎样：绕开这三步直接 `subprocess.Popen(...)` 也能"跑通假服务器"，
    但语言服务器是**由模型间接驱动**的长驻第三方进程 —— 绕开隔离与进程树回收
    等于给模型留了一个不受管的长驻进程。
    """
    import asyncio as _asyncio

    import app.isolation as isolation
    from app.tools import sandbox as sandbox_mod
    from app.tools.lsp import session as session_mod

    recorded: dict[str, object] = {}

    def fake_launch_argv(decision: object, command: list[str]) -> list[str]:
        recorded["command"] = list(command)
        return ["WRAPPED", *command]

    def fake_attach_job(proc: object, limits: object = None, *, label: str = "") -> None:
        recorded["job_label"] = label
        return None

    class _DummyWriter:
        def write(self, data: bytes) -> None:
            recorded.setdefault("written", []).append(data)

        async def drain(self) -> None:
            return None

    class _DummyProc:
        def __init__(self) -> None:
            self.stdin = _DummyWriter()
            self.stdout = _asyncio.StreamReader()
            self.stderr = _asyncio.StreamReader()
            self.returncode: int | None = None
            self.pid = 4321

        def terminate(self) -> None:
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int | None:
            return self.returncode

    async def fake_exec(*argv: str, **kwargs: object) -> _DummyProc:
        recorded["argv"] = list(argv)
        recorded["env"] = dict(kwargs.get("env") or {})  # type: ignore[arg-type]
        recorded["cwd"] = kwargs.get("cwd")
        recorded["stdin"] = kwargs.get("stdin")
        return _DummyProc()

    monkeypatch.setattr(isolation, "child_launch_argv", fake_launch_argv)
    monkeypatch.setattr(isolation, "attach_job", fake_attach_job)
    monkeypatch.setattr(isolation, "resolve_level", lambda requested=None: object())
    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setenv("SANDBOX_ROOT", str(tmp_path / "sbx"))
    monkeypatch.setattr(sandbox_mod, "project_root", lambda: None)

    config = _config(("pyserver", "--stdio"))
    transport = await session_mod._spawn_in_sandbox(config, tmp_path)
    try:
        assert recorded["command"] == ["pyserver", "--stdio"]
        assert recorded["argv"] == ["WRAPPED", "pyserver", "--stdio"]
        assert recorded["job_label"] == "lsp:python"
        env = recorded["env"]
        assert isinstance(env, dict)
        assert env.get("HOME") == str(sandbox_mod.workspace_dir())
        assert "JWT_SECRET" not in env, "沙箱环境没有清掉宿主凭据"
        assert recorded["stdin"] is _asyncio.subprocess.PIPE
        assert recorded["cwd"] == str(tmp_path)
    finally:
        await transport.client.aclose()


# ---------------------------------------------------------------------------
# URI 往返
# ---------------------------------------------------------------------------


async def test_路径与URI往返(tmp_path: Path) -> None:
    """`path_to_uri` / `uri_to_path` 往返一致（Windows 上盘符/反斜杠最容易出错）。"""
    path = tmp_path / "有 空格" / "a.py"
    path.parent.mkdir()
    path.write_text("x = 1\n", encoding="utf-8")
    uri = path_to_uri(path)
    assert uri.startswith("file://")
    back = uri_to_path(uri)
    assert back is not None
    assert back.resolve() == path.resolve()


async def test_非file协议的uri不做路径换算() -> None:
    assert uri_to_path("untitled:Untitled-1") is None
