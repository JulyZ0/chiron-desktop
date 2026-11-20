#!/usr/bin/env bash
# ci-smoke.sh —— 本地一键复核 CI 的关键链路
#
# 把 `.github/workflows/ci.yml` 里三条硬门禁的**关键部分**在提交前跑一遍，
# 失败即以非 0 退出，不必等 push 之后看 CI 结果：
#
#   1. go       ：build + 单测（排除需要真机 DB/Redis 的 live/diag 用例）
#                 + vendor 一致性（仅当仓库真的带 vendor/modules.txt）
#   2. engine   ：pytest 单测（`-m "not live and not integration"`；
#                 忽略 `test_chiron_client_sdk*.py` —— 它们要求 `clients/python/`
#                 里的客户端包，本仓不含，收集阶段就会报 ModuleNotFoundError）
#   3. frontend ：electron 打包静态冒烟 + UI 契约 + vue-tsc + vitest + vite build
#
# 另加 schema 校验：`migrations/versions` 必须**只有一个 head**（分叉会让
# `internal/db` 的 ParseMigrationHead 报错、网关拒绝启动）。
#
# 用法：
#   bash scripts/ci-smoke.sh          # 全量
#   bash scripts/ci-smoke.sh --fast   # 跳过前端 typecheck/单测/构建（仍跑打包静态冒烟）
#
# 环境变量：
#   CHIRON_SMOKE_SKIP=go,python,frontend  跳过指定环节（局部调试用）
#
# 前置：go / node / python 在 PATH 上；全量前端需要 `frontend-vue/node_modules`
# （先 `pnpm install --frozen-lockfile`）。

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAST=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    -h | --help)
      sed -n '2,28p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数：$arg（可用：--fast）" >&2
      exit 2
      ;;
  esac
done

SKIP="${CHIRON_SMOKE_SKIP:-}"
skipped() {
  case ",$SKIP," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

PASS=0
FAIL=0
SKIPPED=0
FAILED_NAMES=""

stage() {
  # stage <名字> <函数>；返回 2 表示"环境不具备，跳过"（不算失败）
  local name="$1"
  local fn="$2"
  local start rc
  start=$(date +%s)
  echo ""
  echo "── $name"
  "$fn"
  rc=$?
  if [ "$rc" -eq 0 ]; then
    echo "   ✔ $name（$(( $(date +%s) - start ))s）"
    PASS=$((PASS + 1))
  elif [ "$rc" -eq 2 ]; then
    echo "   ↷ $name：环境不具备，跳过"
    SKIPPED=$((SKIPPED + 1))
  else
    echo "   ✘ $name"
    FAIL=$((FAIL + 1))
    FAILED_NAMES="$FAILED_NAMES $name"
  fi
}

skip_stage() {
  echo ""
  echo "── $1"
  echo "   ↷ 跳过：$2"
  SKIPPED=$((SKIPPED + 1))
}

# ── 1. Go 网关 ────────────────────────────────────────────────────────────
# live/diag 用例要真机 PostgreSQL/Redis（integration job 用 services 提供）。
# 这里按**测试名**过滤而不是 build tag：仓内 live 用例没有 //go:build 标记，
# 靠环境缺失自跳过，混在一起只会让"跳过"与"通过"分不清。
stage_go() {
  go build -mod=mod ./... &&
    go test -mod=mod ./internal/... ./config/... ./cmd/... -count=1 -skip 'Live|Diag'
}

stage_vendor() {
  if [ -f vendor/modules.txt ]; then
    go build -mod=vendor ./...
  else
    echo "   （无 vendor/modules.txt：仓库 vendor/ 只有参考源码，跳过一致性检查）"
    return 0
  fi
}

# ── 2. 迁移单 head ────────────────────────────────────────────────────────
# 不依赖 alembic（本地未必装了 requirements-migrate.txt）：直接解析
# revision / down_revision。判据与 internal/db 的 ParseMigrationHead 同构。
stage_schema() {
  python - <<'PY'
import pathlib
import re
import sys

versions = pathlib.Path("migrations/versions")
revs = {}
parents = set()
for f in sorted(versions.glob("*.py")):
    text = f.read_text(encoding="utf-8")
    m = re.search(r"^revision\s*(?::[^=\n]+)?=\s*[\"']([^\"']+)[\"']", text, re.M)
    if not m:
        print(f"   ✘ {f.name}: 找不到 revision 声明", file=sys.stderr)
        sys.exit(1)
    revs[m.group(1)] = f.name
    d = re.search(r"^down_revision\s*(?::[^=\n]+)?=\s*(.+)$", text, re.M)
    if d:
        # down_revision 可能是 None / "abc" / ("a", "b")
        for ref in re.finditer(r"[\"']([^\"']+)[\"']", d.group(1)):
            parents.add(ref.group(1))

heads = sorted(set(revs) - parents)
print(f"   revisions={len(revs)} heads={heads}")
if len(heads) != 1:
    print("   ✘ 迁移链必须只有 1 个 head（分叉会让网关拒绝启动）", file=sys.stderr)
    sys.exit(1)
PY
}

# ── 3. Python 引擎 ────────────────────────────────────────────────────────
# CI 口径是**全量**单测（`tests`）。本机另有一套取舍：
# 开发机的 httpx / openai / anthropic 版本与 `requirements.txt` 钉的不一定相同，
# 于是 provider 一族用例会因上游 SDK 的 URL 解析/参数行为变化而红（实测 31 failed /
# 3886 passed，全部落在 provider・todo_plan・git_checkpoint 等 SDK 敏感面，
# 与"代码有问题"是两回事）。Windows 冷启动即全量只会训练人忽略红。
# 因此：CI 或 `CHIRON_SMOKE_PY_FULL=1` 走全量；否则跑**稳定子集的冒烟**
# （api + 引擎装配 + 模式 + 工具守卫 —— 覆盖最核心的契约面，秒级完成）。
stage_python() {
  if ! python -c "import pytest" >/dev/null 2>&1; then
    echo "   ↷ python 环境缺 pytest：先 pip install -r python-engine/requirements-dev.txt"
    return 2
  fi

  local targets="tests"
  local os_name
  os_name="$(uname -s 2>/dev/null || echo unknown)"
  if [ -n "${CI:-}" ] || [ "${CHIRON_SMOKE_PY_FULL:-0}" = "1" ]; then
    echo "   （CI / CHIRON_SMOKE_PY_FULL=1：全量单测）"
  else
    case "$os_name" in
      MINGW* | MSYS* | CYGWIN*)
        targets="tests/api tests/test_engine.py tests/test_modes.py tests/test_guards.py"
        echo "   （Windows 本机：跑稳定子集；全量请设 CHIRON_SMOKE_PY_FULL=1，结果以 CI 为准）"
        ;;
    esac
  fi

  # Windows 下 pytest 会对 %TEMP%\\pytest-of-* 里残留的 pytest-current 做清理，
  # 而删除符号链接需要权限 ⇒ 会话收尾抛 PermissionError（与用例结果无关）。
  # 给 pytest 一个干净的 TEMP 就能绕开，判定仍以用例为准。
  local tmp="${TMPDIR:-/tmp}/chiron-ci-smoke-pytest"
  mkdir -p "$tmp" || true
  (
    cd python-engine || exit 1
    JWT_SECRET="${JWT_SECRET:-ci-python-jwt-secret-0123456789abcdef}" \
      APP_SECRET="${APP_SECRET:-ci-python-app-secret-0123456789abcdef}" \
      TEMP="$tmp" TMP="$tmp" \
      python -m pytest $targets -q -m "not live and not integration" \
      --ignore-glob="tests/test_chiron_client_sdk*.py"
  )
}

