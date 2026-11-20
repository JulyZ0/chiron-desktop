#!/usr/bin/env bash
# check-release-ready.sh —— 发布前就绪自检（可独立跑，也被 `ci-smoke.sh --release` 调用）
#
# 它**不重跑**任何门禁。重跑等于把发布前的等待时间翻倍，而且跑的东西与之后
# 要签字的产物未必是同一份。它只做两类核对：
#
#   1. 仓库状态本身：迁移链只有 1 个 head、工作树干净、HEAD 可解析；
#   2. 门禁**证据**：`.artifacts/ci-smoke/{go,python,frontend-full}.result`
#      存在、可读、结论为 pass，且记录的是**当前 HEAD**。
#
# 第 2 类的最后一条最容易被忽略：跑完门禁又改了一行再提交，旧的"全绿"就
# 不再覆盖新的提交，此时放行等于拿上一次的结论给这一次签字。
#
# 证据由 `bash scripts/ci-smoke.sh`（**全量，不要加 --fast**）写入：
# `--fast` 会跳过前端全量，因此不产出 frontend-full.result —— 发布门禁不接受跳过。
#
# 用法：
#   bash scripts/check-release-ready.sh
#   CHIRON_SMOKE_ARTIFACTS=<dir> bash scripts/check-release-ready.sh   # 换证据目录
#
# 退出码：0 = 全部就绪；1 = 存在未就绪项（逐条打印原因，不中断）。

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

ARTIFACTS="${CHIRON_SMOKE_ARTIFACTS:-$ROOT/.artifacts/ci-smoke}"
PASS=0
FAIL=0

ok() { PASS=$((PASS + 1)); printf '  [ ok ] %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf '  [FAIL] %s\n' "$*"; }
info() { printf '  [info] %s\n' "$*"; }
section() { printf '\n── %s\n' "$*"; }

result_field() {
  # result_field <文件> <键>：读 ci-smoke 落的 key=value 行；缺失返回空串
  awk -F= -v key="$2" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$1" 2>/dev/null
}

# 三门禁的 slug → 证据文件。前端取 `frontend-full`（不是 frontend-static）：
# 打包静态冒烟只读源码，`--fast` 也能绿，拿它当发布就绪等于放行一份没跑过单测的前端。
gate_file() {
  case "$1" in
    go) printf '%s/go.result\n' "$ARTIFACTS" ;;
    python) printf '%s/python.result\n' "$ARTIFACTS" ;;
    frontend) printf '%s/frontend-full.result\n' "$ARTIFACTS" ;;
  esac
}

gate_hint() {
  case "$1" in
    frontend) printf 'bash scripts/ci-smoke.sh（全量；--fast 不产出 frontend-full）' ;;
    *) printf 'bash scripts/ci-smoke.sh' ;;
  esac
}

for arg in "$@"; do
  case "$arg" in
    -h | --help)
      awk 'NR > 1 && /^set -/{exit} NR > 1' "$0"
      exit 0
      ;;
    *)
      echo "未知参数：$arg（本脚本没有参数；证据目录用 CHIRON_SMOKE_ARTIFACTS）" >&2
      exit 2
      ;;
  esac
done

HEAD_SHA="$(git rev-parse HEAD 2>/dev/null || true)"
printf 'check-release-ready：root=%s\n' "$ROOT"
printf '证据目录：%s\n' "${ARTIFACTS#"$ROOT"/}"

section '仓库状态'
if [ -n "$HEAD_SHA" ]; then
  ok "HEAD ${HEAD_SHA:0:12}"
else
  bad '拿不到 git HEAD（当前目录不在 git 工作树里）'
fi

# 未跟踪文件也算脏：发布出包走的是 `git archive` 之外的构建，但"本地绿、提交里没有"
# 是同一类失真，而且它会让"门禁跑过"与"发布的内容"对不上。
DIRTY="$(git status --porcelain 2>/dev/null || true)"
if [ -n "$DIRTY" ]; then
  bad "工作树不干净（$(printf '%s\n' "$DIRTY" | wc -l | tr -d ' ') 项改动）—— 发布只从已提交的内容出包"
  printf '%s\n' "$DIRTY" | head -10 | sed 's/^/         /'
else
  ok '工作树干净（git status --porcelain 为空）'
fi

section '迁移链'
if [ ! -d migrations/versions ]; then
  bad '找不到 migrations/versions'
elif ! command -v python >/dev/null 2>&1; then
  bad '找不到 python —— 无法校验迁移链（单 head 是网关启动的硬前提）'
else
  # 与 ci-smoke.sh 的 schema 环节共用同一个脚本，避免两份正则各自漂移。
  if schema_out="$(python scripts/check_migration_head.py 2>&1)"; then
    ok "迁移链单 head：${schema_out#*heads=}"
  else
    bad "$(printf '%s' "$schema_out" | tr '\n' ' ')"
  fi
fi

section '门禁证据'
for gate in go python frontend; do
  file="$(gate_file "$gate")"
  rel="${file#"$ROOT"/}"
  if [ ! -f "$file" ]; then
    bad "$gate：缺少 $rel —— 先跑 $(gate_hint "$gate")"
    continue
  fi
  status="$(result_field "$file" status)"
  commit="$(result_field "$file" commit)"
  finished="$(result_field "$file" finished_at)"
  case "$status" in
    pass) ;;
    skip)
      bad "$gate：结论是 skip（$rel）—— 发布门禁不接受跳过，补齐环境后重跑"
      continue
      ;;
    '')
      bad "$gate：$rel 读不出 status（文件损坏？）"
      continue
      ;;
    *)
      bad "$gate：结论是 $status（$rel）—— 先修红再谈发布"
      continue
      ;;
  esac
  if [ -n "$HEAD_SHA" ] && [ "$commit" != "$HEAD_SHA" ]; then
    bad "$gate：证据记的是 ${commit:0:12}，当前 HEAD 是 ${HEAD_SHA:0:12} —— 跑完门禁后又改过代码"
    continue
  fi
  ok "$gate：pass（$finished）"
done

section '其它证据'
# 只做展示：这几项在 --fast / 未开 --integration 时本来就可能是 skip 或缺失。
for slug in schema frontend-static integration; do
  file="$ARTIFACTS/$slug.result"
  if [ -f "$file" ]; then
    info "$slug：$(result_field "$file" status)（$(result_field "$file" finished_at)）"
  else
    info "$slug：无记录"
  fi
done

printf '\n就绪：%d 项通过 / %d 项未通过\n' "$PASS" "$FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo '结论：发布就绪（checklist 全绿）。'
  exit 0
fi
echo '结论：未就绪 —— 处理上面 [FAIL] 项后重跑（门禁用 bash scripts/ci-smoke.sh 重跑）。'
exit 1
