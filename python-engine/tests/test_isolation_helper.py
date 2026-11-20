"""提权 helper 的三条安全边界，各用测试钉住。

## 为什么这一层必须有测试

helper 是**唯一**一个以管理员令牌常驻的进程。它存在的前提是"自己从不执行命令，
只把请求转成受限令牌下的 `CreateProcessAsUserW`"。这个前提一旦被削弱：
多认一个宽松参数、少校验一次签名、失败时退回无隔离路径 —— 攻击者拿到的就
不再是"受限执行"通道，而是引擎整体的权限提升。

三类边界分开钉：

1. **协议鉴权** —— 签名覆盖整个请求体、缺失即拒。密钥不走命令行（命令行对同机
   任意进程可见）。helper 活着但没提权，也要如实报告，而不是"起了就算好了"。
2. **不暴露任意执行** —— `argv` 只收列表（杜绝"顺手加个 shell"）、`cwd` 必须
   绝对且存在（提权进程按"当前目录"解析相对路径是经典提权手法）、上下限都夹住。
3. **降级路径** —— helper 不可用时 `launch_restricted` 必须**拒绝执行**，
   绝不退回无隔离执行。这条是整个方案的兑现点：沙箱坏了就不跑。

全部用替身：不需要提权、不需要真的起 helper、不需要 UAC。
"""

from __future__ import annotations

import ctypes

import pytest

from app.isolation import elevated_client as client
from app.isolation import elevated_helper as helper
from app.isolation import elevated_protocol as proto
from app.isolation import win, win_launch

#: 一条形状正确的测试密钥。值不重要，只验证长度契约与签名算法。
TEST_KEY = b"k" * proto.KEY_BYTES


# ── 协议鉴权 ────────────────────────────────────────────


def test_signature_covers_the_whole_body():
    """签名绑定整个请求体：改动任何一字节都必须验不过。

    这是"签名放在 HTTP 头而不是 JSON 体里"的目的 —— 若签名在体内部，
    它就只覆盖"除自己以外的字段"，攻击者可以自由重排/插入字段。
    """
    body = b'{"argv":["cmd.exe","/c","echo hi"],"cwd":"C:\\\\"}'
    signature = proto.sign(TEST_KEY, body)

    assert proto.verify(TEST_KEY, body, signature)
    # 追加一个字节（模拟插入字段）：签名必须失效
    assert not proto.verify(TEST_KEY, body + b" ", signature)
    # 换一把钥匙：同样失效
    assert not proto.verify(b"j" * proto.KEY_BYTES, body, signature)


def test_verify_accepts_case_and_surrounding_space_but_rejects_empty():
    """十六进制大小写与首尾空白容忍；缺失签名一律拒绝。

    这里刻意**不用** `==`：`compare_digest` 防的是"逐字节猜签名"的时序侧信道。
    """
    body = b"{}"
    signature = proto.sign(TEST_KEY, body)

    assert proto.verify(TEST_KEY, body, signature.upper())
    assert proto.verify(TEST_KEY, body, f"  {signature}  ")
    assert not proto.verify(TEST_KEY, body, None)
    assert not proto.verify(TEST_KEY, body, "")


def test_unsigned_request_is_refused_without_leaking_details():
    """未签名请求必须被拒，且不返回请求内容相关细节。

    `_authorized` 只回布尔。失败连接可能是探测，回得越多越方便对方试探。
    """

    class _Handler(helper._Handler):
        """只借 `_authorized` 的逻辑，不碰 socket。"""

        headers: dict[str, str] = {}
        command = "POST"
        path = "/v1/exec"

        def address_string(self) -> str:
            return "127.0.0.1"

        def __init__(self) -> None:  # noqa: N807 — 绕开 BaseHTTPRequestHandler 的初始化
            self.shared_key = TEST_KEY

    assert _Handler()._authorized(b"{}") is False

    signed = _Handler()
    signed.headers = {proto.SIGNATURE_HEADER: proto.sign(TEST_KEY, b"{}")}
    assert signed._authorized(b"{}") is True
    # 同一签名换个 body 就不认 —— 防"签名与体拼接"绕过
    assert signed._authorized(b'{"argv":["whoami"]}') is False


