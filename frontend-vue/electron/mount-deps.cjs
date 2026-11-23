'use strict'
/**
 * 团队模式挂载依赖（rclone / WinFsp）—— 服务端按需管理。
 *
 * 不随安装包分发；首次启用挂载时由管理员触发：
 *   - rclone：固定版本 zip → 下载/离线放置 → SHA256 校验 → 解压到 <userData>/deps
 *   - WinFsp：固定版本 msi → 下载/离线放置 → SHA256 校验 → shell.openPath（UAC 由系统弹）
 *
 * 约束：
 *   - 版本与 SHA256 钉死，取不到校验值/校验失败一律拒绝；
 *   - 不静默安装驱动；WinFsp 必须用户确认 UAC；
 *   - 只允许 win32-x64；其它平台返回不适用。
 */

const fs = require('node:fs')
const fsp = fs.promises
const path = require('node:path')
const crypto = require('node:crypto')
const { execFile } = require('node:child_process')

const MANIFEST = Object.freeze({
  rclone: {
    version: '1.76.0',
    asset: 'rclone-v1.76.0-windows-amd64.zip',
    sha256: '8b4fd2737ade4e70c057ef4a49deb7cffe94b0fb9d7587c189d149823f21a1dc',
    size: 32193987,
    url: 'https://github.com/rclone/rclone/releases/download/v1.76.0/rclone-v1.76.0-windows-amd64.zip',
    kind: 'zip',
    exe: 'rclone.exe',
  },
  winfsp: {
    version: '2.2.26323',
    asset: 'winfsp-2.2.26323.msi',
    sha256: '36ce01f9eedbbe6842b7fb4bedc79d7d4f71983e8aceb6877b46a07b331c7e19',
    size: 2220032,
    url: 'https://github.com/winfsp/winfsp/releases/download/v2.2/winfsp-2.2.26323.msi',
    kind: 'msi',
  },
})

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const h = crypto.createHash('sha256')
    const s = fs.createReadStream(file)
    s.on('data', c => h.update(c))
    s.on('error', reject)
    s.on('end', () => resolve(h.digest('hex')))
  })
}

function run(cmd, args, timeoutMs = 120000) {
  return new Promise(resolve => {
    execFile(cmd, args, { windowsHide: true, timeout: timeoutMs }, (err, stdout, stderr) => {
      resolve({ ok: !err, stdout: String(stdout || ''), stderr: String(stderr || ''), error: err })
    })
  })
}

function winfspInstalledVersion() {
  // WinFsp 的卸载项在 WOW6432Node（x64 安装器也写这里）；读不到就按未安装。
  return run('reg', ['query', 'HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\WinFsp', '/v', 'DisplayVersion'], 5000)
    .then(r => {
      if (!r.ok) return ''
      const m = r.stdout.match(/DisplayVersion\s+REG_\w+\s+([^\r\n]+)/i)
      return m ? m[1].trim() : ''
    })
}

