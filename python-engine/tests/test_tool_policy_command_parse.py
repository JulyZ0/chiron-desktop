"""Shell/Python 命令的**真解析**与风险分类（H3 / PRD 9.1）。

缺陷背景：

* 安全前缀把复合命令降级为 `write`（`echo ok && rm -rf x` 只看首个词是 echo）；
* auto 模式不确认 `write`，于是"降级"= **免确认放行**；
* 分段不拆单个 `&`（`ls & rm -rf x` 被当成一条命令）。

修复后的口径（本文件逐条钉住）：

1. 命令类工具按**解析结果**分级：`&`、`;`、`&&`、`||`、管道、重定向、子 shell、环境赋值、
   解释器 `-c`（含 `-Bc`/紧贴写法）一律停在 `delete`（auto 下也要确认 + 二次校验）；
2. 只有"解析后恰好一条简单只读命令"**且**文本没有命中沙箱逃逸模式时才降为 `write`；
3. 无法证明安全（语法不可解析、`$()`/反引号/换行/绝对路径/`..`）⇒ 升级审批，不猜。
"""

from __future__ import annotations

import pytest

from app.agent.tool_policy import DELETE, WRITE, requires_confirmation, requires_second_check, tool_level
from app.tools.sandbox import parse_shell_program

#: 复合命令攻击探针：(命令, 说明)。每条都必须停在 delete。
COMPOUND_PROBES = [
    ("ls -la & rm -rf victim", "单个 & 是命令分隔符，不是参数"),
    ("ls -la; rm -rf victim", "分号串接"),
    ("ls -la && rm -rf victim", "&& 串接"),
    ("ls -la || rm -rf victim", "|| 串接"),
    ("ls -la | sh", "管道把输出交给解释器"),
    ("cat README.md | tee log.txt", "管道 + 写文件"),
    ("ls -la > out.txt", "重定向写文件"),
    ("ls -la >> out.txt", "追加重定向"),
    ("ls -la 2>&1", "文件描述符重定向也不可静态证明目标"),
    ("(rm -rf victim)", "子 shell"),
    ("{ ls -la; rm -rf victim; }", "命令组"),
    ("FOO=bar ls -la", "环境赋值影响子进程"),
    ("PYTHONPATH=.:x python app.py", "解释器环境被注入"),
    ("echo ok\nrm -rf victim", "换行即命令分隔符"),
    # 解释器 -c：紧贴与组合写法都必须识别
    ("python -c \"import os; os.system('id')\"", "解释器内联代码"),
    ("python -c'import os'", "紧贴写法"),
    ("python -Bc \"import os; os.system('id')\"", "组合短开关"),
    ("python3 -c \"print(1)\"", "python3 同族"),
    ("python -m json.tool", "模块入口源码不可静态审查"),
    # 文本逃逸：结构简单但目标在沙箱外 / 不可静态审查
    ("echo `id`", "反引号命令替换"),
    ("echo $(id)", "$() 命令替换"),
    ("cat /etc/shadow", "绝对路径读取宿主"),
    ("ls ../../../", "父目录穿越"),
    ("cat \\\\host\\share\\secret", "UNC 路径"),
    ("echo $HOME", "环境变量展开"),
]


@pytest.mark.parametrize(("command", "why"), COMPOUND_PROBES)
def test_compound_or_unprovable_commands_escalate(command: str, why: str) -> None:
    level = tool_level("shell_exec", {"command": command})
    assert level == DELETE, f"{command!r} 必须升级到 delete（{why}）"
    assert requires_confirmation(level, "auto") is True, "auto 模式下也必须确认"
    assert requires_second_check(level) is True, "不可逆/不可证明的命令要二次校验"


@pytest.mark.parametrize("tool", ["shell_exec", "persistent_shell", "execute_command"])
@pytest.mark.parametrize("command", ["ls -la & rm -rf victim", "echo ok; cat /etc/shadow"])
def test_every_command_tool_shares_one_classifier(tool: str, command: str) -> None:
    """命令类工具不能各自一套口径：`persistent_shell` 走的是同一条解析链路。"""
    assert tool_level(tool, {"command": command}) == DELETE


def test_persistent_shell_newline_is_escalated_on_posix(monkeypatch) -> None:
    """POSIX 上 `persistent_shell` 是 sh，换行是命令分隔符 —— 不能按 cmd 词法当参数。"""
    monkeypatch.setattr("app.agent.tool_policy.sys.platform", "linux")
    assert tool_level("persistent_shell", {"command": "echo ok\nrm -rf victim"}) == DELETE
    assert tool_level("persistent_shell", {"command": "echo ok"}) == WRITE


@pytest.mark.parametrize(
    "command",
    ["ls -la", "cat README.md", "echo hello", "pwd", "whoami", "which python", "git status",
     "python --version", "node --version"],
)
def test_provably_simple_readonly_commands_stay_write(command: str) -> None:
    """只读、单条、无逃逸模式的命令仍降为 write（ask 模式下照样要确认）。"""
    level = tool_level("shell_exec", {"command": command})
    assert level == WRITE, f"{command!r} 属可证明安全的简单命令"
    assert requires_confirmation(level, "ask") is True


@pytest.mark.parametrize(
    "command",
    ["git push --force", "git log --oneline", "rm -rf /", "python app.py", "npm install"],
)
def test_non_allowlisted_or_stateful_commands_stay_delete(command: str) -> None:
    assert tool_level("shell_exec", {"command": command}) == DELETE


def test_single_ampersand_is_parsed_as_async_separator() -> None:
    """回归：`_split_segments` 曾不拆单个 `&`，于是 `ls & rm -rf x` 看起来只有一段。"""
    program = parse_shell_program("ls & rm -rf victim", dialect="posix")
    assert program.asynchronous is True
    assert "&" in program.operators
    assert len(program.commands) == 2
    assert program.commands[1].argv[0] == "rm"


def test_unparseable_syntax_fails_closed() -> None:
    for command in ["ls 'unterminated", "cat <<EOF", "if true; then rm -rf x; fi"]:
        assert tool_level("shell_exec", {"command": command}) == DELETE, command


def test_missing_command_text_stays_delete() -> None:
    assert tool_level("shell_exec", {}) == DELETE
    assert tool_level("shell_exec", {"command": "   "}) == DELETE


def test_python_tools_never_downgraded() -> None:
    """解释器类工具没有"安全简单命令"这回事：源码一律 delete。"""
    for tool, key in (("execute_python", "code"), ("run_code", "code")):
        assert tool_level(tool, {key: "print('hello')"}) == DELETE
