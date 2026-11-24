#!/usr/bin/env bash
# ci-smoke.sh —— 本地一键复核 CI 的关键链路
#
# 把 `.github/workflows/ci.yml` 里三条硬门禁的**关键部分**在提交前跑一遍，
# 失败即以非 0 退出，不必等 push 之后看 CI 结果：
#
#   1. go       ：build + 单测（排除需要真机 DB/Redis 的 live/diag 用例）
#                 + vendor 一致性（仅当仓库真的带 vendor/modules.txt）
#   2. engine   ：pytest 单测（`-m "not live and not integration and not network"`，
#                 与 ci.yml 的稳定子集同口径；忽略 `test_chiron_client_sdk*.py` ——
#                 它们要求 `clients/python/` 里的客户端包，本仓不含，收集阶段就会
#                 报 ModuleNotFoundError）
#   3. frontend ：electron 打包静态冒烟 + UI 契约 + vue-tsc + vitest + vite build
#
# 另加 schema 校验：`migrations/versions` 必须**只有一个 head**（分叉会让
# `internal/db` 的 ParseMigrationHead 报错、网关拒绝启动）。
#
# 每个环节的结论（pass / fail / skip + 提交号 + 时间）落到
# `.artifacts/ci-smoke/<slug>.result`（**不**提交），供
# `scripts/check-release-ready.sh` 判断发布就绪 —— 那是**同一台机器、同一个
# 工作树**上的证据链：门禁跑的是哪个提交，发布前就核对哪个提交。
#
# 用法：
#   bash scripts/ci-smoke.sh          # 全量
#   bash scripts/ci-smoke.sh --fast   # 跳过前端 typecheck/单测/构建（仍跑打包静态冒烟）
#   bash scripts/ci-smoke.sh --integration
#                                     # 追加 P0-2 真机集成测试带（docker compose，数分钟）；
#                                     # 默认**不跑**；缺 docker / 守护进程未启动时按 skipped 处理
#                                     # （run.sh 以 77 退出，不算失败）
#   bash scripts/ci-smoke.sh --release
#                                     # 发布就绪组合 = **全量** + 末尾追加
#                                     # `scripts/check-release-ready.sh`（迁移单 head /
#                                     # 工作树干净 / go·python·frontend 三门禁证据齐全
#                                     # 且对应当前提交）。与 --fast 互斥。
#
# 环境变量：
#   CHIRON_SMOKE_SKIP=go,python,frontend,integration  跳过指定环节（局部调试用）
#   CHIRON_SMOKE_ARTIFACTS=<dir>                     结论落盘目录（默认 .artifacts/ci-smoke）
#
# 前置：go / node / python 在 PATH 上；全量前端需要 `frontend-vue/node_modules`
# （先 `pnpm install --frozen-lockfile`）；--integration 需要 docker compose。

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

FAST=0
INTEGRATION=0
RELEASE=0
for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --integration) INTEGRATION=1 ;;
    --release) RELEASE=1 ;;
    -h | --help)
      awk 'NR > 1 && /^set -/{exit} NR > 1' "$0"
      exit 0
      ;;
    *)
      echo "未知参数：$arg（可用：--fast --integration --release）" >&2
      exit 2
      ;;
  esac
done

# 发布门禁必须跑完整的门禁：跳过前端全量再"发布就绪"是自相矛盾的组合，
# 与其在 check-release-ready.sh 里报缺证据，不如在这里直接拒绝。
if [ "$RELEASE" = "1" ] && [ "$FAST" = "1" ]; then
  echo "--release 与 --fast 互斥：发布门禁不接受跳过前端全量" >&2
  exit 2
fi

