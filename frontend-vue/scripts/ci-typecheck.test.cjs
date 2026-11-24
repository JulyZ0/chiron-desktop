'use strict'

/**
 * ci-typecheck.sh 的故障注入测试。
 *
 * 用 PATH 里的假 pnpm 控制 vue-tsc 的输出与退出码，直接跑生产门禁脚本。
 * 重点不是“日志里有没有 error TS”，而是进程级状态是否原样传回 CI。
 */

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const FRONTEND_ROOT = path.resolve(__dirname, '..')
const REPO_ROOT = path.resolve(FRONTEND_ROOT, '..')
const GATE = path.join(FRONTEND_ROOT, 'scripts', 'ci-typecheck.sh')
const BASH = process.env.CHIRON_TEST_BASH || 'bash'

function runGate({ code = 0, stdout = '', stderr = '', launchFailure = false } = {}) {
  const binDir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-typecheck-bin-'))
  const argsFile = path.join(binDir, 'args.txt')
  const pnpm = path.join(binDir, 'pnpm')
  try {
    fs.writeFileSync(
      pnpm,
      launchFailure
        ? '#!/definitely/missing/chiron-test-interpreter\n'
        : [
            '#!/bin/sh',
            'printf "%s\\n" "$@" > "$FAKE_ARGS_FILE"',
            'printf "%s" "${FAKE_STDOUT:-}"',
            'printf "%s" "${FAKE_STDERR:-}" >&2',
            'exit "$FAKE_EXIT_CODE"',
            '',
          ].join('\n'),
      { mode: 0o755 },
    )

    const result = require('node:child_process').spawnSync(BASH, [GATE], {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      env: {
        ...process.env,
        PATH: `${binDir}${path.delimiter}${process.env.PATH || ''}`,
        FAKE_ARGS_FILE: argsFile,
        FAKE_EXIT_CODE: String(code),
        FAKE_STDOUT: stdout,
        FAKE_STDERR: stderr,
      },
    })
    const args = fs.existsSync(argsFile) ? fs.readFileSync(argsFile, 'utf8').trim().split(/\r?\n/) : []
    return { ...result, args }
  } finally {
    fs.rmSync(binDir, { recursive: true, force: true })
  }
}

test('成功时透传输出，并强制检查 app/node 两个项目且不写产物', () => {
  const result = runGate({ stdout: 'typecheck complete\n', stderr: 'warning\n' })
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout, 'typecheck complete\n')
  assert.equal(result.stderr, 'warning\n')
  assert.deepEqual(result.args, ['exec', 'vue-tsc', '-b', '--force', '--noEmit'])
})

test('成功且无诊断输出仍正常通过', () => {
  const result = runGate()
  assert.equal(result.status, 0, result.stderr)
  assert.equal(result.stdout, '')
  assert.equal(result.stderr, '')
})

for (const fixture of [
  { name: '普通类型错误', code: 2, stderr: 'src/x.ts(1,1): error TS2322: broken\n' },
  { name: '配置错误且不含 error TS', code: 1, stderr: 'Failed to load tsconfig.json: ENOENT\n' },
  { name: '进程崩溃且完全无输出', code: 134 },
  { name: '未知非零退出码', code: 42 },
]) {
  test(`${fixture.name}：原始退出码 ${fixture.code} 必须传播`, () => {
    const result = runGate(fixture)
    assert.equal(result.status, fixture.code, result.stderr)
    assert.equal(result.stdout, fixture.stdout || '')
    assert.equal(result.stderr, fixture.stderr || '')
  })
}

test('工具无法启动：启动失败不能被空输出吞掉', () => {
  const result = runGate({ launchFailure: true })
  // GNU bash 用 127 表示找不到解释器；Cygwin/MSYS bash 用 126 表示无法执行。
  assert.ok([126, 127].includes(result.status), `unexpected exit: ${result.status}`)
  assert.equal(result.stdout, '')
  assert.ok(result.stderr.length > 0, '启动失败必须留下诊断')
})

test('PR、桌面发布与 ci-smoke 使用同一个硬门禁，不保留退出码吞并', () => {
  for (const relative of [
    '.github/workflows/ci.yml',
    '.github/workflows/desktop.yml',
    'scripts/ci-smoke.sh',
  ]) {
    const source = fs.readFileSync(path.join(REPO_ROOT, relative), 'utf8')
    assert.match(source, /bash (?:frontend-vue\/)?scripts\/ci-typecheck\.sh/, relative)
    assert.doesNotMatch(source, /(?:npx|pnpm exec) vue-tsc/, `${relative} 不应复制类型检查命令`)
  }
})
