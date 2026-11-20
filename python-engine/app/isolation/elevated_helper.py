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

        self._reply(
            HTTPStatus.OK,
            {
                "ok": True,
                "protocol": proto.PROTOCOL_VERSION,
                # 让引擎侧能把"helper 在跑但没权限"与"helper 在跑且有权限"分开，
                # 这是排查 err=5 的第一手信息。
                "elevated": _is_elevated(),
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
    """
    from app.isolation import win_launch

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

    result = win_launch.launch_restricted(
        argv,
        cwd=cwd,
        env=env,
        timeout=timeout,
        max_output=max_output,
    )
    return {
        "started": result.started,
        "spawn_error": result.spawn_error,
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
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