SKIP="${CHIRON_SMOKE_SKIP:-}"
skipped() {
  case ",$SKIP," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

# ── 结论落盘 ──────────────────────────────────────────────────────────────
# key=value 而不是 JSON：这条路径要被 check-release-ready.sh 在最小环境里读回来，
# 而那一侧只有 bash/awk。字段含义：
#   status   pass / fail / skip
#   exit     原始退出码（skip 可能是 2 = 环境不具备，也可能是 0 = 显式跳过）
#   commit   跑门禁时的 HEAD —— 与发布时的 HEAD 不一致即证据作废
#   dirty    跑门禁时工作树是否已脏（0/1，供人判断，不作为门禁判据）
OUT="${CHIRON_SMOKE_ARTIFACTS:-$ROOT/.artifacts/ci-smoke}"
mkdir -p "$OUT" 2>/dev/null || true
rm -f "$OUT"/*.result 2>/dev/null || true
COMMIT="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
DIRTY=0
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  DIRTY=1
fi

record_result() {
  # record_result <slug> <status> <exit> <seconds>
  local slug="$1" status="$2" rc="$3" secs="$4" file
  file="$OUT/$slug.result"
  {
    echo "stage=$slug"
    echo "status=$status"
    echo "exit=$rc"
    echo "duration_s=$secs"
    echo "commit=$COMMIT"
    echo "dirty=$DIRTY"
    echo "finished_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } >"$file" 2>/dev/null || true
}

PASS=0
FAIL=0
SKIPPED=0
FAILED_NAMES=""

stage() {
  # stage <slug> <名字> <函数>；函数返回 2 表示"环境不具备，跳过"（不算失败）
  local slug="$1"
  local name="$2"
  local fn="$3"
  local start rc secs
  start=$(date +%s)
  echo ""
  echo "── $name"
  "$fn"
  rc=$?
  secs=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    echo "   ✔ $name（${secs}s）"
    PASS=$((PASS + 1))
    record_result "$slug" pass 0 "$secs"
  elif [ "$rc" -eq 2 ]; then
    echo "   ↷ $name：环境不具备，跳过"
    SKIPPED=$((SKIPPED + 1))
    record_result "$slug" skip "$rc" "$secs"
  else
    echo "   ✘ $name"
    FAIL=$((FAIL + 1))
    FAILED_NAMES="$FAILED_NAMES $name"
    record_result "$slug" fail "$rc" "$secs"
  fi
}

skip_stage() {
  # skip_stage <slug> <名字> <原因>
  echo ""
  echo "── $2"
  echo "   ↷ 跳过：$3"
  SKIPPED=$((SKIPPED + 1))
  record_result "$1" skip 0 0
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
# 不依赖 alembic（本地未必装了 requirements-migrate.txt）：解析脚本只读
# revision / down_revision，判据与 internal/db 的 ParseMigrationHead 同构。
# 发布门禁（check-release-ready.sh）用的是**同一个**脚本，不是同一段复制 ——
# 两份正则各自漂移时，本地绿与发布红会打架。
stage_schema() {
  python scripts/check_migration_head.py
}

# ── 3. Python 引擎 ────────────────────────────────────────────────────────
# CI 口径是**全量**单测（`tests`）。本机另有一套取舍：
# 开发机的 httpx / openai / anthropic 版本与 `requirements.txt` 钉的不一定相同，
# 于是 provider 一族用例会因上游 SDK 的 URL 解析/参数行为变化而红（实测 31 failed /
# 3886 passed，全部落在 provider・todo_plan・git_checkpoint 等 SDK 敏感面，
# 与"代码有问题"是两回事）。Windows 冷启动即全量只会训练人忽略红。
# 因此：CI 或 `CHIRON_SMOKE_PY_FULL=1` 走全量；否则跑**稳定子集的冒烟**
# （api + 引擎装配 + 模式 + 工具守卫 —— 覆盖最核心的契约面，秒级完成）。
#
# marker 一律带 `not network`（`test_sandbox_service_multiprocess.py` 那类要真起
# 监听端口）—— 与 ci.yml 的 Pytest 步骤同口径。此前这里只写了 `not live and not
# integration`，于是 ci-smoke 在 CI/发布机上比 ci.yml 多跑一个"多进程沙箱服务"用例：
# PR 门禁把它排除、发布门禁却带着它，两边结论天然可能不一致。它由 ci.yml 的
# real-stack integration job 覆盖（`-m "integration or live or network"`）。
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
      python -m pytest $targets -q \
      -m "not live and not integration and not network" \
      --ignore-glob="tests/test_chiron_client_sdk*.py"
  )
}

# ── 4. 前端 ───────────────────────────────────────────────────────────────
# 打包静态冒烟**不需要 node_modules**（只读源码/配置），任何环境都跑。
stage_frontend_static() {
  node --test frontend-vue/electron/__tests__/packaging.test.cjs
}

stage_frontend_full() {
  # 整段放进**子 shell**：它必须 cd 进 frontend-vue，而在函数里直接 cd 会把工作目录
  # 泄漏给后续环节 —— 后面以前没有别的环节，所以这个泄漏一直没暴露；`--release` 的
  # release-ready 会因此在 frontend-vue 里找脚本（No such file or directory）。
  (
    cd frontend-vue || exit 1
    if [ ! -d node_modules ]; then
      echo "   ✘ frontend-vue/node_modules 不存在：先 pnpm install --frozen-lockfile" >&2
      exit 1
    fi
    npm run check:ui || exit 1

    # 与 PR / 桌面发布共用硬门禁；先注入失败，证明空输出和进程级错误也不会假绿。
    node --test scripts/ci-typecheck.test.cjs || exit 1
    bash scripts/ci-typecheck.sh || exit 1

    npx vitest run src || exit 1
    npx vite build
  )
}

# ── 5. 真机集成测试带（opt-in）────────────────────────────────────────────
# 默认不跑：它要 docker compose 起 PG/Redis/网关/引擎/pageindex/假 LLM，数分钟起。
# run.sh 的契约是"环境不具备 → 77"，这里翻译成 stage 的"跳过（2）"，与 go/python
# 两边缺失依赖的语义一致；真正的断言失败仍然是 1。
stage_integration() {
  bash scripts/integration/run.sh
  local rc=$?
  if [ "$rc" -eq 77 ]; then
    return 2
  fi
  return "$rc"
}

# ── 6. 发布就绪自检（--release）───────────────────────────────────────────
# 判据全在 scripts/check-release-ready.sh（迁移单 head / 工作树干净 / 三门禁证据
# 齐全且对应当前提交）。这里只做串联，顺序不能反：**先跑门禁、再看证据** ——
# 反了就是拿上一次运行的残留结论给这一次签字。
stage_release_ready() {
  # 绝对路径：即使将来某个环节又漏了 cd，这里也不会"找不到脚本"而假失败。
  bash "$ROOT/scripts/check-release-ready.sh"
}

DIRTY_NOTE=""
if [ "$DIRTY" = "1" ]; then DIRTY_NOTE="，工作树已脏"; fi
echo "ci-smoke：root=$ROOT fast=$FAST integration=$INTEGRATION release=$RELEASE"
echo "         结论落盘：${OUT#"$ROOT"/}（commit ${COMMIT:0:12}${DIRTY_NOTE}）"

if skipped go; then
  skip_stage go "go（build + 单测）" "CHIRON_SMOKE_SKIP 指定"
else
  stage go "go（build + 单测，排除 live/diag）" stage_go
  stage go-vendor "go vendor 一致性" stage_vendor
fi

stage schema "schema（迁移单 head）" stage_schema

if skipped python; then
  skip_stage python "engine（pytest 单测）" "CHIRON_SMOKE_SKIP 指定"
else
  stage python "engine（pytest 单测）" stage_python
fi

if skipped frontend; then
  skip_stage frontend "frontend" "CHIRON_SMOKE_SKIP 指定"
else
  stage frontend-static "frontend 打包静态冒烟" stage_frontend_static
  if [ "$FAST" = "1" ]; then
    skip_stage frontend-full "frontend 全量（check:ui / vue-tsc / vitest / vite build）" "--fast"
  else
    stage frontend-full "frontend 全量（check:ui / vue-tsc / vitest / vite build）" stage_frontend_full
  fi
fi

if [ "$INTEGRATION" = "1" ]; then
  if skipped integration; then
    skip_stage integration "integration（真机 compose 链路）" "CHIRON_SMOKE_SKIP 指定"
  else
    stage integration "integration（真机 compose 链路）" stage_integration
  fi
else
  skip_stage integration "integration（真机 compose 链路）" "默认跳过；加 --integration 开启"
fi

if [ "$RELEASE" = "1" ]; then
  stage release-ready "release-ready（发布就绪自检）" stage_release_ready
fi

echo ""
echo "ci-smoke：pass=$PASS fail=$FAIL skip=$SKIPPED${FAILED_NAMES:+  失败环节:$FAILED_NAMES}"
[ "$FAIL" -eq 0 ]