def test_key_never_appears_on_the_helper_command_line():
    """共享密钥不得出现在 argv 里。

    命令行对同机任意进程可见（`wmic process get commandline`），把密钥放进去
    等于没有鉴权。helper 与引擎都从受 DACL 保护的目录读它。
    """
    argv = client.helper_argv(1234)
    assert argv[1:3] == ["-m", "app.isolation.elevated_helper"]
    assert "--parent-pid=1234" in argv

    joined = " ".join(argv)
    # 密钥的十六进制/证明串都不该出现
    assert TEST_KEY.hex() not in joined
    assert TEST_KEY.decode() not in joined


# ── 可用性探测：三种"不可用"必须能分开 ───────────────────


@pytest.fixture
def fresh_probe(monkeypatch):
    """复位探测缓存，避免用例间互相污染（`available()` 只缓存成功）。"""
    monkeypatch.setattr(client, "_ready", None)
    monkeypatch.setattr(client, "_ready_reason", "")
    monkeypatch.setattr(client, "ensure_key", lambda: (TEST_KEY, ""))
    yield


@pytest.mark.skipif(not win.IS_WINDOWS, reason="helper 只在 Windows 上有意义")
def test_missing_port_file_means_helper_not_started(fresh_probe, monkeypatch):
    """没有端口文件 ⇒ "未启动"，而不是含糊的"不可用"。"""
    monkeypatch.setattr(client, "_read_port", lambda: 0)

    ok, reason = client.available()

    assert ok is False
    assert "未启动" in reason


@pytest.mark.skipif(not win.IS_WINDOWS, reason="helper 只在 Windows 上有意义")
def test_unresponsive_helper_clears_the_stale_port_file(fresh_probe, monkeypatch):
    """连不上时清掉端口文件：它多半是上一次运行的残留。

    不清就会每次白试同一个死端口，用户看到的是"一直不可用"，而真正原因
    （helper 没在跑）永远不会出现在任何一次探测里。
    """
    cleared: list[int] = []
    monkeypatch.setattr(client, "_read_port", lambda: 45678)
    monkeypatch.setattr(client, "_ping", lambda port, key, timeout: (False, False, "ConnectionRefused"))
    monkeypatch.setattr(client, "_clear_port_file", lambda: cleared.append(1))

    ok, reason = client.available()

    assert ok is False
    assert "无响应" in reason
    assert cleared == [1]


@pytest.mark.skipif(not win.IS_WINDOWS, reason="helper 只在 Windows 上有意义")
def test_alive_but_unelevated_helper_is_not_reported_as_ready(fresh_probe, monkeypatch):
    """helper 活着但没拿到管理员权限 ⇒ 仍然不可用。

    `CreateProcessAsUserW` 在这种情形下照旧 `err=5`。把"进程在跑"当成
    "档位可用"，用户会看到档位亮着、命令全部失败 —— 最难排查的那种状态。
    """
    monkeypatch.setattr(client, "_read_port", lambda: 45678)
    monkeypatch.setattr(client, "_ping", lambda port, key, timeout: (True, False, ""))

    ok, reason = client.available()

    assert ok is False
    assert "未获得管理员权限" in reason


@pytest.mark.skipif(not win.IS_WINDOWS, reason="helper 只在 Windows 上有意义")
def test_engine_elevated_does_not_ask_the_user_for_uac(monkeypatch):
    """引擎自己已提权时，`restricted` 走本地路径，界面不该催用户点 UAC。

    这条钉住 `helper_status` 的字段分工：`available` 与 `engine_elevated`
    是两件事，前端要能据此隐藏"启用助手"入口。
    """
    monkeypatch.setattr(win_launch, "helper_available", lambda: (False, "提权 helper 未启动"))
    monkeypatch.setattr(win_launch, "is_admin", lambda: True)

    status = win_launch.helper_status()

    assert status["available"] is False
    assert status["engine_elevated"] is True
    assert status["detail"]  # 不可用的原因要带出来，不能只有布尔


