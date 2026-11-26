#!/usr/bin/env bash
# 依赖漏洞与 SBOM 快检（L29）。
#
# 设计取舍：
# - 工具**不内置**：CI/主机上装了什么就跑什么，缺工具记 SKIP 而不是假装通过。
# - 有漏洞时退出码 1；全部 SKIP 退出 77（与 integration 同口径：环境不具备 ≠ 成功）。
# - 只读，不改依赖、不自动升级 —— 升级要人看 breaking change。
#
# 用法：
#   ./scripts/dep_audit.sh                 # 跑全部可用检查
#   ./scripts/dep_audit.sh --sbom DIR      # 额外把 SBOM 写到 DIR
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SBOM_DIR=""
if [[ "${1:-}" == "--sbom" ]]; then
  SBOM_DIR="${2:-$ROOT/.artifacts/sbom}"
  mkdir -p "$SBOM_DIR"
fi

ran=0
failed=0
skipped=0

note() { printf '\n==> %s\n' "$*"; }
skip() { echo "SKIP: $*"; skipped=$((skipped + 1)); }

note "Go (govulncheck)"
if command -v govulncheck >/dev/null 2>&1; then
  ran=$((ran + 1))
  ( cd "$ROOT" && govulncheck ./... ) || failed=$((failed + 1))
else
  skip "govulncheck 未安装（go install golang.org/x/vuln/cmd/govulncheck@latest）"
fi

note "Python (pip-audit)"
if command -v pip-audit >/dev/null 2>&1; then
  ran=$((ran + 1))
  ( cd "$ROOT/python-engine" && pip-audit -r requirements.txt ) || failed=$((failed + 1))
else
  skip "pip-audit 未安装（pip install pip-audit）"
fi

note "Node (pnpm audit --prod)"
if command -v pnpm >/dev/null 2>&1; then
  ran=$((ran + 1))
  ( cd "$ROOT/frontend-vue" && pnpm audit --prod ) || failed=$((failed + 1))
else
  skip "pnpm 未安装"
fi

if [[ -n "$SBOM_DIR" ]]; then
  note "SBOM"
  if command -v syft >/dev/null 2>&1; then
    ran=$((ran + 1))
    syft "dir:$ROOT" -o "spdx-json=$SBOM_DIR/chiron.spdx.json" || failed=$((failed + 1))
  else
    skip "syft 未安装（https://github.com/anchore/syft）"
  fi
fi

printf '\n汇总：ran=%d failed=%d skipped=%d\n' "$ran" "$failed" "$skipped"
if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
if [[ "$ran" -eq 0 ]]; then
  exit 77
fi
exit 0
