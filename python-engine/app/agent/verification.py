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
import re
from dataclasses import dataclass, field
from typing import Any

#: 会被视为"改了工作区"的工具。删除也算：删错了同样需要验证。
_WRITE_TOOLS = frozenset({"write_file", "edit_file", "delete_file"})

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

    def to_event(self) -> dict[str, Any]:
        """`done` 事件里的 `verification` 字段（体积很小，可以每轮都带）。"""
        return {
            "status": self.status,
            "command": self.command,
            "kind": self.kind,
            "exit_code": self.exit_code,
            "wrote_files": self.wrote_files,
        }


@dataclass
class VerificationTracker:
    """一次 run 内的账本。只在 run() 内部使用，不需要线程安全。"""

    records: list[VerificationRecord] = field(default_factory=list)
    wrote_files: bool = False

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
            )
        if self.wrote_files:
            return VerificationSummary(status="unverified", wrote_files=True)
        return VerificationSummary(status="not-needed")

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
