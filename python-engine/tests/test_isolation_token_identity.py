"""P0-1：受限进程的 token 用户必须是**调用方**，不能是管理员。

## 这条不变量为什么单独一组测试

`restricted` 档此前用**提权 helper 自己的**令牌派生受限令牌。`WRITE_RESTRICTED`
的 restricting SIDs **只在评估写访问时**参与检查（`CreateRestrictedToken` 原文），
所以那个子进程实际上：读遍全盘、身份是管理员、只在写某些目录时被拦。
"隔离"变成了"靠没有可用提权路径"维持的假设。

修复后令牌派生自**调用方（引擎）的令牌**，子进程因此运行在调用方的
用户/完整性级别下。这里钉住三件事，全部可在无 UAC、无管理员的环境下跑：

1. **身份来源** —— `restricted_token_for_caller()` 的 `source` 是 `"caller"`
   时，受限令牌确实由调用方令牌派生（`create_restricted_token_from` 收到的是
   那个句柄，而不是 `None`）；
2. **降级不静默** —— 拿不到调用方令牌时仍执行（限制性 SID 生效），但必须带
   `degrade_reason=token_target_unavailable`；
3. **请求不能指定身份** —— helper 只认启动时记录的父进程；请求体里的
   `caller_pid` 只做交叉校验，改不动身份，否则攻破引擎就等于"以任意用户执行"。

真机验证步骤（`whoami /groups`）写在交付报告里，不放进自动化用例 ——
它需要 UAC，而这里刻意保持"零特权可跑"。
"""

from __future__ import annotations

import json
import os

import pytest

from app.isolation import elevated_client as client
from app.isolation import elevated_helper as helper
from app.isolation import elevated_protocol as proto
from app.isolation import win_launch, win_token

#: 一个普通用户 SID（RID 1001）：不是管理员。
USER_SID = "S-1-5-21-1935223896-1533613557-3741183318-1001"
#: 内置 Administrator（RID 500）：管理员身份的可靠标识。
ADMIN_SID = "S-1-5-21-1935223896-1533613557-3741183318-500"

#: 受限令牌的 sentinel 句柄。用例里不让真的去建令牌，也就不会泄漏句柄。
FAKE_TOKEN = object()


@pytest.fixture(autouse=True)
def _clean_globals(monkeypatch):
    """全局状态清零：`_caller` / 探测缓存都是模块级变量。"""
    monkeypatch.setattr(helper, "_caller", None)
    monkeypatch.setattr(client, "_ready", None)
    monkeypatch.setattr(client, "_ready_reason", "")
    monkeypatch.setattr(client, "_helper_protocol", None)
    # 句柄是 sentinel：任何真实的 `CloseHandle(sentinel)` 都会因类型不符抛异常，
    # 从而把"用例其实在测真实 Win32"这件事暴露成噪音。统一换成 no-op。
    monkeypatch.setattr(win_token, "_k32", lambda: _NoopK32())
    yield


# ── ① 管理员身份的判定（不变量本身）───────────────────────────


@pytest.mark.parametrize(
    ("sid", "elevated", "is_admin"),
    [
        (ADMIN_SID, False, True),  # 内置 Administrator，未提权也算管理员身份
        (ADMIN_SID, True, True),
        (USER_SID, True, True),  # 提权令牌：即使 SID 是普通用户，身份已是 High IL
        (USER_SID, False, False),
        ("S-1-5-18", False, False),  # SYSTEM 不是"当前用户"，但也不是 RID 500
    ],
)
def test_administrator_identity_predicate(sid, elevated, is_admin):
    """`is_administrator_identity` 是运行时与测试共用的唯一判定。

    刻意**不**用"在 `BUILTIN\\Administrators` 组里"：UAC 下普通用户令牌也带这个组
    （deny-only），按组判定会把所有管理员账户的普通会话误报成"以管理员运行"——
    而这个函数恰恰是用来区分这两种情况的。
    """
    assert win_token.is_administrator_identity(sid, elevated) is is_admin


