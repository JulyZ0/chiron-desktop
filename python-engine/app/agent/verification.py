"""本轮「验证结论」的纯逻辑（N6）。

## 要解决的问题

Agent 说"改完了"不等于改对了。产品需要一个**诚实的结论**：这一轮到底有没有跑过
测试/类型检查/构建，跑了是通过还是失败；如果一个文件都没改，则明确"无需验证"。

## 为什么用启发式而不是新工具

让模型调用一个 `verify` 工具听起来更干净，但会引入新的权限面（要自动跑命令）与
新的提示词约定；而 `shell_exec` 的结果里本来就有 `exit_code`。先做**记账**：
从已经发生的工具调用里识别"看起来是验证"的命令，按最差的一条给结论。
它不新增任何执行能力，也不会因为误判而让任务失败 —— 判错只影响提示文案。

## 口径

- 只统计**真正执行过**的 `shell_exec`（被审批/护栏拦下的不算）；
- 命令分类用**关键词**（test / typecheck / lint / build），宁可漏判成"未验证"，
  也不要把 `git status` 当成验证；
- 多条记录取**最差**结论：任何一条失败即整体失败，`command` 记第一条失败的；
- 修改类工具（write/edit/delete）只用来判断"有没有东西需要验证"。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

#: 会被视为"改了工作区"的工具。删除也算：删错了同样需要验证。
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})

#: 项目级验证配置（N6.1）。放在仓库里而不是服务端数据库：
#: 它描述的是"这个代码库怎么验证自己"，与代码一起版本化、跟着分支走。
VERIFY_CONFIG_PATH = os.path.join(".chiron", "verify.json")
#: 配置文件上限：它只是几十条命令，读 64KB 已经非常宽松；超限按"没有配置"处理。
_MAX_CONFIG_BYTES = 64 * 1024
#: 命令条数上限：防止一个坏文件把每轮记账变成 O(n) 扫描。
_MAX_CONFIG_COMMANDS = 50

#: 验证命令类别（顺序即优先级：同一条命令只归一类）。
#: 只放**明确用于质量门禁**的模式；`go run`、`npm start` 这类不算。
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "test",
        re.compile(
            r"\b(pytest|py\.test|unittest|go\s+test|npm\s+(run\s+)?test|pnpm\s+(run\s+)?test"
            r"|yarn\s+(run\s+)?test|bun\s+test|vitest|jest|mocha|rspec|cargo\s+test"
            r"|mvn\s+test|gradle\s+test|make\s+(test|check))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "typecheck",
        re.compile(
            r"\b(tsc|vue-tsc|mypy|pyright|go\s+vet|dart\s+analyze)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "lint",
        re.compile(
            r"\b(eslint|ruff\s+check|golangci-lint|flake8|pylint|prettier\s+--check"
            r"|gofmt\s+-l)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "build",
        re.compile(
            r"\b(go\s+build|npm\s+run\s+build|pnpm\s+(run\s+)?build|yarn\s+build"
            r"|cargo\s+build|make\s+build|mvn\s+(compile|package)|gradle\s+build)\b",
            re.IGNORECASE,
        ),
    ),
)


def classify_verification_command(command: str) -> str:
    """返回 test / typecheck / lint / build；不是验证命令则返回空串。"""
    text = str(command or "").strip()
    if not text:
        return ""
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    return ""


def _command_of(arguments: Any) -> str:
    """从工具参数里取 command。参数可能是 dict，也可能是 JSON 字符串。"""
    if isinstance(arguments, dict):
        return str(arguments.get("command") or "")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except (TypeError, ValueError):
            return ""
        if isinstance(parsed, dict):
            return str(parsed.get("command") or "")
    return ""


def _normalize_command(command: str) -> str:
    """归一化用于比较的命令。

    - 折叠空白（`go  test` 与 `go test` 视为同一条）；
    - 去掉前置 `cd <path> &&`：模型的常见写法，与配置里的裸命令是同一个动作；
    - 不做 shell 解析 —— 这里只用于**识别**，不执行，过度解析反而会误判。
    """
    text = " ".join(str(command or "").split()).strip()
    previous = None
    while text and text != previous:
        previous = text
        match = re.match(r"^cd\s+[^&;]+\s*&&\s*", text, re.IGNORECASE)
        if match:
            text = text[match.end():].strip()
    return text


def command_matches_config(command: str, configured: tuple[str, ...]) -> str:
    """命令是否命中项目配置；命中返回配置里的那条（便于事件里展示），否则空串。

    用"前缀 + 分隔符"而不是子串：`pnpm test` 应命中 `pnpm test -- --run`，
    但 `pnpm test:unit` 不该被当成 `pnpm test`（冒号不是参数分隔符）。
    """
    text = _normalize_command(command)
    if not text:
        return ""
    for item in configured:
        want = _normalize_command(item)
        if not want:
            continue
        if text == want or text.startswith(want + " "):
            return item
    return ""


def load_verify_config(root: str) -> tuple[str, ...]:
    """读取 `<项目根>/.chiron/verify.json` 里的验证命令。

    格式：`{"commands": ["pnpm test", "pnpm -r typecheck"]}`。
    任何异常（文件缺失、JSON 坏、结构不对、超限）都返回空元组 —— 配置是**增强**，
    读不到就回落到启发式识别，绝不能让一个坏配置文件阻断对话。
    """
    project_root = str(root or "").strip()
    if not project_root:
        return ()
    try:
        base = os.path.realpath(project_root)
        path = os.path.realpath(os.path.join(base, VERIFY_CONFIG_PATH))
        # 符号链接不能跳出项目根：`.chiron/verify.json -> /etc/xxx` 不该被读。
        if path != base and not path.startswith(base + os.sep):
            return ()
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(_MAX_CONFIG_BYTES + 1)
    except OSError:
        return ()
    if len(raw) > _MAX_CONFIG_BYTES:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    commands = data.get("commands") if isinstance(data, dict) else None
    if not isinstance(commands, list):
        return ()
    out: list[str] = []
    for item in commands:
        if not isinstance(item, str):
            continue
        command = " ".join(item.split()).strip()
        if command and command not in out:
            out.append(command)
        if len(out) >= _MAX_CONFIG_COMMANDS:
            break
    return tuple(out)


@dataclass(frozen=True)
class VerificationRecord:
    command: str
    kind: str
    exit_code: int
    passed: bool


@dataclass(frozen=True)
class VerificationSummary:
    """一轮结束时的结论。`status` 是前端直接展示的三档。"""

    status: str  # passed | failed | unverified | not-needed
    command: str = ""
    kind: str = ""
    exit_code: int | None = None
    wrote_files: bool = False
    #: 判定来源：config（项目配置声明过验证命令）| heuristic（按关键字识别）。
    #: 只用于诊断与文案，不影响三档结论。
    source: str = "heuristic"

    def to_event(self) -> dict[str, Any]:
        """`done` 事件里的 `verification` 字段（体积很小，可以每轮都带）。"""
        return {
            "status": self.status,
            "command": self.command,
            "kind": self.kind,
            "exit_code": self.exit_code,
            "wrote_files": self.wrote_files,
            "source": self.source,
        }


@dataclass
class VerificationTracker:
    """一次 run 内的账本。只在 run() 内部使用，不需要线程安全。"""

    records: list[VerificationRecord] = field(default_factory=list)
    wrote_files: bool = False
    #: 项目声明的验证命令（N6.1）。非空时**只认**这些命令：
    #: 否则"配置了却没跑"会被别的 test 命令掩盖，配置就失去意义。
    configured: tuple[str, ...] = ()

    def note_execution(
        self,
        name: str,
        arguments: Any,
        result: Any,
        *,
        executed: bool,
    ) -> None:
        """记录一次工具执行结果；不满足条件的调用直接忽略。"""
        if not executed:
            return
        tool_name = str(name or "")
        if tool_name in _WRITE_TOOLS:
            self.wrote_files = True
            return
        if tool_name != "shell_exec":
            return
        if not isinstance(result, dict):
            return
        raw_code = result.get("exit_code")
        if isinstance(raw_code, bool) or not isinstance(raw_code, int):
            return
        command = _command_of(arguments)
        if self.configured:
            # N6.1：配置优先。命中配置的命令才记账；没命中就当"没验证过"，
            # 否则模型随手跑一个 `pytest` 就能绕过项目声明的质量门禁。
            matched = command_matches_config(command, self.configured)
            if not matched:
                return
            kind = classify_verification_command(command) or "custom"
        else:
            kind = classify_verification_command(command)
            if not kind:
                return
        self.records.append(
            VerificationRecord(
                command=command.strip(),
                kind=kind,
                exit_code=raw_code,
                passed=raw_code == 0,
            )
        )

    def summary(self) -> VerificationSummary:
        source = "config" if self.configured else "heuristic"
        if self.records:
            failed = next((r for r in self.records if not r.passed), None)
            last = self.records[-1]
            chosen = failed or last
            return VerificationSummary(
                status="failed" if failed else "passed",
                command=chosen.command,
                kind=chosen.kind,
                exit_code=chosen.exit_code,
                wrote_files=self.wrote_files,
                source=source,
            )
        if self.wrote_files:
            return VerificationSummary(status="unverified", wrote_files=True, source=source)
        return VerificationSummary(status="not-needed", source=source)

    def needs_revision(self) -> bool:
        """是否值得追加一轮"先修好再收尾"。

        只在**改了东西**时要求：只跑测试但没改文件（例如复现一个既有失败）不属于
        本轮该修的债，强行续轮会把只读排查变成无底洞。
        """
        summary = self.summary()
        if not summary.wrote_files:
            return False
        return summary.status in {"failed", "unverified"}


def revision_nudge(summary: VerificationSummary) -> str:
    """回灌给模型的提醒文案。只陈述事实与要求，不伪造失败原因。"""
    if summary.status == "failed":
        return (
            f"本轮验证命令失败了（`{summary.command}`，exit {summary.exit_code}）。"
            "请先修复失败原因并重新运行验证；如果无法修复，请明确说明阻塞点、"
            "已尝试的办法与剩余风险，不要直接结束。"
        )
    return (
        "本轮修改了文件，但没有运行任何测试/类型检查/构建，因此改动**未经验证**。"
        "请运行项目对应的验证命令；如果仓库里没有可用的验证命令，"
        "请说明原因以及这次改动未被覆盖的风险。"
    )