# ── 不暴露任意执行 ──────────────────────────────────────


def _launch_args(**overrides):
    """一条合法请求的最小载荷，按需覆盖字段。"""
    payload = {"argv": ["cmd.exe", "/c", "echo hi"], "cwd": "C:\\"}
    payload.update(overrides)
    return payload


@pytest.fixture
def captured_launch(monkeypatch, tmp_path):
    """把真正的受限启动换成替身，只记录它收到的参数。

    `_handle_exec` 的唯一出口必须是 `launch_restricted`。替身在这里的作用是
    **证明**它确实走那条路 —— 如果哪天有人加了一条直接执行的捷径，这个测试
    会看到替身没被调用而变红。

    替身接收 `**kwargs`：调用方现在还会下发 `target_pid` / `expect_creation_time`
    （调用方身份，P0-1），签名写死会让"新增一个参数"变成"整组测试失败"。
    """
    seen: dict[str, object] = {}
    # 调用方身份是模块级全局；不清会让上一个用例（或真实 serve）的记录带进来。
    monkeypatch.setattr(helper, "_caller", None)

    def fake_launch(argv, *, cwd, env, timeout, max_output, **kwargs):
        seen.update(
            argv=argv, cwd=cwd, env=env, timeout=timeout, max_output=max_output, **kwargs
        )
        return win_launch.LaunchResult(0, "out", "", "")

    monkeypatch.setattr(win_launch, "launch_restricted", fake_launch)
    return seen, tmp_path


def test_valid_request_goes_through_restricted_launch(captured_launch):
    """合法请求原样交给 `launch_restricted` —— helper 自己不执行任何东西。"""
    seen, workspace = captured_launch

    payload = helper._handle_exec(_launch_args(cwd=str(workspace)))

    # 身份字段（protocol / token_* / caller_user_sid）随协议 v2 新增，
    # 旧引擎只读前五个键，因此这里是"包含"断言而不是整体相等。
    assert {
        "started": payload["started"],
        "spawn_error": payload["spawn_error"],
        "exit_code": payload["exit_code"],
        "stdout": payload["stdout"],
        "stderr": payload["stderr"],
    } == {
        "started": True,
        "spawn_error": "",
        "exit_code": 0,
        "stdout": "out",
        "stderr": "",
    }
    assert seen["argv"] == ["cmd.exe", "/c", "echo hi"]
    assert seen["cwd"] == str(workspace)