# ── ② 身份来源：受限令牌派生自调用方令牌 ──────────────────────


def test_restricted_token_is_derived_from_the_caller_token(monkeypatch):
    """`source == "caller"` ⇒ `CreateRestrictedToken` 的源是调用方令牌句柄。

    这是 P0-1 的核心：源令牌决定子进程的**用户身份**；restricting SIDs 只额外
    约束写访问。若这里退回 `None`（=用自己的令牌），子进程的用户就是 helper
    自己（通常是管理员）。
    """
    caller_handle = object()
    seen: dict[str, object] = {}

    monkeypatch.setattr(win_token, "open_process_token", lambda pid, access=None: (caller_handle, ""))
    monkeypatch.setattr(win_token, "token_user_sid", lambda handle: USER_SID)
    monkeypatch.setattr(win_token, "token_elevated", lambda handle: False)
    monkeypatch.setattr(win_token, "process_creation_time", lambda pid: 777)

    def fake_create(source=None):
        seen["source"] = source
        return FAKE_TOKEN, win_token.SYNTHETIC_SID

    monkeypatch.setattr(win_token, "create_restricted_token_from", fake_create)

    target = win_token.restricted_token_for_caller(4321, expect_creation_time=777)

    assert seen["source"] is caller_handle, (
        "受限令牌必须派生自调用方令牌；派生自 helper 自身令牌时，"
        "子进程的用户身份就是管理员（限制性 SID 只约束写访问）"
    )
    assert target.source == "caller"
    assert target.user_sid == USER_SID
    assert target.elevated is False
    assert target.reason == "", "正常路径不该有降级理由"
    assert target.token is FAKE_TOKEN


def test_own_process_is_treated_as_the_caller(monkeypatch):
    """`target_pid` 为空或就是自己 ⇒ 用本进程令牌，且**不算降级**。

    引擎自己已提权时调用方就是本进程，这不是降权失败；把它记成降级会让每次
    探测都带上一条假告警。
    """
    calls: list[object] = []

    def fake_create(source=None):
        calls.append(source)
        return FAKE_TOKEN, win_token.SYNTHETIC_SID

    monkeypatch.setattr(win_token, "create_restricted_token_from", fake_create)
    monkeypatch.setattr(win_token, "token_user_sid", lambda handle: USER_SID)
    monkeypatch.setattr(win_token, "token_elevated", lambda handle: False)

    for pid in (None, 0, os.getpid()):
        target = win_token.restricted_token_for_caller(pid)
        assert target.source == "self"
        assert target.reason == ""

    assert calls == [None, None, None]


# ── ③ 降级：仍执行、仍带限制性 SID，但理由必须显式 ────────────


def test_unavailable_caller_token_degrades_with_reason_but_keeps_restricting_sids(monkeypatch):
    """调用方令牌拿不到 ⇒ 退回本进程令牌，`reason=token_target_unavailable`。

    为什么**不**拒绝执行：受限令牌 + 独立桌面仍然生效（写访问照样被 restricting
    SIDs 约束），直接拒绝会让"引擎刚重启、父进程一时读不到"变成整档不可用。
    但降级绝不允许静默 —— `reason` 会被写进响应、日志与状态。
    """
    created: list[object] = []

    monkeypatch.setattr(
        win_token, "open_process_token", lambda pid, access=None: (None, "OpenProcess 失败 err=5")
    )

    def fake_create(source=None):
        created.append(source)
        return FAKE_TOKEN, win_token.SYNTHETIC_SID

    monkeypatch.setattr(win_token, "create_restricted_token_from", fake_create)
    monkeypatch.setattr(win_token, "token_user_sid", lambda handle: ADMIN_SID)
    monkeypatch.setattr(win_token, "token_elevated", lambda handle: True)

    target = win_token.restricted_token_for_caller(4321)

    assert target.reason == win_token.TOKEN_TARGET_UNAVAILABLE == "token_target_unavailable"
    assert target.source == "self"
    assert created == [None], "降级路径仍要造出受限令牌（限制性 SID 必须继续生效）"
    assert target.token is FAKE_TOKEN
    assert "err=5" in target.detail, "降级细节要保留，否则排障时只剩一句'降级了'"


