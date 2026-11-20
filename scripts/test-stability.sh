#!/usr/bin/env bash
# 前端测试稳定性巡检：连续跑 N 轮，统计 flaky（失败但换一轮通过）。
#
# 为什么需要它：全量 157 个 spec 在高负载机器上偶发「5s 超时」抖动 ——
# 单跑必绿、全量偶红。这类失败不会在 CI 上稳定复现（runner 比本机干净），
# 但会消耗每次提交的排查时间；判定"是环境抖动还是真回归"的唯一办法是
# **同一份代码连跑多轮**并记录哪条用例在哪一轮红。
#
# 口径：
#   * 每轮都是**完整**的 `vitest run $TARGET`（不是只跑上次失败的文件）——
#     只跑失败文件会失去"在同一负载形态下复现"的前提；
#   * flaky = 在某轮失败、又在**另一轮**通过（两轮都红的算真失败，两轮都绿的不计入）；
#   * 报告落盘到 .artifacts/test-stability/（**不**提交），含每轮 JSON 与 summary.md。
#
# 用法：
#   scripts/test-stability.sh                     # 3 轮全量 src
#   ROUNDS=5 scripts/test-stability.sh            # 5 轮
#   TARGET="src/api src/composables" scripts/test-stability.sh   # 只跑受影响子集（快）
#   EXTRA_ARGS="--bail=1" scripts/test-stability.sh              # 追加 vitest 参数
#
# 退出码：0 = 全部轮次全绿；1 = 存在真失败（两轮以上都红）；2 = 仅存在 flaky
#         （只要出现过 flaky 就算不干净，但失败可复现性不足，需要人工判断）。
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel)"
FRONTEND="$ROOT/frontend-vue"
ROUNDS="${ROUNDS:-3}"
TARGET="${TARGET:-src}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
OUT="$ROOT/.artifacts/test-stability"

if [ ! -d "$FRONTEND/node_modules" ]; then
  echo "错误：$FRONTEND/node_modules 不存在。先执行 (cd frontend-vue && pnpm install --frozen-lockfile)" >&2
  exit 64
fi

mkdir -p "$OUT"
rm -f "$OUT"/round-*.json

# vitest 的 JSON reporter 结构解析（放外部文件，避免 node -e 里的引号地狱）。
PARSER="$OUT/parse-round.mjs"
cat > "$PARSER" <<'EOF'
// 读取一轮 vitest JSON，输出「失败用例全名」，每行一条。
// vitest 的 JSON 形状：{ testResults: [ { name, assertionResults: [ {fullName,status} ] } ] }
import { readFileSync } from 'node:fs'

const file = process.argv[2]
const data = JSON.parse(readFileSync(file, 'utf8'))
const failed = []
for (const suite of data.testResults ?? []) {
  for (const a of suite.assertionResults ?? []) {
    if (a.status === 'failed') failed.push(`${suite.name}::${a.fullName}`)
  }
}
console.log(failed.join('\n'))
EOF

declare -a ROUND_EXIT
echo "== 稳定性巡检：$ROUNDS 轮，target=[$TARGET] =="

for i in $(seq 1 "$ROUNDS"); do
  echo "── round $i/$ROUNDS ──"
  # shellcheck disable=SC2086  # TARGET / EXTRA_ARGS 需要分词
  ( cd "$FRONTEND" && npx vitest run $TARGET \
      --reporter=dot --reporter=json --outputFile.json="$OUT/round-$i.json" $EXTRA_ARGS )
  ROUND_EXIT[$i]=$?
  echo "round $i exit=${ROUND_EXIT[$i]}"
done

# ── 汇总 ─────────────────────────────────────────────────────────────────
node - "$OUT" "$ROUNDS" <<'EOF'
import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'

const [outDir, roundsArg] = process.argv.slice(2)
const rounds = Number(roundsArg)
const failedByRound = []
const passedByRound = []

for (let i = 1; i <= rounds; i += 1) {
  const file = join(outDir, `round-${i}.json`)
  if (!existsSync(file)) { failedByRound.push(new Set()); passedByRound.push(new Set()); continue }
  const data = JSON.parse(readFileSync(file, 'utf8'))
  const failed = new Set()
  const passed = new Set()
  for (const suite of data.testResults ?? []) {
    for (const a of suite.assertionResults ?? []) {
      const key = `${suite.name}::${a.fullName}`
      if (a.status === 'failed') failed.add(key)
      if (a.status === 'passed') passed.add(key)
    }
  }
  failedByRound.push(failed)
  passedByRound.push(passed)
}

const allFailed = new Set()
failedByRound.forEach((s) => s.forEach((k) => allFailed.add(k)))

const flaky = []
const consistent = []
for (const key of allFailed) {
  const failCount = failedByRound.filter((s) => s.has(key)).length
  const passCount = passedByRound.filter((s) => s.has(key)).length
  if (failCount > 0 && passCount > 0) flaky.push({ key, failCount, passCount })
  else consistent.push({ key, failCount, passCount })
}

const lines = []
lines.push('# 前端测试稳定性报告')
lines.push('')
lines.push(`- 轮次：${rounds}`)
lines.push(`- 收集到的失败用例（去重）：${allFailed.size}`)
lines.push(`- flaky（失败但另一轮通过）：${flaky.length}`)
lines.push(`- 真失败（每轮都红）：${consistent.length}`)
lines.push('')
if (flaky.length) {
  lines.push('## flaky 用例')
  lines.push('')
  for (const f of flaky) lines.push(`- ${f.key} （失败 ${f.failCount} 轮 / 通过 ${f.passCount} 轮）`)
  lines.push('')
}
if (consistent.length) {
  lines.push('## 真失败用例')
  lines.push('')
  for (const f of consistent) lines.push(`- ${f.key} （失败 ${f.failCount} 轮）`)
  lines.push('')
}
const body = lines.join('\n')
writeFileSync(join(outDir, 'summary.md'), `${body}\n`)
console.log(body)
console.log(`报告：${join(outDir, 'summary.md')}`)
process.exit(flaky.length || consistent.length ? (consistent.length ? 1 : 2) : 0)
EOF
STATUS=$?

if [ "$STATUS" -eq 1 ]; then
  echo "结果：存在**可复现**的失败（每轮都红）—— 这是真回归，不是抖动。" >&2
  exit 1
elif [ "$STATUS" -eq 2 ]; then
  echo "结果：仅 flaky（有轮次红、有轮次绿）。详见 $OUT/summary.md" >&2
  exit 2
fi

echo "结果：$ROUNDS 轮全绿。"