@pytest.mark.parametrize(
    "payload",
    [
        {"argv": "cmd.exe /c echo hi", "cwd": "C:\\"},
        {"argv": [], "cwd": "C:\\"},
        {"argv": ["cmd.exe", ""], "cwd": "C:\\"},
        {"argv": ["cmd.exe", 1], "cwd": "C:\\"},
        {"argv": ["cmd.exe"], "cwd": "relative\\path"},
        {"argv": ["cmd.exe"], "cwd": ""},
        {"argv": ["cmd.exe"], "cwd": "C:\\definitely-not-here-9f3a"},
        {"argv": ["cmd.exe"], "cwd": "C:\\", "env": {"A": 1}},
        {"argv": ["cmd.exe"], "cwd": "C:\\", "env": ["A=B"]},
        {"argv": ["cmd.exe"], "cwd": "C:\\", "timeout": float("nan")},
        {"argv": ["cmd.exe"], "cwd": "C:\\", "timeout": -1},
        {"argv": ["cmd.exe"], "cwd": "C:\\", "max_output": 0},
        {"argv": ["cmd.exe"], "cwd": "C:\\", "max_output": 1.5},
    ],
)
def test_malformed_requests_are_refused(payload, monkeypatch):
    """每一条都是一个具体的滥用面，逐条拒绝。

    * `argv` 是字符串 —— 会诱导实现"顺手交给 shell"，那就是任意命令执行；
    * 空 `argv` / 空串项 —— 没有明确目标进程，行为不可预期；
    * `cwd` 相对路径 —— 提权进程按"当前目录"解析，是经典提权手法；
    * `cwd` 不存在 —— 启动必然失败，不如直接说清楚；
    * `env` 非字符串键值 —— 环境块构造会静默变形；
    * `timeout` NaN / 负数、`max_output` 非正整数 —— 绕过上限的入口。

    全部断言**没有**走到 `launch_restricted`：拒绝必须发生在起进程之前。
    """
    called: list[int] = []
    monkeypatch.setattr(win_launch, "launch_restricted", lambda *a, **k: called.append(1))

    with pytest.raises(helper._BadRequestError):
        helper._handle_exec(payload)

    assert called == []


def test_timeout_and_output_caps_are_clamped_not_rejected(captured_launch):
    """超上限的 `timeout` / `max_output` 夹到上限而不是整条拒绝。

    引擎已经按档位算好了超时；这里拒绝只会让长构建莫名其妙失败。
    但**上限**必须真的生效 —— 否则一条请求能长期占住提权进程的线程。
    """
    seen, workspace = captured_launch

    helper._handle_exec(
        _launch_args(cwd=str(workspace), timeout=10**6, max_output=10**9)
    )

    assert seen["timeout"] == helper.MAX_TIMEOUT_SECONDS
    assert seen["max_output"] == helper.MAX_OUTPUT_BYTES


def test_helper_defaults_never_allow_unbounded_execution(captured_launch):
    """省略 `timeout` / `max_output` 时用默认值，且都在上限之内。"""
    seen, workspace = captured_launch

    helper._handle_exec(_launch_args(cwd=str(workspace)))

    assert seen["timeout"] == helper.DEFAULT_TIMEOUT_SECONDS
    assert seen["max_output"] == helper.MAX_OUTPUT_BYTES
    assert seen["env"] == {}


def test_helper_only_listens_on_loopback():
    """绝不监听 `0.0.0.0`：它提供的是提权能力，不能暴露在网络上。"""
    assert helper.BIND_ADDRESS == proto.LOOPBACK == "127.0.0.1"


# ── 降级路径：不可用时拒绝执行，绝不退回无隔离 ───────────────


def test_missing_helper_returns_spawn_error_instead_of_running_unisolated(monkeypatch, tmp_path):
    """helper 不可用且引擎未提权 ⇒ 如实返回 `spawn_error`，绝不执行。

    这是整个隔离方案最容易被"优化"掉的一步：`spawn_error` 非空时若退回普通
    `CreateProcess`，等于沙箱一坏就静默降级成无隔离执行，而用户以为自己在跑
    受限令牌。所以这里连本地路径都不许碰。
    """
    local_called: list[int] = []
    monkeypatch.setattr(
        win_launch,
        "_via_helper",
        lambda *a, **k: win_launch.LaunchResult(None, "", "", "提权 helper 未启动"),
    )
    monkeypatch.setattr(win_launch, "is_admin", lambda: False)
    monkeypatch.setattr(
        win_launch, "_launch_restricted_local", lambda *a, **k: local_called.append(1)
    )

    result = win_launch.launch_restricted(["cmd.exe", "/c", "echo hi"], cwd=str(tmp_path), env={})

    assert result.spawn_error == "提权 helper 未启动"
    assert result.started is False
    assert local_called == []


