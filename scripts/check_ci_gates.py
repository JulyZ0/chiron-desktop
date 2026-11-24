#!/usr/bin/env python3
"""CI 门禁存在性检查（PRD 9.3 / H18）。

为什么需要它：H18 的核心抱怨不是"某条检查红了"，而是"这条检查**可以不存在**"——
lint 步骤缺失、跨 OS job 缺失、sandbox-service / PageIndex 没有 job，CI 依然一片绿。
那种失败模式在任何一次运行里都看不出来，只能在**配置层**核对。

本脚本读 `.github/workflows/*.yml`，按"必须存在的门禁"逐条断言，并把结果打印成
可粘贴的证据。它是**纯静态**的：不联网、不跑构建，因此可以放进任何门禁流水线的前置。

用法：
    python scripts/check_ci_gates.py            # 通过 exit 0，缺项 exit 1
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
GO_VERSION = ROOT / ".go-version"

#: 必须出现在 CI 里的门禁。每项 = (说明, 正则)。
#: 正则都写得**具体**（带命令或 job id），避免"注释里提了一嘴"就算通过。
GATES: list[tuple[str, str]] = [
    ("Go 版本从 .go-version 取（不跟 go.mod 的次要版本漂移）", r"go-version-file:\s*\.go-version"),
    ("gofmt 门禁（干净才通过）", r"gofmt -l"),
    ("golangci-lint（版本显式固定）", r"GOLANGCI_LINT_VERSION:\s*v?\d+\.\d+\.\d+"),
    ("govulncheck（版本显式固定）", r"GOVULNCHECK_VERSION:\s*v?\d+\.\d+\.\d+"),
    ("go test -race（并发重的包）", r"go test[^\n]*-race"),
    ("单元测试跳过 Live/Diag 的口径", r"-skip 'Live\|Diag'"),
    ("跨 OS job（Windows）", r"runs-on:\s*windows-latest"),
    ("PageIndex 独立 job", r"^\s{2}pageindex:\s*$"),
    ("sandbox-service 独立 job", r"^\s{2}sandbox-service:\s*$"),
    ("沙箱隔离用例不许静默 skip", r"沙箱隔离用例出现 skip"),
]


def main() -> int:
    problems: list[str] = []

    if not CI.is_file():
        print(f"[FAIL] 缺少 {CI.relative_to(ROOT)}")
        return 1
    text = CI.read_text(encoding="utf-8")

    if not GO_VERSION.is_file():
        problems.append(".go-version 不存在（Go 门禁没有固定版本可依）")
    else:
        version = GO_VERSION.read_text(encoding="utf-8").strip()
        if not re.fullmatch(r"1\.\d+\.\d+", version):
            problems.append(f".go-version 内容不是补丁级版本：{version!r}")

    for label, pattern in GATES:
        if re.search(pattern, text, re.MULTILINE):
            print(f"[ OK ] {label}")
        else:
            problems.append(f"缺少门禁：{label}（正则 {pattern}）")

    if problems:
        print()
        for p in problems:
            print(f"[FAIL] {p}")
        return 1
    print(f"\n全部 {len(GATES)} 条 CI 门禁存在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