def test_reused_pid_is_rejected_before_the_token_is_trusted(monkeypatch):
    """pid 被复用 ⇒ 不采信那个令牌，降级并记录。

    父进程退出后 pid 会被系统回收；不校验创建时间就可能把"恰好拿到同一个 pid
    的另一个进程"当成调用方 —— 那等于让 helper 用别人的身份起进程。
    """
    monkeypatch.setattr(win_token, "open_process_token", lambda pid, access=None: (object(), ""))
    monkeypatch.setattr(win_token, "process_creation_time", lambda pid: 999)  # 与预期不符
    monkeypatch.setattr(win_token, "create_restricted_token_from", lambda source=None: (FAKE_TOKEN, win_token.SYNTHETIC_SID))

    target = win_token.restricted_token_for_caller(4321, expect_creation_time=777)

    assert target.reason == win_token.TOKEN_TARGET_UNAVAILABLE
    assert target.source == "self"
    assert "复用" in target.detail


def test_degrade_reason_survives_into_the_launch_result(monkeypatch):
    """降级理由必须一路带到 `LaunchResult.degrade_reason`（不能停在内部变量里）。"""
    monkeypatch.setattr(
        win_token,
        "restricted_token_for_caller",
        lambda pid, **_kw: win_token.TargetToken(
            token=FAKE_TOKEN,
            user_sid=ADMIN_SID,
            elevated=True,
            source="self",
            reason=win_token.TOKEN_TARGET_UNAVAILABLE,
            detail="调用方不可达",
        ),
    )
    # 让流程在桌面这一步停下：本用例只关心"理由有没有带出来"。
    monkeypatch.setattr(win_launch, "ensure_desktop", lambda extra="": (False, "桌面打不开"))
    monkeypatch.setattr(win_token, "_k32", lambda: _NoopK32())

    result = win_launch._launch_restricted_local(  # noqa: SLF001 — 结构断言
        ["cmd.exe"], cwd="C:\\", env={}, timeout=1, max_output=10, target_pid=4321
    )

    assert result.started is False
    assert result.degrade_reason == "token_target_unavailable"
    assert result.token_target == "self"
    assert result.token_elevated is True


class _NoopK32:
    """`CloseHandle` 的替身：用例里没有真实句柄，避免对无效句柄做真调用。"""

    def CloseHandle(self, _handle):  # noqa: N802 — 与 Win32 同名
        return True


def test_desktop_dacl_covers_the_caller_identity(monkeypatch):
    """以调用方身份运行时，桌面 DACL 必须显式包含那个 SID。

    标准用户走"输入管理员凭据"的 UAC 时，helper 运行在**另一个账户**下；
    只授权 helper 自己的桌面，那个子进程连自己所在的桌面都打不开 ——
    表现为进程起来了、没有任何输出。
    """
    seen: dict[str, str] = {}

    monkeypatch.setattr(
        win_token,
        "restricted_token_for_caller",
        lambda pid, **_kw: win_token.TargetToken(
            token=FAKE_TOKEN, user_sid=USER_SID, elevated=False, source="caller"
        ),
    )

    def fake_desktop(extra=""):
        seen["extra"] = extra
        return False, "停在这里"

    monkeypatch.setattr(win_launch, "ensure_desktop", fake_desktop)

    win_launch._launch_restricted_local(  # noqa: SLF001
        ["cmd.exe"], cwd="C:\\", env={}, timeout=1, max_output=10, target_pid=4321
    )

    assert seen["extra"] == USER_SID


# ── ④ 请求不能指定身份 ────────────────────────────────────────


def _identity(pid: int = 4321) -> helper.CallerIdentity:
    return helper.CallerIdentity(pid=pid, user_sid=USER_SID, elevated=False, creation_time=777)


