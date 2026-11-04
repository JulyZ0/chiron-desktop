const test = require('node:test')
const assert = require('node:assert/strict')
const { EventEmitter } = require('node:events')
const av = require('../account-verify.cjs')

const SEP = String.fromCharCode(92)

test.afterEach(() => av._resetDeps())

test('parseAccount: DOMAIN + username', () => {
  assert.deepEqual(av.parseAccount('CORP' + SEP + 'alice'), { user: 'alice', domain: 'CORP' })
})

test('parseAccount: 没有域时用 . 表示本机', () => {
  assert.deepEqual(av.parseAccount('alice'), { user: 'alice', domain: '.' })
})

test('parseAccount: 空串与只有域名的都拒绝', () => {
  for (const bad of ['', '   ', null, undefined, 'CORP' + SEP]) {
    assert.equal(av.parseAccount(bad), null, String(bad))
  }
})

test('非 Windows 平台直接判 unsupported，不 spawn', async () => {
  let spawned = false
  av._setDeps({ platform: 'linux', spawn: () => { spawned = true } })
  const r = await av.verifyAccountPassword({ account: 'X' + SEP + 'u', password: 'p' })
  assert.equal(r.reason, 'unsupported')
  assert.equal(spawned, false)
})

test('空密码不发进程', async () => {
  let spawned = false
  av._setDeps({ platform: 'win32', spawn: () => { spawned = true } })
  const r = await av.verifyAccountPassword({ account: 'X' + SEP + 'u', password: '' })
  assert.equal(r.reason, 'empty-password')
  assert.equal(spawned, false)
})

test('Verified → ok', async () => {
  const r = await av.verifyAccountPassword({
    account: 'X' + SEP + 'u',
    password: 'pw',
    verify: async () => ({ verified: true, code: 0 }),
  })
  assert.equal(r.ok, true)
  assert.equal(r.reason, 'verified')
})

test('1326 → bad-credential（唯一算「密码错」的错误码）', async () => {
  const r = await av.verifyAccountPassword({
    account: 'X' + SEP + 'u',
    password: 'pw',
    verify: async () => ({ verified: false, code: 1326 }),
  })
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'bad-credential')
})

test('账号层面问题不报成密码错', async () => {
  for (const code of [1330, 1907, 1331, 1327]) {
    const r = await av.verifyAccountPassword({
      account: 'X' + SEP + 'u',
      password: 'pw',
      verify: async () => ({ verified: false, code }),
    })
    assert.equal(r.reason, 'account-issue', String(code))
    assert.match(r.message, /\S/)
  }
})

test('未知错误码不当作通过', async () => {
  const r = await av.verifyAccountPassword({
    account: 'X' + SEP + 'u',
    password: 'pw',
    verify: async () => ({ verified: false, code: 9999 }),
  })
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'error')
})

test('验证函数抛错也不当作通过', async () => {
  const r = await av.verifyAccountPassword({
    account: 'X' + SEP + 'u',
    password: 'pw',
    verify: async () => { throw new Error('boom') },
  })
  assert.equal(r.ok, false)
  assert.equal(r.reason, 'error')
})

test('密码绝不出现在命令行参数里（核心安全约束）', async () => {
  const seen = { args: null, env: null }
  av._setDeps({
    platform: 'win32',
    spawn: (cmd, args, opts) => {
      seen.args = args
      seen.env = opts.env
      const child = new EventEmitter()
      child.stdout = new EventEmitter()
      child.stderr = new EventEmitter()
      child.stdin = { end: () => setImmediate(() => child.emit('close')) }
      child.kill = () => {}
      return child
    },
  })

  const SECRET = 'Sup3rSecret!' + 'pw'
  await av.verifyAccountPassword({ account: 'X' + SEP + 'u', password: SECRET })

  const joined = JSON.stringify(seen.args)
  assert.equal(joined.includes(SECRET), false, '密码绝不能进 argv')
  assert.equal(seen.args.includes('-EncodedCommand'), true, '脚本必须编码传入')
  assert.equal(seen.env.CHIRON_VERIFY_USER, 'u')
})

test('脚本编码为 base64，明文不出现在参数中', () => {
  const enc = av.encodeCommand(av.VERIFY_SCRIPT)
  assert.match(enc, /^[A-Za-z0-9+/=]+$/)
  assert.equal(enc.includes('LogonUser'), false, '明文脚本文本不能出现在编码结果里')
})

test('输出 Verified 才算过', async () => {
  const run = stdout => {
    av._setDeps({
      platform: 'win32',
      spawn: () => {
        const child = new EventEmitter()
        child.stdout = new EventEmitter()
        child.stderr = new EventEmitter()
        child.stdin = { end: () => setImmediate(() => {
          child.stdout.emit('data', stdout)
          child.emit('close')
        }) }
        child.kill = () => {}
        return child
      },
    })
    return av.verifyAccountPassword({ account: 'X' + SEP + 'u', password: 'pw' })
  }

  assert.equal((await run('Verify=Verified' + String.fromCharCode(10))).ok, true)
  const bad = await run('Verify=Failed' + String.fromCharCode(10) + 'Code=1326' + String.fromCharCode(10))
  assert.equal(bad.reason, 'bad-credential')
  const junk = await run('garbage' + String.fromCharCode(10))
  assert.equal(junk.ok, false)
  assert.equal(junk.reason, 'error')
})

test('LOGON_TYPE 必须是 INTERACTIVE(2)：NETWORK(3) 断网时域账户必然失败', () => {
  // 清空本机数据是纯本地操作，用依赖网络的验证去挡它会让断网/隔离环境
  // 的用户永远清不了数据。INTERACTIVE 走本地缓存凭据。
  assert.match(av.VERIFY_SCRIPT, /LogonUser\(\$user, \$domain, \$pw, 2, 0/)
})

test('域控不可达等环境问题报 unavailable，而不是密码错', async () => {
  for (const code of [1311, 1355, 1722, 5, 1909]) {
    const r = await av.verifyAccountPassword({
      account: 'CORP' + SEP + 'u',
      password: 'pw',
      verify: async () => ({ verified: false, code }),
    })
    assert.equal(r.ok, false, String(code))
    assert.equal(r.reason, 'unavailable', String(code))
    assert.notEqual(r.reason, 'bad-credential')
  }
})

test('1326 仍是 bad-credential，不被环境码吞掉', async () => {
  const r = await av.verifyAccountPassword({
    account: 'CORP' + SEP + 'u',
    password: 'pw',
    verify: async () => ({ verified: false, code: 1326 }),
  })
  assert.equal(r.reason, 'bad-credential')
})
