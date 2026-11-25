'use strict'
/**
 * H56：Windows 子进程树终止（PRD 9.8）。
 *
 * 缺陷形态：安装/设备 Git 操作超时或取消时只 `child.kill()`。
 *   - Windows 上那只杀**直接子进程**，python.exe 派生的 pip、git 派生的 credential
 *     helper / ssh 会继续跑；
 *   - 它们继续持有文件与 `index.lock`，于是紧接着的"清理临时目录/覆盖安装/重试"
 *     全部失败，而报错指向的是删目录 —— 真正的元凶（残留进程）在日志里看不到；
 *   - 用户点了"取消"，任务仍在后台写盘。
 *
 * 修法：Windows `taskkill /T /F /PID`；POSIX 自成进程组后 `kill(-pid)`。
 * 这里用注入的 spawnSync / process.kill 验证"发出了什么命令"，不依赖真实进程树。
 */
const test = require('node:test')
const assert = require('node:assert')

const { killProcessTree, killOptions } = require('../proc-tree.cjs')

function fakeChild(pid = 4321) {
  const killed = []
  return {
    pid,
    killed,
    kill(signal) {
      killed.push(signal || 'SIGTERM')
      return true
    },
  }
}

test('win32：用 taskkill /T /F 杀整棵树', async () => {
  const calls = []
  const child = fakeChild()
  const acted = await killProcessTree(child, {
    platform: 'win32',
    spawnSync: (cmd, args, opts) => {
      calls.push({ cmd, args, opts })
      return { status: 0 }
    },
  })
  assert.equal(acted, true)
  assert.equal(calls.length, 1)
  assert.equal(calls[0].cmd, 'taskkill')
  assert.deepEqual(calls[0].args, ['/T', '/F', '/PID', '4321'])
  // 仍然保底 kill 一次（taskkill 可能因权限失败）
  assert.deepEqual(child.killed, ['SIGKILL'])
})

test('win32：taskkill 非零退出要上报，但仍保底 kill', async () => {
  const logs = []
  const child = fakeChild()
  await killProcessTree(child, {
    platform: 'win32',
    log: (level, msg) => logs.push(`${level}:${msg}`),
    spawnSync: () => ({ status: 128, stderr: 'Access is denied.' }),
  })
  assert.ok(logs.some(l => l.includes('taskkill')), '失败必须留痕，否则排查时看不到')
  assert.deepEqual(child.killed, ['SIGKILL'])
})

test('posix：对进程组发 SIGKILL（负数 PID）', async () => {
  const signals = []
  const original = process.kill
  process.kill = (pid, sig) => {
    signals.push([pid, sig])
    return true
  }
  try {
    const child = fakeChild(777)
    await killProcessTree(child, { platform: 'linux' })
    assert.deepEqual(signals[0], [-777, 'SIGKILL'], '必须是负 PID（整组），不是单个进程')
    assert.deepEqual(child.killed, ['SIGKILL'])
  } finally {
    process.kill = original
  }
})

test('posix：进程组已消失时仍保底 kill，不抛错', async () => {
  const original = process.kill
  process.kill = () => {
    throw new Error('ESRCH')
  }
  try {
    const child = fakeChild(778)
    const acted = await killProcessTree(child, { platform: 'darwin' })
    assert.equal(acted, true)
    assert.deepEqual(child.killed, ['SIGKILL'])
  } finally {
    process.kill = original
  }
})

test('pid 缺失时只走保底 kill', async () => {
  const calls = []
  const child = { kill: s => (calls.push(s), true) }
  const acted = await killProcessTree(child, {
    platform: 'win32',
    spawnSync: () => ({ status: 0 }),
  })
  assert.equal(acted, true)
  assert.deepEqual(calls, ['SIGKILL'])
})

test('空 child 返回 false（不抛）', async () => {
  assert.equal(await killProcessTree(null), false)
  assert.equal(await killProcessTree(undefined), false)
})

test('killOptions：posix 自成进程组，win32 不加键', () => {
  assert.deepEqual(killOptions('win32'), {})
  assert.deepEqual(killOptions('linux'), { detached: true })
  assert.deepEqual(killOptions('darwin'), { detached: true })
})

test('源码守卫：安装与设备 Git 的超时/取消路径都走进程树终止', () => {
  const fs = require('node:fs')
  const path = require('node:path')
  const dir = path.resolve(__dirname, '..')
  const install = fs.readFileSync(path.join(dir, 'runtime-install.cjs'), 'utf8')
  const deviceFs = fs.readFileSync(path.join(dir, 'device-fs.cjs'), 'utf8')

  assert.ok(install.includes('killProcessTree'), 'runtime-install 必须用进程树终止')
  assert.ok(deviceFs.includes('killProcessTree'), 'device-fs 必须用进程树终止')
  // 不允许再出现"裸 child.kill()"（旧形态）
  for (const [name, src] of [
    ['runtime-install.cjs', install],
    ['device-fs.cjs', deviceFs],
  ]) {
    assert.ok(!/child\.kill\(\)/.test(src), `${name} 里仍有裸 child.kill()（只杀直接子进程）`)
  }
})
