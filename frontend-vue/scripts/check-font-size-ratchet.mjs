#!/usr/bin/env node
/**
 * 字号 token 棘轮（font-size ratchet）。
 *
 * 背景：`src/style.css` 有一套随用户字号缩放的字号 token（`--fs-xs` … `--fs-display`），
 * 但历史组件里大量写死 `font-size: 12px` —— 结果是"设置里调了字号，聊天区纹丝不动"，
 * 而 12px 又低于 12px 最小门禁（11px 的几处直接违反可读性下限）。
 *
 * 本脚本只约束**裸 px**：`var(--fs-*)`、`em`、`inherit`、`var(--chat-text-size)` 等
 * 都不算命中。存量按文件记入 baseline，**任何新增/上调都失败**；把某处换成 token 后
 * 应下调对应条目（`--write` 可重新生成基线，但 CI 只跑检查）。
 *
 * 用法：
 *   node scripts/check-font-size-ratchet.mjs           # 检查（CI）
 *   node scripts/check-font-size-ratchet.mjs --write   # 迁移后重写基线
 */
import { readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'
import { stripComments } from './strip-comments.mjs'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const BASELINE_PATH = join(ROOT, 'scripts/font-size-baseline.json')

/**
 * 裸 px 字号。负向后顾排除 `--ui-font-size: 14px` 这类**自定义属性**：
 * 它的名字里含 `font-size`，但它不是应用到元素的字号声明。
 */
const RAW_PX = /(?<![\w-])font-size\s*:\s*\d+(?:\.\d+)?px\b/g

function walk(dir) {
  const out = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) out.push(...walk(full))
    else if (/\.(vue|css)$/.test(entry)) out.push(full)
  }
  return out
}

function scan() {
  const counts = {}
  for (const file of walk(join(ROOT, 'src'))) {
    const rel = relative(ROOT, file).replaceAll('\\', '/')
    const source = stripComments(readFileSync(file, 'utf8'))
    const n = (source.match(RAW_PX) ?? []).length
    if (n > 0) counts[rel] = n
  }
  return counts
}

const current = scan()
const write = process.argv.includes('--write')

if (write) {
  const sorted = Object.fromEntries(Object.entries(current).sort(([a], [b]) => a.localeCompare(b)))
  writeFileSync(BASELINE_PATH, `${JSON.stringify(sorted, null, 2)}\n`)
  const total = Object.values(sorted).reduce((a, b) => a + b, 0)
  console.log(`字号棘轮基线已写入：${Object.keys(sorted).length} 个文件 / ${total} 处裸 px`)
  process.exit(0)
}

let baseline
try {
  baseline = JSON.parse(readFileSync(BASELINE_PATH, 'utf8'))
} catch {
  console.error('字号棘轮基线缺失或损坏：scripts/font-size-baseline.json')
  process.exit(1)
}

const failures = []
for (const [file, n] of Object.entries(current)) {
  const allowed = baseline[file] ?? 0
  if (n > allowed) {
    failures.push(`${file}: ${n} 处裸 px 字号（基线 ${allowed}）`)
  }
}

const totalCurrent = Object.values(current).reduce((a, b) => a + b, 0)
const totalBaseline = Object.values(baseline).reduce((a, b) => a + b, 0)

if (failures.length) {
  console.error('字号棘轮失败：新增或上调了裸 px font-size')
  for (const line of failures) console.error(`  - ${line}`)
  console.error('改用 src/style.css 的字号 token（--fs-xs … --fs-display）；')
  console.error('确实无法迁移的先写进 baseline 并说明原因（只允许下调）。')
  process.exit(1)
}

console.log(
  `字号棘轮通过：裸 px ${totalCurrent} 处（基线 ${totalBaseline}）/ ${Object.keys(current).length} 个文件`,
)
if (totalCurrent < totalBaseline) {
  console.log('  基线可以下调：node scripts/check-font-size-ratchet.mjs --write')
}
