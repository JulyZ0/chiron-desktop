const test = require('node:test')
const assert = require('node:assert/strict')
const hello = require('../windows-hello.cjs')

/** 伪造 execFile：记录调用，回放预设 stdout */
function fakeExec(stdout, err = null) {
  const calls = []
  const fn = (file, args, opts, cb) => {
    calls.push({ file, args, opts })
    setImmediate(() => cb(err, stdout))
  }
  fn.calls = calls
  return fn
}

test.afterEach(() => hello._resetDeps())

test('非 Windows 平台：不执行任何命令，直接判「不可用」', async () => {
  const exec = fakeExec('Availability=Available')
  hello._setDeps({ platform: 'linux', execFile: exec })

  assert.equal(await hello.checkAvailability(), null)
  assert.equal(exec.calls.length, 0, '非 Windows 上不该 spawn powershell')
  assert.equal(await hello.requestVerification('hi'), false)
  assert.equal(exec.calls.length, 0)
})

test('可用性：只认 Available', async () => {
  for (const [value, expected] of [
    ['Available', 'Available'],
    ['DeviceNotPresent', 'DeviceNotPresent'],
    ['NotConfiguredForUser', 'NotConfiguredForUser'],
  ]) {
    hello._setDeps({ platform: 'win32', execFile: fakeExec(`Availability=${value}\n`) })
    assert.equal(await hello.checkAvailability(), expected)
  }
})

test('可用性：输出里没有结果行时为 null（脚本报错也不崩）', async () => {
  hello._setDeps({ platform: 'win32', execFile: fakeExec('some noise\n') })
  assert.equal(await hello.checkAvailability(), null)
})

test('确认：只有 Verified 算通过', async () => {
  for (const [value, expected] of [
    ['Verified', true],
    ['Canceled', false],
    ['RetriesExhausted', false],
    ['DeviceNotPresent', false],
  ]) {
    hello._setDeps({ platform: 'win32', execFile: fakeExec(`Verification=${value}\n`) })
    assert.equal(await hello.requestVerification('confirm'), expected, value)
  }
})

test('确认：execFile 报错且无输出时返回 false（不能把异常当通过）', async () => {
  hello._setDeps({
    platform: 'win32',
    execFile: fakeExec('', new Error('powershell.exe not found')),
  })
  assert.equal(await hello.requestVerification('confirm'), false)
})

test('确认：execFile 同步抛错也返回 false', async () => {
  hello._setDeps({
    platform: 'win32',
    execFile: () => {
      throw new Error('boom')
    },
  })
  assert.equal(await hello.requestVerification('confirm'), false)
})

test('确认脚本：消息里的单引号被翻倍，避免闭合字符串', () => {
  const script = hello.verificationScript("it's a 'test'")
  assert.ok(script.includes("it''s a ''test''"), '单引号必须翻倍')
  assert.ok(script.includes('RequestVerificationAsync'))
})

test('确认脚本：null/undefined 消息退化成空串', () => {
  assert.ok(hello.verificationScript(null).includes("RequestVerificationAsync('')"))
  assert.ok(hello.verificationScript(undefined).includes("RequestVerificationAsync('')"))
})

test('调用 powershell 时是隐藏窗口 + 非交互', async () => {
  const exec = fakeExec('Availability=Available\n')
  hello._setDeps({ platform: 'win32', execFile: exec })
  await hello.checkAvailability()

  const { file, args, opts } = exec.calls[0]
  assert.equal(file, 'powershell.exe')
  assert.ok(args.includes('-NoProfile'))
  assert.ok(args.includes('-NonInteractive'))
  assert.equal(opts.windowsHide, true)
})

test('可用性探测不弹 UI：脚本里只有 CheckAvailabilityAsync', () => {
  assert.ok(hello.AVAILABILITY_SCRIPT.includes('CheckAvailabilityAsync'))
  assert.ok(
    !hello.AVAILABILITY_SCRIPT.includes('RequestVerificationAsync'),
    '探测脚本里绝不能出现会弹窗的调用',
  )
})
