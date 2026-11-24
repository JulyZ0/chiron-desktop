'use strict'
/**
 * H49：本地路径打开与执行授权。
 *
 * 修复前：`desktop:open-path` 只检查「绝对路径 + 存在」。工作台的文件列表来自后端
 * API，服务端（或被投毒的后端）放一条可执行文件/脚本，用户点一下就用系统默认程序
 * 启动了它 —— 渲染层没有任何机会拦住，主进程也没有第二道判据。
 *
 * 现在钉死四件事：
 *   1. canonical（realpath）路径必须落在**用户批准的项目根**内，否则先问用户；
 *   2. 默认只 reveal / 打开白名单文档；
 *   3. 可执行类型（含 POSIX 执行位）必须经独立确认；
 *   4. UNC / 网络路径直接拒绝（连确认机会都不给）。
 */
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')

const guard = require('../open-path-guard.cjs')
const rootsStore = require('../approved-roots.cjs')

function tempDir(t, prefix = 'chiron-open-path-') {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), prefix))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  return dir
}

/** 一个真实的临时项目根（含一个文档、一个可执行文件、一个子目录）。 */
function tempProject(t) {
  const root = tempDir(t)
  fs.writeFileSync(path.join(root, 'notes.md'), '# hi', 'utf8')
  fs.writeFileSync(path.join(root, 'setup.exe'), 'MZ', 'utf8')
  fs.writeFileSync(path.join(root, 'unknown.weird'), 'x', 'utf8')
  fs.mkdirSync(path.join(root, 'assets'))
  fs.writeFileSync(path.join(root, 'assets', 'logo.png'), 'png', 'utf8')
  return root
}

const noConfirm = async () => false
const yesConfirm = async () => true

test('classifyTarget：可执行、文档、未知与目录各不相同', () => {
  assert.equal(guard.classifyTarget({ target: 'C:\\a\\setup.exe' }), 'executable')
  assert.equal(guard.classifyTarget({ target: 'C:\\a\\notes.md' }), 'document')
  assert.equal(guard.classifyTarget({ target: 'C:\\a\\blob.weird' }), 'unknown')
  assert.equal(guard.classifyTarget({ target: 'C:\\a\\dir', isDirectory: true }), 'dir')
  // POSIX 执行位即使没有扩展名也算可执行
  assert.equal(guard.classifyTarget({ target: '/usr/local/bin/deploy', mode: 0o755, platform: 'linux' }), 'executable')
})

test('open-path：根外的路径被拒（修复前只要绝对+存在就放行）', async t => {
  const root = tempProject(t)
  const outside = tempDir(t, 'chiron-outside-')
  const target = path.join(outside, 'report.pdf')
  fs.writeFileSync(target, 'pdf', 'utf8')

  const verdict = await guard.authorizeOpenPath({
    target,
    roots: [rootsStore.canonicalizeDir(root)],
    confirmRoot: noConfirm,
    confirmExecutable: yesConfirm,
  })
  assert.equal(verdict.ok, false)
  assert.equal(verdict.reason, 'outside-approved-root')
})

test('open-path：根内文档直接放行；reveal 也放行', async t => {
  const root = tempProject(t)
  const roots = [rootsStore.canonicalizeDir(root)]
  const doc = await guard.authorizeOpenPath({ target: path.join(root, 'notes.md'), roots })
  assert.equal(doc.ok, true)
  assert.equal(doc.action, 'open')
  assert.equal(doc.kind, 'document')

  const reveal = await guard.authorizeOpenPath({
    target: path.join(root, 'notes.md'), reveal: true, roots,
  })
  assert.equal(reveal.ok, true)
  assert.equal(reveal.action, 'reveal')
})

test('open-path：可执行文件在没有独立确认时被拒（修复前会被直接启动）', async t => {
  const root = tempProject(t)
  const roots = [rootsStore.canonicalizeDir(root)]
  const verdict = await guard.authorizeOpenPath({
    target: path.join(root, 'setup.exe'),
    roots,
    confirmExecutable: noConfirm,
  })
  assert.equal(verdict.ok, false)
  assert.equal(verdict.reason, 'executable-needs-confirmation')
  assert.equal(verdict.kind, 'executable')
})

test('open-path：用户完成独立确认后才放行可执行文件', async t => {
  const root = tempProject(t)
  const roots = [rootsStore.canonicalizeDir(root)]
  const seen = []
  const verdict = await guard.authorizeOpenPath({
    target: path.join(root, 'setup.exe'),
    roots,
    confirmExecutable: async info => {
      seen.push(info)
      return true
    },
  })
  assert.equal(verdict.ok, true)
  assert.equal(verdict.kind, 'executable')
  assert.equal(seen.length, 1)
  assert.equal(seen[0].ext, 'exe', '确认对话框必须知道具体类型')
})

test('open-path：未知类型同样要确认（不给"关联程序是谁"留空子）', async t => {
  const root = tempProject(t)
  const roots = [rootsStore.canonicalizeDir(root)]
  const verdict = await guard.authorizeOpenPath({
    target: path.join(root, 'unknown.weird'),
    roots,
    confirmExecutable: noConfirm,
  })
  assert.equal(verdict.ok, false)
  assert.equal(verdict.reason, 'unknown-needs-confirmation')
})