def _stub_launch(monkeypatch) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_launch(argv, *, cwd, env, timeout, max_output, **kwargs):
        seen.update(argv=argv, cwd=cwd, **kwargs)
        return win_launch.LaunchResult(
            0, "ok", "", "", token_target="caller", token_user_sid=USER_SID, token_elevated=False
        )

    monkeypatch.setattr(win_launch, "launch_restricted", fake_launch)
    return seen


def test_helper_passes_the_recorded_caller_identity(monkeypatch, tmp_path):
    """helper 把**记录的**调用方 pid 与创建时间一起下发给 `launch_restricted`。"""
    helper.set_caller(_identity())
    seen = _stub_launch(monkeypatch)

    payload = helper._handle_exec({"argv": ["cmd.exe"], "cwd": str(tmp_path), "protocol": 2})

    assert seen["target_pid"] == 4321
    assert seen["expect_creation_time"] == 777
    assert payload["token_target"] == "caller"
    assert payload["token_user_sid"] == USER_SID
    assert payload["caller_user_sid"] == USER_SID


def test_request_pid_cannot_override_the_recorded_identity(monkeypatch, tmp_path):
    """请求里的 `caller_pid` 改了也不影响身份。

    否则攻破引擎的一方只要写一个 `caller_pid=<管理员进程>`，就能让提权 helper
    打开那个进程的令牌 —— 那是比原问题更严重的提权。
    """
    helper.set_caller(_identity(pid=4321))
    seen = _stub_launch(monkeypatch)

    helper._handle_exec(
        {"argv": ["cmd.exe"], "cwd": str(tmp_path), "protocol": 2, "caller_pid": 4}
    )

    assert seen["target_pid"] == 4321, "身份只能来自 helper 启动时记录的父进程"


def test_no_recorded_caller_never_trusts_the_request(monkeypatch, tmp_path):
    """未记录调用方（`--parent-pid=0`）⇒ 退回本助手令牌，`target_pid=None`。

    这条路上无法验证请求方是谁，因此**不能**按请求里的 pid 取令牌；
    `launch_restricted` 会自己填 `degrade_reason=token_target_unavailable`。
    """
    helper.set_caller(None)
    seen = _stub_launch(monkeypatch)

    helper._handle_exec(
        {"argv": ["cmd.exe"], "cwd": str(tmp_path), "protocol": 2, "caller_pid": 4}
    )

    assert seen["target_pid"] is None
    assert seen["expect_creation_time"] is None


def test_resolve_caller_reads_the_parent_identity(monkeypatch):
    """启动时按 `--parent-pid` 读调用方身份（SID + 创建时间）。"""
    monkeypatch.setattr(win_token, "process_identity", lambda pid: (USER_SID, False))
    monkeypatch.setattr(win_token, "process_creation_time", lambda pid: 777)

    identity = helper.resolve_caller(4321)

    assert identity is not None
    assert (identity.pid, identity.user_sid, identity.elevated) == (4321, USER_SID, False)
    assert identity.creation_time == 777, "创建时间是防 pid 复用的唯一依据"
    assert helper.resolve_caller(0) is None


# ── ⑤ 协议版本与双向兼容 ──────────────────────────────────────


def test_protocol_is_versioned_and_v1_stays_compatible():
    """升到 v2，但 v1 helper 仍可通话（只是不能降权）。"""
    assert proto.PROTOCOL_VERSION == 2
    assert proto.MIN_COMPATIBLE_PROTOCOL == 1

    assert proto.compatible(1) is True
    assert proto.compatible(2) is True
    # 0 / 未来版本 / 非法值都不能猜着解析：半懂不懂地解析"起进程"指令更危险
    assert proto.compatible(0) is False
    assert proto.compatible(proto.PROTOCOL_VERSION + 1) is False
    assert proto.compatible(None) is False
    assert proto.compatible("v2") is False


def test_incompatible_helper_reason_is_actionable():
    """不兼容的版本必须给出**可执行**的动作，而不是"版本不一致"四个字。"""
    reason = proto.incompatible_reason(9)

    assert str(proto.PROTOCOL_VERSION) in reason
    assert "重新启用" in reason, f"原因要能指导用户动作，实际：{reason}"