def test_helper_not_applicable_falls_through_to_local_path(monkeypatch, tmp_path):
    """helper 这条路"不适用"（没装/没起）时返回 `None`，交给调用方另寻路径。

    与上一条的区别：`None` 表示"这条路不该走"（例如引擎自己已提权），
    而 `spawn_error` 表示"这条路试过了、失败了"。把两者混成一种，就没法
    区分"该降级"与"该拒绝"。
    """
    monkeypatch.setattr(win_launch, "helper_available", lambda: (False, "提权 helper 未启动"))

    assert win_launch._via_helper(["cmd.exe"], cwd=str(tmp_path), env={}, timeout=1, max_output=1) is None


def test_helper_exec_without_port_reports_error_rather_than_silence(monkeypatch):
    """helper 客户端拿不到端口时返回带原因的 `spawn_error`。"""
    monkeypatch.setattr(client, "ensure_key", lambda: (TEST_KEY, ""))
    monkeypatch.setattr(client, "_read_port", lambda: 0)

    result = client.helper_exec(["cmd.exe"], cwd="C:\\", env={}, timeout=1, max_output=1)

    assert result.started is False
    assert "未启动" in result.spawn_error


# ── UAC 取消：用户的正常选择，不是故障 ─────────────────────


def _fake_shell32(code: int):
    """构造一个只回固定返回码的 `shell32` 替身。

    用 `SimpleNamespace` + 普通函数，而不是类实例：`spawn_elevated` 会给
    `ShellExecuteW` 设 `restype` / `argtypes`（64 位下不声明会截断指针），
    而**绑定方法**不支持设置属性 —— 用类实例会直接 `AttributeError`，
    测出来的是替身的毛病，不是被测代码的。
    """
    import types

    def _shell_execute_w(*args):
        return code

    return types.SimpleNamespace(ShellExecuteW=_shell_execute_w)


@pytest.mark.skipif(not win.IS_WINDOWS, reason="ShellExecuteW 是 Windows 专有")
@pytest.mark.parametrize(
    ("return_code", "last_error"),
    [
        (5, 0),  # SE_ERR_ACCESSDENIED：用户点了"否"，最常见形态
        (2, 1223),  # 返回值是别的失败码，但 last error 是 ERROR_CANCELLED
    ],
)
def test_uac_cancel_is_reported_as_cancelled(return_code, last_error, monkeypatch):
    """用户点了"否" ⇒ `cancelled=True`，界面据此说"已取消授权"而不是"启用失败"。

    取消有两个来源，都要认：返回值 5（`SE_ERR_ACCESSDENIED`），以及
    `GetLastError()==ERROR_CANCELLED(1223)`。

    **1223 绝不可能是返回值**：`ShellExecuteW` 的返回值 >32 一律按成功解释，
    所以把 1223 当返回值去判，会把一次失败读成"启动成功"。这个区别是本用例
    刻意分成两组参数的原因 —— 只写一个 `code in (5, 1223)` 的判断看似更简洁，
    但它对 1223 恒不成立，等于这条分支从来没被验证过。
    """
    monkeypatch.setattr(client, "ensure_key", lambda: (TEST_KEY, ""))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _fake_shell32(return_code))
    monkeypatch.setattr(ctypes, "get_last_error", lambda: last_error)

    ok, reason, cancelled = client.spawn_elevated(4321)

    assert ok is False
    assert cancelled is True
    assert "取消" in reason


@pytest.mark.skipif(not win.IS_WINDOWS, reason="ShellExecuteW 是 Windows 专有")
def test_other_shell_execute_errors_are_not_reported_as_cancel(monkeypatch):
    """真正的启动失败不能伪装成"用户取消"：后者会让人以为不需要排障。"""
    monkeypatch.setattr(client, "ensure_key", lambda: (TEST_KEY, ""))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: _fake_shell32(2))  # ERROR_FILE_NOT_FOUND
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 2)

    ok, reason, cancelled = client.spawn_elevated(4321)

    assert ok is False
    assert cancelled is False
    assert "ShellExecuteW=2" in reason