test('open-path：UNC / 网络路径直接拒绝，连确认都不弹', async t => {
  let asked = 0
  for (const target of ['\\\\server\\share\\x.exe', '//server/share/x.pdf', '\\\\?\\UNC\\server\\share\\x']) {
    const verdict = await guard.authorizeOpenPath({
      target,
      roots: [rootsStore.canonicalizeDir(os.tmpdir())],
      confirmRoot: async () => { asked += 1; return true },
      confirmExecutable: async () => { asked += 1; return true },
    })
    assert.equal(verdict.ok, false, target)
    assert.equal(verdict.reason, 'unc-path', target)
  }
  assert.equal(asked, 0, 'UNC 路径不该触发任何确认对话框')
})

test('open-path：未授权根先问用户；用户拒绝即终止，同意后才放行', async t => {
  const root = tempProject(t)
  const target = path.join(root, 'notes.md')

  const denied = await guard.authorizeOpenPath({
    target,
    root,
    roots: [],
    confirmRoot: noConfirm,
    confirmExecutable: yesConfirm,
  })
  assert.equal(denied.ok, false)
  assert.equal(denied.reason, 'root-not-approved')

  const approved = await guard.authorizeOpenPath({
    target,
    root,
    roots: [],
    confirmRoot: async info => info.root === rootsStore.canonicalizeDir(root),
    confirmExecutable: yesConfirm,
  })
  assert.equal(approved.ok, true)
  assert.equal(approved.approvedRoot, rootsStore.canonicalizeDir(root))
})

test('open-path：声明根不覆盖目标时不弹授权框（防止借无关根提权）', async t => {
  const root = tempProject(t)
  const other = tempDir(t, 'chiron-other-')
  const target = path.join(other, 'notes.md')
  fs.writeFileSync(target, 'x', 'utf8')
  let asked = 0
  const verdict = await guard.authorizeOpenPath({
    target,
    root,
    roots: [],
    confirmRoot: async () => { asked += 1; return true },
  })
  assert.equal(verdict.ok, false)
  assert.equal(verdict.reason, 'outside-approved-root')
  assert.equal(asked, 0)
})

test('open-path：盘根不能被当成声明根（批准 C:\\ 等于整机放行）', async t => {
  const driveRoot = path.parse(process.cwd()).root
  const screened = rootsStore.screenRootCandidate(driveRoot)
  assert.equal(screened.ok, false)
  assert.equal(screened.reason, 'root-too-broad')
})

test('open-path：symlink/junction 指向根外时按 realpath 判为根外', async t => {
  const root = tempProject(t)
  const outside = tempDir(t, 'chiron-escape-')
  const secret = path.join(outside, 'secret.pdf')
  fs.writeFileSync(secret, 'pdf', 'utf8')
  const link = path.join(root, 'escape')
  try {
    fs.symlinkSync(outside, link, 'junction')
  } catch (err) {
    t.skip(`该环境不支持创建 junction：${err.code || err.message}`)
    return
  }
  const verdict = await guard.authorizeOpenPath({
    target: path.join(link, 'secret.pdf'),
    roots: [rootsStore.canonicalizeDir(root)],
    confirmRoot: noConfirm,
  })
  assert.equal(verdict.ok, false, '经 junction 逃出根的路径必须被拒')
  assert.equal(verdict.reason, 'outside-approved-root')
})

// ── 批准名单本身 ───────────────────────────────────────────────────────────

test('approved-roots：批准后 covers 生效，撤销后失效，且落盘可重读', t => {
  const stateDir = tempDir(t, 'chiron-roots-state-')
  const project = tempProject(t)
  const store = rootsStore.createApprovedRoots({ dir: stateDir })
  assert.deepEqual(store.list(), [])

  const granted = store.grant(project)
  assert.equal(granted, rootsStore.canonicalizeDir(project))
  assert.equal(store.covers(path.join(project, 'notes.md')), true)
  assert.equal(store.covers(path.dirname(project)), false)

  // 重新打开：批准名单来自磁盘
  const reopened = rootsStore.createApprovedRoots({ dir: stateDir })
  assert.deepEqual(reopened.list(), [granted])

  assert.equal(reopened.revoke(project), true)
  assert.deepEqual(reopened.list(), [])
})

test('approved-roots：拒绝相对路径/不存在的目录/UNC', t => {
  const stateDir = tempDir(t, 'chiron-roots-state-')
  const store = rootsStore.createApprovedRoots({ dir: stateDir })
  for (const bad of ['relative/dir', path.join(os.tmpdir(), `chiron-nope-${Date.now()}`), '\\\\server\\share']) {
    assert.throws(() => store.grant(bad), /项目根/)
  }
  assert.deepEqual(store.list(), [])
})

test('main.cjs：open-path 走 guard、声明根与批准名单都由主进程持有', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'main.cjs'), 'utf8')
  assert.match(source, /openPathGuard\.authorizeOpenPath\(/, 'open-path 必须走授权判定')
  assert.match(source, /confirmExecutable: async info => confirmExecutableOpen\(info\)/,
    '可执行确认必须是主进程自己的对话框')
  assert.match(source, /checkboxChecked/, '可执行确认要有独立的勾选动作')
  assert.match(source, /createApprovedRoots\(\{[\s\S]{0,120}dir: app\.getPath\('userData'\)/,
    '批准名单必须落在 userData 下')
})
