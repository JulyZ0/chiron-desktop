'use strict'

const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { EventEmitter } = require('node:events')
const { execFileSync } = require('node:child_process')

const { handleDeviceOp, runDeviceGit } = require('../device-fs.cjs')

const GIT_NULL = process.platform === 'win32' ? 'NUL' : os.devNull
const SERVER_PREFIX = [
  '--no-optional-locks',
  '-c', 'core.quotepath=false',
  '-c', 'core.fsmonitor=false',
  '-c', 'core.alternateRefsCommand=',
  '-c', 'diff.external=',
  '-c', 'core.pager=cat',
]

function tempDir(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'chiron-device-git-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

function gitEnv() {
  const env = {}
  for (const key of ['PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP']) {
    if (process.env[key]) env[key] = process.env[key]
  }
  return {
    ...env,
    GIT_CONFIG_NOSYSTEM: '1',
    GIT_CONFIG_GLOBAL: GIT_NULL,
    GIT_TERMINAL_PROMPT: '0',
  }
}

function git(cwd, ...args) {
  return execFileSync('git', ['-C', cwd, ...args], {
    env: gitEnv(),
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 15000,
  })
}

function repoWithCommit(t) {
  const root = tempDir(t)
  git(root, 'init', '-q')
  git(root, 'config', 'user.name', 'Device Git Test')
  git(root, 'config', 'user.email', 'device-git@example.invalid')
  git(root, 'config', 'commit.gpgSign', 'false')
  fs.writeFileSync(path.join(root, 'tracked.txt'), 'before\n', 'utf8')
  git(root, 'add', '-A')
  git(root, 'commit', '-q', '-m', 'initial')
  return root
}

function harmlessProbe(t) {
  const dir = tempDir(t)
  const marker = path.join(dir, 'executed.marker')
  const script = path.join(dir, 'probe.cjs')
  fs.writeFileSync(script, "require('node:fs').writeFileSync(process.argv[2], 'executed')\n", 'utf8')
  const command = `"${process.execPath}" "${script}" "${marker}"`
  return { marker, command }
}

function fakeSpawn(responses) {
  const calls = []
  const spawnGit = (command, args, options) => {
    calls.push({ command, args: [...args], options })
    const child = new EventEmitter()
    child.stdout = new EventEmitter()
    child.stderr = new EventEmitter()
    child.kill = () => {}
    const response = responses.shift() || { code: 0, stdout: '' }
    const chunks = Array.isArray(response.stdout) ? response.stdout : [response.stdout]
    queueMicrotask(() => {
      for (const chunk of chunks) {
        if (chunk) child.stdout.emit('data', Buffer.from(chunk))
      }
      if (response.stderr) child.stderr.emit('data', Buffer.from(response.stderr))
      child.emit('close', response.code == null ? 0 : response.code)
    })
    return child
  }
  return { calls, spawnGit }
}

test('device git accepts only the server static prefix and known command shapes', async t => {
  const root = repoWithCommit(t)
  fs.writeFileSync(path.join(root, 'tracked.txt'), 'after\n', 'utf8')
  const status = ['status', '--porcelain=v1', '-z']
  const result = await handleDeviceOp({
    op: 'git', root, path: root, args: [...SERVER_PREFIX, ...status],
  })
  assert.equal(result.code, 0, result.stderr)
  assert.match(result.stdout, /tracked\.txt/)

  const vcs = await handleDeviceOp({
    op: 'git', root, path: root, args: [...SERVER_PREFIX, 'rev-parse', '--is-inside-work-tree'],
  })
  assert.equal(vcs.code, 0, vcs.stderr)
  assert.equal(vcs.stdout.trim(), 'true')
})

test('device git initializes a normal project repository', async t => {
  const root = tempDir(t)
  const result = await handleDeviceOp({
    op: 'git', root, path: root, args: [...SERVER_PREFIX, 'init', '-q'],
  })
  assert.equal(result.code, 0, result.stderr)
  assert.equal(fs.statSync(path.join(root, '.git')).isDirectory(), true)
})

test('device git rejects arbitrary config, aliases, hooks, helpers, pagers and unknown options', async t => {
  const root = repoWithCommit(t)
  const rejected = [
    ['-c', 'core.pager=malicious', 'status'],
    ['--config-env=core.pager=MALICIOUS', 'status'],
    ['status', '--config-env=core.pager=MALICIOUS'],
    ['pwn'],
    ['alias'],
    ['--exec-path'],
    ['-C', root, 'status'],
    ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
    ['status', '--porcelain=v1', '-z', '--ignored'],
    ['rev-parse', '--exec-path'],
    ['log', '--oneline'],
    ['diff', '--no-ext-diff'],
    ['commit', '-m', 'no'],
    ['status', '--porcelain=v1', '-z', '--', '../outside'],
    [...SERVER_PREFIX.slice(0, -1), '-c', 'core.pager=malicious', 'status'],
  ]
  for (const args of rejected) {
    await assert.rejects(
      () => handleDeviceOp({ op: 'git', root, path: root, args }),
      /git args|git command/i,
      args.join(' '),
    )
  }
})

test('device git never executes harmless malicious repository configuration', async t => {
  const root = repoWithCommit(t)
  const { marker, command } = harmlessProbe(t)
  for (const key of [
    'filter.malicious.clean',
    'filter.malicious.smudge',
    'filter.malicious.process',
    'diff.malicious.textconv',
    'core.fsmonitor',
    'core.pager',
    'credential.helper',
  ]) git(root, 'config', key, command)
  git(root, 'config', 'filter.malicious.required', 'true')
  git(root, 'config', 'pager.status', 'true')
  fs.writeFileSync(path.join(root, '.gitattributes'), '*.txt filter=malicious diff=malicious\n', 'utf8')
  fs.writeFileSync(path.join(root, 'tracked.txt'), 'after\n', 'utf8')

  const result = await handleDeviceOp({
    op: 'git', root, path: root, args: [...SERVER_PREFIX, 'status', '--porcelain=v1', '-z'],
  })

  assert.equal(result.code, 0, result.stderr)
  assert.match(result.stdout, /tracked\.txt/)
  assert.equal(fs.existsSync(marker), false, 'repository helper executed')
})

test('device git discovers filters through included configuration', async t => {
  const root = repoWithCommit(t)
  const { marker, command } = harmlessProbe(t)
  const included = path.join(root, 'included.config')
  git(root, 'config', '-f', included, 'filter.included.clean', command)
  git(root, 'config', '-f', included, 'filter.included.smudge', command)
  git(root, 'config', 'include.path', included)
  fs.writeFileSync(path.join(root, '.gitattributes'), '*.txt filter=included\n', 'utf8')
  fs.writeFileSync(path.join(root, 'tracked.txt'), 'after\n', 'utf8')

  const result = await handleDeviceOp({
    op: 'git', root, path: root, args: ['status', '--porcelain=v1', '-z'],
  })

  assert.equal(result.code, 0, result.stderr)
  assert.equal(fs.existsSync(marker), false, 'included repository filter executed')
})

test('device git preserves UTF-8 filter names split across stdout chunks', async t => {
  const root = tempDir(t)
  const listing = Buffer.from('filter.évil.clean\nprobe\0', 'utf8')
  const split = listing.indexOf(0xc3) + 1
  const fake = fakeSpawn([
    { code: 0, stdout: [listing.subarray(0, split), listing.subarray(split)] },
    { code: 0, stdout: '' },
  ])

  const result = await runDeviceGit(root, ['status', '--porcelain=v1', '-z'], 1000, fake)

  assert.equal(result.code, 0, result.stderr)
  assert.ok(fake.calls[1].args.includes('filter.évil.clean='))
  assert.ok(fake.calls[1].args.includes('filter.évil.required=false'))
})

test('device git fails closed when the configuration probe fails', async t => {
  const root = tempDir(t)
  for (const response of [
    { code: 1, stderr: 'bad repository configuration' },
    { code: 0, stdout: 'malformed' },
    { code: -1, stderr: 'git timeout' },
  ]) {
    const fake = fakeSpawn([response])
    const result = await runDeviceGit(root, ['status', '--porcelain=v1', '-z'], 1000, fake)
    assert.equal(result.code, -1)
    assert.match(result.stderr, /configuration probe failed/)
    assert.equal(fake.calls.length, 1, 'worktree command must not run after a failed probe')
  }
})

test('device git uses a minimal environment and dynamic filter overrides', async t => {
  const root = tempDir(t)
  const fake = fakeSpawn([
    {
      code: 0,
      stdout: 'filter.evil.clean\nprobe\0filter.evil.smudge\nprobe\0diff.evil.textconv\nprobe\0',
    },
    { code: 0, stdout: ' M tracked.txt\0' },
  ])
  const oldSecret = process.env.HOST_SECRET
  const oldConfig = process.env.GIT_CONFIG_COUNT
  process.env.HOST_SECRET = 'must-not-leak'
  process.env.GIT_CONFIG_COUNT = '1'
  t.after(() => {
    if (oldSecret === undefined) delete process.env.HOST_SECRET
    else process.env.HOST_SECRET = oldSecret
    if (oldConfig === undefined) delete process.env.GIT_CONFIG_COUNT
    else process.env.GIT_CONFIG_COUNT = oldConfig
  })

  const result = await runDeviceGit(root, ['status', '--porcelain=v1', '-z'], 1000, fake)

  assert.equal(result.code, 0, result.stderr)
  assert.equal(fake.calls.length, 2)
  const probe = fake.calls[0]
  const execution = fake.calls[1]
  assert.deepEqual(probe.args.slice(-4), ['config', '--includes', '--null', '--list'])
  for (const call of fake.calls) {
    assert.equal(call.command, 'git')
    assert.equal(call.options.windowsHide, true)
    assert.equal(call.options.env.GIT_CONFIG_NOSYSTEM, '1')
    assert.equal(call.options.env.GIT_CONFIG_GLOBAL, GIT_NULL)
    assert.equal(call.options.env.GIT_TERMINAL_PROMPT, '0')
    assert.equal(call.options.env.GIT_PAGER, 'cat')
    assert.equal(call.options.env.GIT_ALLOW_PROTOCOL, '')
    assert.equal(call.options.env.GIT_PROTOCOL_FROM_USER, '0')
    assert.equal(call.options.env.HOST_SECRET, undefined)
    assert.equal(call.options.env.GIT_CONFIG_COUNT, undefined)
    assert.ok(call.args.includes('--no-pager'))
    assert.ok(call.args.includes(`core.hooksPath=${GIT_NULL}`))
    assert.ok(call.args.includes('core.fsmonitor=false'))
    assert.ok(call.args.includes('credential.helper='))
    assert.ok(call.args.includes('diff.external='))
    assert.ok(call.args.includes('protocol.allow=never'))
  }
  for (const setting of [
    'filter.evil.clean=',
    'filter.evil.smudge=',
    'filter.evil.process=',
    'filter.evil.required=false',
    'diff.evil.textconv=',
    'diff.evil.command=',
  ]) assert.ok(execution.args.includes(setting), setting)
})

test('device git refuses malformed repository configuration before status', async t => {
  const root = repoWithCommit(t)
  fs.appendFileSync(path.join(root, '.git', 'config'), '\n[broken\n', 'utf8')

  const result = await handleDeviceOp({
    op: 'git', root, path: root, args: ['status', '--porcelain=v1', '-z'],
  })

  assert.equal(result.code, -1)
  assert.match(result.stderr, /configuration probe failed/)
})