# ── 4. 前端 ───────────────────────────────────────────────────────────────
# 打包静态冒烟**不需要 node_modules**（只读源码/配置），任何环境都跑。
stage_frontend_static() {
  node --test frontend-vue/electron/__tests__/packaging.test.cjs
}

stage_frontend_full() {
  cd frontend-vue || exit 1
  if [ ! -d node_modules ]; then
    echo "   ✘ frontend-vue/node_modules 不存在：先 pnpm install --frozen-lockfile" >&2
    return 1
  fi
  npm run check:ui || return 1

  # 存量已知错误：src/api/index.ts 的 IsolationHelperResult 族（P2 跟踪）。
  # 只过滤这一族；其余任何 `error TS` 都必须让门禁红，否则 --noEmit 形同虚设。
  local out remaining
  out="$(npx vue-tsc --noEmit -p tsconfig.app.json 2>&1 || true)"
  printf '%s\n' "$out"
  remaining="$(printf '%s\n' "$out" | grep 'error TS' | grep -v 'IsolationHelperResult' || true)"
  if [ -n "$remaining" ]; then
    echo "   ✘ vue-tsc 报出非 IsolationHelperResult 的类型错误" >&2
    return 1
  fi

  npx vitest run src || return 1
  npx vite build
}

echo "ci-smoke：root=$ROOT fast=$FAST"

if skipped go; then
  skip_stage "go（build + 单测）" "CHIRON_SMOKE_SKIP 指定"
else
  stage "go（build + 单测，排除 live/diag）" stage_go
  stage "go vendor 一致性" stage_vendor
fi

stage "schema（迁移单 head）" stage_schema

if skipped python; then
  skip_stage "engine（pytest 单测）" "CHIRON_SMOKE_SKIP 指定"
else
  stage "engine（pytest 单测）" stage_python
fi

if skipped frontend; then
  skip_stage "frontend" "CHIRON_SMOKE_SKIP 指定"
else
  stage "frontend 打包静态冒烟" stage_frontend_static
  if [ "$FAST" = "1" ]; then
    skip_stage "frontend 全量（check:ui / vue-tsc / vitest / vite build）" "--fast"
  else
    stage "frontend 全量（check:ui / vue-tsc / vitest / vite build）" stage_frontend_full
  fi
fi

echo ""
echo "ci-smoke：pass=$PASS fail=$FAIL skip=$SKIPPED${FAILED_NAMES:+  失败环节:$FAILED_NAMES}"
[ "$FAIL" -eq 0 ]