function createMountDeps({ app, net, shell, log = () => {}, platform = process.platform, arch = process.arch }) {
  const busy = new Set()

  function depsRoot() {
    return path.join(app.getPath('userData'), 'deps')
  }

  function targetDir(name) {
    return path.join(depsRoot(), name, MANIFEST[name].version)
  }

  async function status() {
    const out = {
      platform,
      arch,
      supported: platform === 'win32' && arch === 'x64',
      depsRoot: depsRoot(),
      rclone: { installed: false, version: MANIFEST.rclone.version, path: '' },
      winfsp: { installed: false, version: MANIFEST.winfsp.version },
    }
    if (!out.supported) return out
    const exe = path.join(targetDir('rclone'), MANIFEST.rclone.exe)
    try {
      const st = await fsp.stat(exe)
      out.rclone.installed = st.isFile() && st.size > 0
      if (out.rclone.installed) out.rclone.path = exe
    } catch {
      /* 未安装 */
    }
    const ver = await winfspInstalledVersion()
    out.winfsp.installed = Boolean(ver)
    if (ver) out.winfsp.installedVersion = ver
    return out
  }

  async function downloadVerified(dep, dest) {
    await fsp.mkdir(path.dirname(dest), { recursive: true })
    if (fs.existsSync(dest)) {
      const h = await sha256File(dest)
      if (h === dep.sha256) return dest
      log('warn', `offline asset checksum mismatch for ${dep.asset}; re-downloading`)
      await fsp.unlink(dest).catch(() => {})
    }
    log('info', `downloading ${dep.url}`)
    const res = await net.fetch(dep.url, { redirect: 'follow' })
    if (!res.ok) throw new Error(`download ${dep.asset} failed: HTTP ${res.status}`)
    const buf = Buffer.from(await res.arrayBuffer())
    const actual = crypto.createHash('sha256').update(buf).digest('hex')
    if (actual !== dep.sha256) {
      throw new Error(`checksum mismatch for ${dep.asset}: expected ${dep.sha256}, got ${actual}`)
    }
    await fsp.writeFile(dest, buf)
    return dest
  }

  async function installRclone() {
    const dep = MANIFEST.rclone
    const dir = targetDir('rclone')
    const exe = path.join(dir, dep.exe)
    if (fs.existsSync(exe)) return { ok: true, already: true, path: exe }

    const staging = path.join(depsRoot(), 'rclone', '.staging')
    const zip = path.join(staging, dep.asset)
    const unpack = path.join(staging, 'unpacked')
    await fsp.rm(staging, { recursive: true, force: true })
    await fsp.mkdir(staging, { recursive: true })
    await downloadVerified(dep, zip)

    // Windows 自带 bsdtar，可解 zip；失败再退 PowerShell Expand-Archive。
    let r = await run('tar', ['-xf', zip, '-C', unpack])
    if (!r.ok) {
      await fsp.mkdir(unpack, { recursive: true })
      r = await run('powershell', ['-NoProfile', '-Command', `Expand-Archive -LiteralPath '${zip.replace(/'/g, "''")}' -DestinationPath '${unpack.replace(/'/g, "''")}' -Force`])
      if (!r.ok) throw new Error(`extract failed: ${r.stderr || r.error?.message || 'unknown'}`)
    }
    // zip 内有一层 rclone-v...-windows-amd64/ 目录，找到 rclone.exe 所在目录。
    const found = findFile(unpack, dep.exe)
    if (!found) throw new Error('rclone.exe not found in archive')
    await fsp.mkdir(dir, { recursive: true })
    await fsp.cp(path.dirname(found), dir, { recursive: true })
    await fsp.rm(staging, { recursive: true, force: true }).catch(() => {})
    return { ok: true, path: path.join(dir, dep.exe) }
  }

  async function installWinFsp() {
    const dep = MANIFEST.winfsp
    const dest = path.join(targetDir('winfsp'), dep.asset)
    await downloadVerified(dep, dest)
    const failure = await shell.openPath(dest)
    if (failure) return { ok: false, reason: 'launch', message: failure, path: dest }
    return { ok: true, launched: true, path: dest }
  }

  function findFile(root, name) {
    const stack = [root]
    while (stack.length) {
      const dir = stack.pop()
      let entries = []
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true })
      } catch {
        continue
      }
      for (const e of entries) {
        const p = path.join(dir, e.name)
        if (e.isDirectory()) stack.push(p)
        else if (e.name.toLowerCase() === name.toLowerCase()) return p
      }
    }
    return ''
  }

  async function install(name) {
    if (platform !== 'win32' || arch !== 'x64') {
      return { ok: false, reason: 'unsupported-platform', message: `${platform}-${arch} is not supported` }
    }
    if (!MANIFEST[name]) return { ok: false, reason: 'unknown-dependency' }
    if (busy.has(name)) return { ok: false, reason: 'busy' }
    busy.add(name)
    try {
      if (name === 'rclone') return await installRclone()
      if (name === 'winfsp') return await installWinFsp()
      return { ok: false, reason: 'unknown-dependency' }
    } catch (err) {
      log('warn', `install ${name} failed: ${err.message}`)
      return { ok: false, reason: 'failed', message: err.message }
    } finally {
      busy.delete(name)
    }
  }

  return { status, install, manifest: MANIFEST }
}

module.exports = { createMountDeps, MANIFEST }
