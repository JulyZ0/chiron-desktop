"""服务身份与端口归属的判定（PRD 9.7 / H43）。

放进独立模块而不是塞回 `run.py`：这些函数是**纯解析 + 一次命令调用**，
可以被单测直接喂假输出；而 `run.py` 是启动器，导入即探测环境，不适合当被测算子。

三条问题（此前全部存在，且都会让"验收/重启"假成功）：

1. **PID 子串匹配**：`str(pid) in tasklist_output` —— PID `123` 会命中 `1234`，
   于是"上个进程早死了、PID 被别人的进程复用"也能报"运行中"。
2. **端口子串匹配**：`f":{port}" in line` —— 端口 `8080` 会命中 `18080` / `80800`，
   于是"8080 上有人监听"可能是完全无关的服务。
3. **端口归属**：谁在监听和"是不是我们刚起的那个进程"是两件事。只查
   `is_port_open` 等于"端口有人应就算成功"，于是重启失败（旧进程还在）也能报成功。

判定原则：**能证明才算通过**。取不到归属信息时返回 `None`（未知），由调用方
降级为"无法证明"，而不是乐观当成通过。
"""

from __future__ import annotations

import re
import subprocess

__all__ = [
    "parse_tasklist_pid",
    "parse_netstat_listener_pid",
    "parse_lsof_listener_pid",
    "is_windows",
]


def is_windows() -> bool:
    import platform

    return platform.system().lower() == "windows"


# ── PID 精确匹配 ─────────────────────────────────────────────────────────────

#: `tasklist /FO CSV /NH` 的一行：`"python.exe","1234","Console","1","123,456 K"`
_TASKLIST_CSV = re.compile(r'^"(?P<image>[^"]*)","(?P<pid>\d+)"')


def parse_tasklist_pid(output: str, pid: int) -> str | None:
    """在 `tasklist /FO CSV /NH` 输出里找 **PID 恰好等于** `pid` 的进程，返回映像名。

    为什么要改用 CSV：默认表格输出是**按列对齐的文本**，PID 与其它列（会话号、
    内存）在窄终端里会被挤成一行，用子串匹配必然误判。CSV 的第二个字段就是 PID，
    精确可比。
    """
    for line in output.splitlines():
        m = _TASKLIST_CSV.match(line.strip())
        if not m:
            continue
        if int(m.group("pid")) == pid:
            return m.group("image")
    return None


# ── 监听端口 → 归属 PID ──────────────────────────────────────────────────────

#: `netstat -ano` 的 TCP 行：`  TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    1234`
_NETSTAT_LINE = re.compile(
    r"^\s*TCP\s+(?P<local>\S+)\s+(?P<remote>\S+)\s+(?P<state>[A-Z_]+)\s+(?P<pid>\d+)\s*$"
)


def _host_port(local: str) -> int | None:
    """从 `0.0.0.0:8080` / `[::]:8080` / `*:8080` 里取端口。

    必须**按最后一个冒号切**再比对整段：`f":{port}" in local` 会把 `:18080`
    也算成 `:8080` 命中（子串匹配的经典错）。
    """
    if ":" not in local:
        return None
    tail = local.rsplit(":", 1)[1]
    if not tail.isdigit():
        return None
    return int(tail)


def parse_netstat_listener_pid(output: str, port: int) -> int | None:
    """从 `netstat -ano` 输出里取**监听** `port` 的 PID（Windows）。"""
    for line in output.splitlines():
        m = _NETSTAT_LINE.match(line)
        if not m:
            continue
        if m.group("state") != "LISTENING":
            continue
        if _host_port(m.group("local")) != port:
            continue
        return int(m.group("pid"))
    return None


def parse_lsof_listener_pid(output: str, port: int) -> int | None:
    """从 `lsof -nP -iTCP:<port> -sTCP:LISTEN -t` 输出里取 PID（POSIX）。

    lsof 已按端口过滤，但输出可能有多个 PID（SO_REUSEPORT / 父子进程）：
    这里取**最小**的一个并返回全部给调用方判断的话信息量更大 —— 因此约定
    多 PID 时返回 None（"归属不唯一"），让上层按"无法证明"处理。
    """
    pids = [int(x) for x in output.split() if x.strip().isdigit()]
    unique = sorted(set(pids))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        return None  # 归属不唯一：不能证明"是我们那个"
    return None


def listener_pid(port: int, *, runner=subprocess.run) -> int | None:
    """查询当前监听 `port` 的 PID；取不到返回 None（未知 ≠ 通过）。"""
    try:
        if is_windows():
            res = runner(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True)
            return parse_netstat_listener_pid(res.stdout or "", port)
        res = runner(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
        )
        return parse_lsof_listener_pid(res.stdout or "", port)
    except (OSError, subprocess.SubprocessError):
        return None