def test_old_helper_notes_that_identity_downgrade_is_unavailable():
    """v1 helper 的说明必须点明"无法按调用方身份降权"。"""
    note = proto.degrade_note(1)

    assert note, "v1 helper 只能用自己的令牌，这件事必须说出来"
    assert "调用方" in note
    assert proto.degrade_note(proto.PROTOCOL_VERSION) == ""


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class _FakeConn:
    """最小 HTTP 替身：按顺序回放预置响应，并记录请求体。"""

    def __init__(self, responses: list[tuple[int, dict]], seen: list[dict]) -> None:
        self._responses = responses
        self._seen = seen

    def request(self, method, path, body=None, headers=None):  # noqa: D401, ANN001
        self._seen.append({"method": method, "path": path, "body": body})

    def getresponse(self):
        status, payload = self._responses.pop(0)
        return _FakeResponse(status, json.dumps(payload).encode())

    def close(self):
        return None


def _install_conn(monkeypatch, responses: list[tuple[int, dict]]) -> list[dict]:
    seen: list[dict] = []
    monkeypatch.setattr(
        client.http.client, "HTTPConnection", lambda *a, **k: _FakeConn(responses, seen)
    )
    monkeypatch.setattr(client, "_read_port", lambda: 45678)
    monkeypatch.setattr(client, "ensure_key", lambda: (b"k" * proto.KEY_BYTES, ""))
    return seen


def test_engine_reports_incompatible_helper_as_unavailable(monkeypatch):
    """引擎侧看到不可用版本 ⇒ `available()=False`，原因是可行动的说明。"""
    _install_conn(monkeypatch, [(200, {"ok": True, "protocol": 99, "elevated": True})])

    ok, reason = client.available()

    assert ok is False
    assert "重新启用" in reason


def test_v1_helper_exec_is_recorded_as_token_target_unavailable(monkeypatch):
    """v1 helper 不报身份字段 ⇒ 引擎自己补 `token_target_unavailable`。

    否则"升级助手才能降权"这件事只会体现在一条没人看的日志里，
    而界面/状态都会显示得很正常。
    """
    seen = _install_conn(
        monkeypatch,
        [
            (200, {"ok": True, "protocol": 1, "elevated": True}),
            (200, {"started": True, "spawn_error": "", "exit_code": 0, "stdout": "", "stderr": ""}),
        ],
    )
    assert client.available()[0] is True

    result = client.helper_exec(["cmd.exe"], cwd="C:\\", env={}, timeout=1, max_output=10)

    assert result.started is True
    assert result.degrade_reason == proto.REASON_TOKEN_TARGET_UNAVAILABLE
    posted = json.loads(seen[-1]["body"])
    assert posted["protocol"] == proto.PROTOCOL_VERSION
    assert posted["caller_pid"] == os.getpid()


def test_v2_helper_identity_fields_are_propagated(monkeypatch):
    """v2 helper 报的身份字段原样回到 `LaunchResult`（供状态与断言使用）。"""
    _install_conn(
        monkeypatch,
        [
            (200, {"ok": True, "protocol": 2, "elevated": True}),
            (
                200,
                {
                    "started": True,
                    "spawn_error": "",
                    "exit_code": 0,
                    "stdout": "",
                    "stderr": "",
                    "token_target": "caller",
                    "token_user_sid": USER_SID,
                    "token_elevated": False,
                    "degrade_reason": "",
                },
            ),
        ],
    )
    # 引擎侧的交叉核对会读自己的 SID；这里让它与 helper 报的一致（真实场景即如此）。
    monkeypatch.setattr(win_token, "current_user_sid", lambda: USER_SID)
    assert client.available()[0] is True

    result = client.helper_exec(["cmd.exe"], cwd="C:\\", env={}, timeout=1, max_output=10)

    assert result.token_target == "caller"
    assert result.token_user_sid == USER_SID
    assert result.token_elevated is False
    assert result.degrade_reason == ""
    assert not win_token.is_administrator_identity(result.token_user_sid, result.token_elevated)
