"""提权执行 helper —— 唯一职责：**用受限令牌起进程**。

    python -m app.isolation.elevated_helper --parent-pid=1234

## 为什么要单独一个提权进程

`CreateProcessAsUserW` 需要 `SeIncreaseQuotaPrivilege`，该特权只在提权令牌上。
本机实测（普通账户）：`err=5`；退路 `CreateProcessWithTokenW` 需要
`SeImpersonatePrivilege`（`err=1314`）。所以 `restricted` 档在未提权的引擎里
**永远**起不来 —— 这不是配置问题，是令牌权限的硬约束。

把整个引擎提权是反向做法：引擎承载模型输出，是被攻击面最大的进程，应该留在
Medium IL。所以只提权这一个瘦进程。

## 安全边界（这是本模块的全部重点）

1. **本进程从不直接执行命令。** 收到请求后只调用
   `win_launch.launch_restricted` —— 目标进程带受限令牌 + 独立桌面，权限比
   引擎自身更低。即使引擎被完全攻破，攻击者拿到的也只是"受限执行"通道，
   强度不降。这是整个设计成立的前提，改动前请先想清楚。
1b. **子进程的身份是调用方，不是本助手。** 受限令牌派生自**调用方令牌**
   （`CallerIdentity`，启动时按 `--parent-pid` 记录，带创建时间防 pid 复用），
   而不是本助手的提权令牌。否则限制性 SID 只约束写访问，子进程读遍全盘且
   身份仍是管理员 —— 那不是"降权"。拿不到调用方令牌时**允许**降级，
   但必须带 `degrade_reason=token_target_unavailable`。
2. **只监听环回。** 绝不 `0.0.0.0`。它提供的是提权能力。
3. **每个请求都要 HMAC 签名。** 密钥由引擎写入 `%LOCALAPPDATA%`，不走命令行
   （命令行对同机任意进程可见）。签名失败立刻断开且不记录内容 —— 那可能是探测。
4. **父进程死则自杀。** 否则会留一个提权进程在用户机器上，且无人能管。
5. **参数从严。** `timeout` / `max_output` 有硬上限；`argv` 必须是列表
   （不接受字符串，杜绝"顺手加个 shell"）；`cwd` 必须存在且是绝对路径。

## 不做什么

不提供"任意提权执行"（那等于把引擎提权了，只是换了个进程）；
不读写工作区之外的文件；不持久化任何请求内容。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

logger = logging.getLogger("chiron.elevated_helper")

#: 端口文件在 helper **真正绑定成功后**才写 —— 提前写会让引擎连到一个还不存在的端口，
#: 表现为"helper 起了但连不上"，比"没起"更难排查。
#:
#: 用 0 让内核分配端口：固定端口会被抢注，而提权端口被抢注是安全问题。
BIND_ADDRESS = "127.0.0.1"

#: 单次执行超时的上限（秒）。请求可以更短，但不能更长 ——
#: 否则一个请求能长期占住提权进程的线程。
MAX_TIMEOUT_SECONDS = 300.0
DEFAULT_TIMEOUT_SECONDS = 60.0

#: 单次输出上限。与 `launch_restricted` 的默认 `max_output` 对齐。
MAX_OUTPUT_BYTES = 1_000_000

#: 父进程存活检查间隔（秒）。父进程退出后最多这么久 helper 就消失。
_PARENT_POLL_SECONDS = 2.0


class CallerIdentity:
    """helper **启动时**记录的调用方（引擎）身份。

    ## 为什么身份在启动时定，而不是每个请求带

    请求体里的 `caller_pid` 只是**交叉校验**。若让请求决定身份，攻破引擎的一方
    就能让提权 helper 打开任意进程的令牌并以其身份起进程 —— 那是把引擎的
    攻击面放大成"以任意用户身份执行"，比原来的问题（子进程继承 helper 的
    管理员身份）更糟。

    启动时的 `--parent-pid` 由引擎在 `spawn_elevated` 里给定，且 helper 会用
    `creation_time` 防 pid 复用：父进程退出后 pid 会被系统回收，不校验创建时间
    就可能把"恰好拿到同一个 pid 的另一个进程"当成调用方。
    """

    __slots__ = ("creation_time", "elevated", "pid", "user_sid")

    def __init__(self, pid: int, user_sid: str, elevated: bool, creation_time: int | None) -> None:
        self.pid = pid
        self.user_sid = user_sid
        self.elevated = elevated
        self.creation_time = creation_time

    def as_dict(self) -> dict[str, Any]:
        return {"pid": self.pid, "user_sid": self.user_sid, "elevated": self.elevated}


#: 当前记录的调用方身份。`None` = 未知（helper 以 `--parent-pid=0` 启动，
#: 或父进程令牌读不到）⇒ 受限进程只能退回 helper 自身令牌，并记录
#: `token_target_unavailable`。
_caller: CallerIdentity | None = None


def set_caller(identity: CallerIdentity | None) -> None:
    """设置/清除调用方身份（`serve()` 与测试使用）。"""
    global _caller
    _caller = identity


def caller_identity() -> CallerIdentity | None:
    return _caller


def resolve_caller(pid: int) -> CallerIdentity | None:
    """按 pid 读调用方身份。读不到返回 `None`（调用方会据此降级并记录理由）。"""
    if pid <= 0:
        return None
    from app.isolation import win_token

    user_sid, elevated = win_token.process_identity(int(pid))
    if not user_sid:
        logger.warning("无法读取调用方 pid=%s 的令牌身份，受限进程将退回本助手令牌", pid)
        return None
    return CallerIdentity(
        pid=int(pid),
        user_sid=user_sid,
        elevated=elevated,
        creation_time=win_token.process_creation_time(int(pid)),
    )


def _parent_alive(pid: int) -> bool:
    """父进程是否仍存活。

    Windows 上不能靠 `os.kill(pid, 0)`（它会真的发信号，行为与 POSIX 不同），
    所以走 `OpenProcess` + `GetExitCodeProcess`：句柄能打开且仍是 `STILL_ACTIVE`
    才算活着。句柄打不开按"已退出"处理 —— 那是**安全**方向的误判：
    多杀一个 helper 的代价远小于留一个孤儿提权进程。
    """
    if pid <= 0:
        return False
    try:
        import ctypes
        import ctypes.wintypes  # noqa: F401 — `ctypes.wintypes` 不是自动导入的子模块

        from app.isolation import win_token

        k32 = win_token._k32()  # noqa: SLF001 — 已配置好 argtypes
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 — 判定失败按"已退出"（安全方向）
        return False


def _watch_parent(pid: int) -> None:
    """父进程退出后自杀。作为守护线程运行。"""
    while True:
        time.sleep(_PARENT_POLL_SECONDS)
        if not _parent_alive(pid):
            # 直接 `os._exit`：此刻可能正有请求在执行，但父进程已死，没人会再收结果。
            # 留着它只会变成一个无人管理的提权进程。
            logger.warning("父进程 %s 已退出，提权 helper 随之结束", pid)
            os._exit(0)


class _Handler(BaseHTTPRequestHandler):
    """只认三个端点：`/v1/ping`、`/v1/exec` 与 `/v1/shutdown`。"""

    #: 由 `serve()` 注入。用类属性而不是实例属性，避免每次请求重建。
    shared_key: bytes = b""

    #: `serve()` 注入的 server 实例，供 `/v1/shutdown` 调用 `shutdown()`。
    #:
    #: 为什么不通过 `self.server`：`BaseHTTPRequestHandler` 确实有这个属性，
    #: 但它由 `socketserver` 在 `__init__` 里赋的是**原始类型**；这里显式留一个
    #: 带类型声明的入口，改动时不会被"看起来能用"的属性误导。
    server_instance: Any = None

    server_version = "chiron-elevated-helper/1"
    #: 关掉默认的 stderr 访问日志：它会记录路径，而路径里可能含工作区名。
    #: 需要排障时由 `logger` 显式输出。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        logger.debug("%s - %s", self.address_string(), fmt % args)

    # ── 鉴权 ──────────────────────────────────────────────

    def _authorized(self, body: bytes) -> bool:
        """校验签名。失败时**不回任何细节**、不记请求内容。"""
        from app.isolation import elevated_protocol as proto

        provided = self.headers.get(proto.SIGNATURE_HEADER)
        if proto.verify(self.shared_key, body, provided):
            return True
        logger.warning(
            "拒绝未签名的 helper 请求: %s %s from %s",
            self.command,
            self.path,
            self.address_string(),
        )
        return False

    def _reply(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ── 路由 ──────────────────────────────────────────────

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler 约定
        if self.path != "/v1/ping":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        # ping 也要签名：否则任何人都能探测"这台机器上有没有提权 helper"。
        if not self._authorized(b""):
            self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        from app.isolation import elevated_protocol as proto

        identity = _caller
        self._reply(
            HTTPStatus.OK,
            {
                "ok": True,
                "protocol": proto.PROTOCOL_VERSION,
                # 让引擎侧能把"helper 在跑但没权限"与"helper 在跑且有权限"分开，
                # 这是排查 err=5 的第一手信息。
                "elevated": _is_elevated(),
                # 调用方身份：引擎可据此确认"助手记录的调用方就是我自己"。
                # 为空表示未记录 ⇒ 该助手只能用自身令牌起进程（降权不可用）。
                "caller": identity.as_dict() if identity is not None else None,
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        # `/v1/shutdown` 让用户能主动关掉 helper。为什么必须由 helper 自己退：
        # 引擎跑在 Medium IL，**无权终止** High IL 的进程（`OpenProcess` +
        # `TerminateProcess` 会 err=5）。没有这条端点，"停用助手"只能靠杀整个
        # 应用或等父进程退出 —— 用户会以为关不掉。
        #
        # 与 `/v1/exec` 同等级鉴权：关掉提权通道也是安全动作，不能无签名就放行。
        if self.path == "/v1/shutdown":
            from app.isolation import elevated_protocol as proto

            if not self._authorized(b""):
                self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            self._reply(HTTPStatus.OK, {"ok": True, "stopping": True})
            logger.info("收到签名 shutdown 请求，helper 即将退出")
            server = type(self).server_instance
            if server is not None:
                # 在**另一个线程**里关：`shutdown()` 要等 `serve_forever()` 的循环
                # 收敛，从当前请求线程直接调用会自己等自己。
                threading.Thread(target=server.shutdown, daemon=True).start()
            return

        if self.path != "/v1/exec":
            self._reply(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        from app.isolation import elevated_protocol as proto

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > proto.MAX_FRAME_BYTES:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "bad content length"})
            return
        body = self.rfile.read(length)
        if not self._authorized(body):
            self._reply(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        try:
            request = json.loads(body)
        except ValueError:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return

        try:
            payload = _handle_exec(request)
        except _BadRequestError as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 — helper 内部异常要如实回传
            logger.exception("helper 执行异常")
            self._reply(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            return
        self._reply(HTTPStatus.OK, payload)


class _BadRequestError(Exception):
    """请求参数不合法（调用方的问题，不是 helper 故障）。"""


def _is_elevated() -> bool:
    try:
        from app.isolation import win_launch

        return bool(win_launch.is_admin())
    except Exception:  # noqa: BLE001
        return False


def _handle_exec(request: dict[str, Any]) -> dict[str, Any]:
    """校验参数并**以受限令牌**起进程。

    校验的严格程度与"这是提权进程"相称：这里放过的每一个宽松点，都是提权身份
    可以被滥用的一条路径。

    ## P0-1：子进程的身份是**调用方**，不是 helper

    受限令牌派生自 `_caller` 记录的（引擎）令牌，于是子进程运行在调用方的
    用户/完整性级别下；限制性 SID 只额外约束写访问。若 `_caller` 未知，
    `launch_restricted` 会退回本助手令牌并在响应里给出
    `degrade_reason=token_target_unavailable` —— 执行继续（限制性 SID 仍生效），
    但调用方能看见"这次没有降权"。

    请求里的 `caller_pid` **不参与身份决定**，只与记录值交叉校验。
    """
    from app.isolation import elevated_protocol as proto
    from app.isolation import win_launch

    version = request.get("protocol")
    if version is not None and not proto.compatible(version):
        raise _BadRequestError(proto.incompatible_reason(version))

    argv = request.get("argv")
    if not isinstance(argv, list) or not argv:
        raise _BadRequestError("argv 必须是非空列表")
    if not all(isinstance(item, str) and item for item in argv):
        raise _BadRequestError("argv 的每一项都必须是非空字符串")

    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        raise _BadRequestError("cwd 必须是非空字符串")
    if not os.path.isabs(cwd):
        # 相对路径会被按 helper 自己的工作目录解析，结果不可预期 ——
        # 一个提权进程按"当前目录"来解析路径，正是经典的提权手法。
        raise _BadRequestError("cwd 必须是绝对路径")
    if not os.path.isdir(cwd):
        raise _BadRequestError(f"cwd 不存在或不是目录: {cwd}")

    env = request.get("env")
    if env is None:
        env = {}
    if not isinstance(env, dict):
        raise _BadRequestError("env 必须是对象")
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise _BadRequestError("env 的键和值都必须是字符串")

    timeout = request.get("timeout", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or timeout != timeout:  # NaN 也拦
        raise _BadRequestError("timeout 必须是数字")
    timeout = float(timeout)
    if timeout <= 0:
        raise _BadRequestError("timeout 必须为正")
    # 夹到上限而不是拒绝：引擎已经按档位算好了超时，这里拒绝只会让长构建失败。
    timeout = min(timeout, MAX_TIMEOUT_SECONDS)

    max_output = request.get("max_output", MAX_OUTPUT_BYTES)
    if not isinstance(max_output, int) or max_output <= 0:
        raise _BadRequestError("max_output 必须是正整数")
    max_output = min(max_output, MAX_OUTPUT_BYTES)

    # 身份只来自启动时记录的调用方（见 `CallerIdentity`）。
    identity = _caller
    requested_pid = request.get("caller_pid")
    if identity is not None and isinstance(requested_pid, int) and requested_pid != identity.pid:
        # 不一致不改身份，只留痕：多进程引擎（子进程转发请求）会触发它，
        # 但**不能**因此放宽成"按请求里的 pid 取令牌"。
        logger.info(
            "请求声明的 caller_pid=%s 与记录的调用方 pid=%s 不一致；身份仍按记录值",
            requested_pid,
            identity.pid,
        )
    if identity is None and requested_pid:
        logger.warning(
            "未记录调用方身份（本助手不是由引擎以 --parent-pid 启动）；"
            "本次请求的 caller_pid=%s 不被采信，受限进程将退回本助手令牌（%s）",
            requested_pid,
            proto.REASON_TOKEN_TARGET_UNAVAILABLE,
        )

    result = win_launch.launch_restricted(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        max_output=max_output,
        target_pid=identity.pid if identity is not None else None,
        expect_creation_time=identity.creation_time if identity is not None else None,
    )
    return {
        "started": result.started,
        "spawn_error": result.spawn_error,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        # P0-1 的观测口。旧引擎不认识这些字段（只读上面几个），因此可安全新增。
        "protocol": proto.PROTOCOL_VERSION,
        "token_target": result.token_target,
        "token_user_sid": result.token_user_sid,
        "token_elevated": result.token_elevated,
        "degrade_reason": result.degrade_reason,
        "caller_user_sid": identity.user_sid if identity is not None else "",
    }


def serve(parent_pid: int, *, key: bytes | None = None) -> int:
    """绑定环回端口、写端口文件、进入服务循环。

    端口文件在 `serve_forever()` **之前**写：引擎侧看到文件就来连，
    此时 socket 已 `listen()`，连接会进 backlog 而不会 `ECONNREFUSED`。
    """
    from app.isolation import elevated_protocol as proto

    shared_key = key if key is not None else proto.read_key()
    if not shared_key:
        sys.stderr.write(
            f"[helper] FATAL: 读不到共享密钥（{proto.key_path()}）；"
            "请先用引擎侧生成密钥再启动 helper\n"
        )
        return 2

    server = ThreadingHTTPServer((BIND_ADDRESS, 0), _Handler)
    server.daemon_threads = True
    _Handler.shared_key = shared_key
    # 注入给 `/v1/shutdown`：它需要从请求线程里触发 `serve_forever()` 退出。
    _Handler.server_instance = server
    port = int(server.server_address[1])

    # **P0-1 的身份锚点**：在任何请求进来之前记录调用方（引擎）身份。
    # 之后每条 `/v1/exec` 都用它派生受限令牌，于是子进程以调用方用户运行。
    set_caller(resolve_caller(parent_pid))
    identity = _caller
    if identity is not None:
        logger.info(
            "调用方身份已记录: pid=%s sid=%s elevated=%s —— 受限进程将以该身份运行",
            identity.pid,
            identity.user_sid,
            identity.elevated,
        )
    else:
        logger.warning(
            "未能记录调用方身份（parent_pid=%s）；受限进程将退回本助手令牌，"
            "每次执行都会带 degrade_reason=%s",
            parent_pid,
            proto.REASON_TOKEN_TARGET_UNAVAILABLE,
        )

    # **helper 这条路径同样要启用启动特权**：提权令牌只是"持有"
    # SeAssignPrimaryTokenPrivilege / SeIncreaseQuotaPrivilege，默认禁用。
    # 这里先预热一次，失败会在"就绪"之前留下点名特权的 warning，而不是等到
    # 第一条 `/v1/exec` 才回一个光秃秃的 err=5。
    # （`launch_restricted` 每次执行前还会再启用一次 —— 令牌状态可能被策略刷新。）
    from app.isolation import win_token

    priv_ok, priv_reason = win_token.enable_launch_privileges()
    if not priv_ok:
        logger.warning("受限启动特权未能在助手启动时启用（执行时会再次尝试）: %s", priv_reason)

    try:
        proto.port_path().parent.mkdir(parents=True, exist_ok=True)
        proto.port_path().write_text(str(port), encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"[helper] FATAL: 写端口文件失败: {exc}\n")
        server.server_close()
        return 2

    logger.info(
        "提权 helper 已就绪: %s:%s (elevated=%s, parent=%s)",
        BIND_ADDRESS,
        port,
        _is_elevated(),
        parent_pid,
    )
    if not _is_elevated():
        # 不退出：让引擎侧从 ping 的 `elevated` 字段如实报告，
        # 而不是让 helper 悄悄消失、只看到"连不上"。
        logger.warning(
            "helper 未提权，restricted 档仍会 err=5 —— 请检查启动方式（UAC）"
        )

    if parent_pid > 0:
        threading.Thread(
            target=_watch_parent, args=(parent_pid,), daemon=True, name="parent-watch"
        ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        try:
            proto.port_path().unlink(missing_ok=True)
        except OSError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    parser = argparse.ArgumentParser(
        prog="python -m app.isolation.elevated_helper",
        description="提权执行 helper：只做「用受限令牌起进程」",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=0,
        help="引擎 pid；它退出后 helper 自动结束（0 表示不监视）",
    )
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    parser.add_argument(
        "--state-dir",
        default="",
        help=(
            "密钥/端口文件所在目录。由引擎下发：标准用户走 UAC 时 helper 运行在"
            "另一个管理员账户下，其 LOCALAPPDATA 与引擎不同，不指定就会读不到密钥"
            "（注意：help 文本里不能出现裸的百分号，argparse 会把它当格式符解析）"
        ),
    )
    args = parser.parse_args(argv)

    from app.isolation import elevated_protocol as proto

    if args.state_dir:
        proto.set_state_dir(args.state_dir)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[helper] %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    return serve(args.parent_pid)


if __name__ == "__main__":
    raise SystemExit(main())
