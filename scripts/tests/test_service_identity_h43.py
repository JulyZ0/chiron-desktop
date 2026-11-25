"""H43：服务身份与端口归属的解析必须**精确**（PRD 9.7）。

修复前的三种"假成功"（都能在这组用例里复现）：

1. `is_process_running` 用 `str(pid) in tasklist_output`：PID `123` 命中 `1234`，
   于是"进程早死了、PID 被复用"也报运行中；
2. `_find_service_pid` 用 `f":{port}" in line`：端口 `8080` 命中 `18080`/`80800`；
3. `restart()` 没有返回值、`main` 不 `sys.exit`：重启失败也退出码 0。

前两条在本文件里用**真实命令输出的假样本**钉住；第三条由 `run.py` 的
`restart(...) -> bool` + `sys.exit(0 if … else 1)` 保证，这里用 AST 静态检查守住
"别再写回无返回值 / 别再把 restart 的退出码吞掉"。
"""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.service_identity import (
    parse_lsof_listener_pid,
    parse_netstat_listener_pid,
    parse_tasklist_pid,
)

ROOT = Path(__file__).resolve().parents[2]

# ── 1. PID 必须精确匹配 ──────────────────────────────────────────────────────
_TASKLIST_CSV = (
    '"python.exe","1234","Console","1","12,345 K"\n'
    '"chiron.exe","123","Console","1","98,765 K"\n'
)


def test_tasklist_pid_match_is_exact() -> None:
    assert parse_tasklist_pid(_TASKLIST_CSV, 123) == "chiron.exe"
    assert parse_tasklist_pid(_TASKLIST_CSV, 1234) == "python.exe"


def test_tasklist_pid_prefix_does_not_match() -> None:
    """旧实现的致命点：123 会命中 1234。"""
    output = '"python.exe","1234","Console","1","12,345 K"\n'
    assert parse_tasklist_pid(output, 123) is None


def test_tasklist_empty_or_header_only() -> None:
    assert parse_tasklist_pid("", 1) is None
    assert parse_tasklist_pid("INFO: No tasks are running which match the specified criteria.\n", 1) is None


# ── 2. 端口必须精确匹配且只看 LISTENING ─────────────────────────────────────
_NETSTAT = """\r
Active Connections\r
\r
  Proto  Local Address          Foreign Address        State           PID\r
  TCP    0.0.0.0:18080          0.0.0.0:0              LISTENING       4242\r
  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       1234\r
  TCP    0.0.0.0:8080           127.0.0.1:51515        ESTABLISHED     9999\r
  TCP    [::]:8080              [::]:0                 LISTENING       1234\r
"""


def test_netstat_listener_pid_exact_port() -> None:
    assert parse_netstat_listener_pid(_NETSTAT, 8080) == 1234
    assert parse_netstat_listener_pid(_NETSTAT, 18080) == 4242


def test_netstat_ignores_substring_port() -> None:
    """旧实现 `f":8080" in line` 会把 18080 也算命中。"""
    only_18080 = "  TCP    0.0.0.0:18080          0.0.0.0:0              LISTENING       4242\n"
    assert parse_netstat_listener_pid(only_18080, 8080) is None


def test_netstat_port_prefix_does_not_match() -> None:
    """更经典的子串陷阱：查 80 时 `:8080` / `:8000` 都含 `:80`。

    旧实现会因此把"8080 上的服务"报成"80 在监听"，进而让 `restart` 的端口归属
    校验张冠李戴 —— 这正是 H43 要消灭的假成功形态之一。
    """
    only_8080 = "  TCP    0.0.0.0:8080           0.0.0.0:0              LISTENING       5151\n"
    assert parse_netstat_listener_pid(only_8080, 80) is None
    assert parse_netstat_listener_pid(only_8080, 800) is None
    assert parse_netstat_listener_pid(only_8080, 8080) == 5151


def test_netstat_ignores_non_listening_states() -> None:
    """只有 ESTABLISHED 时不算"在监听"（客户端连出去的连接也有本端端口）。"""
    established = (
        "  TCP    0.0.0.0:8080           127.0.0.1:51515        ESTABLISHED     9999\n"
    )
    assert parse_netstat_listener_pid(established, 8080) is None


def test_netstat_handles_ipv6_binding() -> None:
    ipv6 = "  TCP    [::]:9200              [::]:0                 LISTENING       777\n"
    assert parse_netstat_listener_pid(ipv6, 9200) == 777


# ── 3. POSIX：多 PID 时归属不唯一，返回 None 而不是"挑第一个" ────────────────
def test_lsof_single_pid() -> None:
    assert parse_lsof_listener_pid("1234\n", 8080) == 1234


def test_lsof_multiple_pids_is_unknown() -> None:
    """SO_REUSEPORT / 父子进程同时监听：不能证明"是我们那个"。"""
    assert parse_lsof_listener_pid("1234\n5678\n", 8080) is None


def test_lsof_empty_is_unknown() -> None:
    assert parse_lsof_listener_pid("", 8080) is None


# ── 4. run.py 的契约：restart 必须返回布尔且退出码反映它 ─────────────────────
def _load_run_ast() -> ast.Module:
    return ast.parse((ROOT / "run.py").read_text(encoding="utf-8"))


def _service_manager_class(tree: ast.Module) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ServiceManager":
            return node
    raise AssertionError("run.py 里找不到 ServiceManager")


def test_restart_returns_bool() -> None:
    cls = _service_manager_class(_load_run_ast())
    for item in cls.body:
        if isinstance(item, ast.FunctionDef) and item.name == "restart":
            assert item.returns is not None, "restart 必须标注返回类型（H43）"
            assert "bool" in ast.unparse(item.returns), f"restart 应返回 bool，实际 {ast.unparse(item.returns)}"
            returns = [n for n in ast.walk(item) if isinstance(n, ast.Return)]
            assert returns, "restart 必须真的 return 结论，而不是隐式 None"
            return
    raise AssertionError("ServiceManager 里没有 restart")


def test_restart_exit_code_is_reflected() -> None:
    """`main` 里 restart 分支必须把结果交给 sys.exit —— 不能吞掉失败。"""
    tree = _load_run_ast()
    main_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    source = ast.unparse(main_fn)
    # ast.unparse 会把字符串引号统一成单引号，这里按语义匹配（不绑引号风格）。
    marker = "args.command == 'restart'"
    assert marker in source or 'args.command == "restart"' in source
    restart_branch = source.split("restart")[1] if marker not in source else source.split(marker)[1]
    assert "sys.exit(" in restart_branch.split("elif")[0], (
        "restart 分支必须 sys.exit(0/1)，否则重启失败仍退出 0（假重启）"
    )
