import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../index'
import {
  MAX_SPEC_LEN,
  PACKAGE_NAME_RE,
  PIP_SPEC_RE,
  getDepJob,
  installDep,
  isNewerVersion,
  isTerminalJob,
  listDeps,
  searchPypi,
  uninstallDep,
  validatePackageName,
  validatePipSpec,
} from '../deps'

/**
 * 依赖管理页的**校验函数**是这一页的安全边界：安装规格最终会变成 pip 命令，
 * 前端这一层必须与后端逐字一致（python-engine/app/api/deps.py 与
 * internal/api/deps_admin.go 的同名正则）。
 *
 * 这里用**同一组用例**锁住"前端认为合法 ⇔ 后端认为合法"里最容易漂的那几条：
 * 注入形态（分号/反引号/`&&`）、pip 的路径与选项语义（`.env` / `-r`）、extras 与 URL。
 */
describe('validatePipSpec', () => {
  it.each([
    'requests',
    'requests==2.31.0',
    'requests>=2.31',
    'requests<=2.0',
    'requests!=1.0',
    'requests~=2.0',
    'requests>2',
    'requests<3',
    'requests==2.*',
    'Pillow',
    'my-pkg_1.0',
    'zope.interface==5.0.0',
  ])('接受 %s', spec => {
    expect(validatePipSpec(spec)).toBeNull()
  })

  it.each([
    ['分号注入', 'requests; rm -rf /'],
    ['命令拼接', 'requests && curl evil'],
    ['管道', 'requests|whoami'],
    ['反引号', 'requests`id`'],
    ['追加选项', 'requests==2.0 --index-url http://evil/simple'],
    ['extras', 'requests[security]'],
    ['VCS URL', 'git+https://evil/x.git'],
    ['直链 URL', 'https://evil/x.whl'],
    ['-r 读文件', '-r requirements.txt'],
    ['-e 可编辑', '-e .'],
    ['点文件（pip 当本地目录）', '.env'],
    ['相对路径', '../escape'],
    ['子路径', 'pkg/sub'],
    ['反斜杠路径', 'pkg\\sub'],
    ['双约束', 'pkg==1.0,<2'],
    ['版本前空格', 'pkg >=2'],
    ['空版本', 'pkg=='],
    ['换行注入', 'pkg==2.0\nother'],
    ['结尾连字符', 'pkg-'],
  ])('拒绝 %s', (_label, spec) => {
    expect(validatePipSpec(spec)).toBe('invalid')
  })

  it('空值与纯空白返回 required（而不是 invalid）', () => {
    expect(validatePipSpec('')).toBe('required')
    expect(validatePipSpec('   ')).toBe('required')
  })

  it('超长返回 too_long', () => {
    expect(validatePipSpec('a'.repeat(MAX_SPEC_LEN + 1))).toBe('too_long')
  })

  it('两侧空白会被裁剪后判定', () => {
    expect(validatePipSpec('  requests==2.31.0  ')).toBeNull()
  })
})

describe('validatePackageName', () => {
  it.each(['requests', 'Pillow', 'my-pkg_1.0', 'zope.interface'])('接受 %s', name => {
    expect(validatePackageName(name)).toBeNull()
  })

  it.each(['requests==2.31', 'requests>=2', 'requests; id', '-r req.txt', '.env', '../x', 'a/b', 'pkg name'])(
    '拒绝 %s',
    name => {
      expect(validatePackageName(name)).toBe('invalid')
    },
  )

  it('空值返回 required', () => {
    expect(validatePackageName('')).toBe('required')
  })
})

describe('与后端同语义的正则常量', () => {
  it('PIP_SPEC_RE 与 PACKAGE_NAME_RE 的差异只在版本约束', () => {
    expect(PIP_SPEC_RE.test('requests==2.0')).toBe(true)
    expect(PACKAGE_NAME_RE.test('requests==2.0')).toBe(false)
  })

  it('名称必须以字母或数字开头结尾', () => {
    for (const bad of ['.env', '-r', 'pkg-', '.', 'pkg.']) {
      expect(PIP_SPEC_RE.test(bad)).toBe(false)
    }
  })
})

describe('isNewerVersion', () => {
  it('按点分数字比较', () => {
    expect(isNewerVersion('2.32.3', '2.31.0')).toBe(true)
    expect(isNewerVersion('2.31.0', '2.32.3')).toBe(false)
    expect(isNewerVersion('2.31', '2.31.0')).toBe(false)
    expect(isNewerVersion('2.31.1', '2.31')).toBe(true)
  })

  it('相同或缺失版本不算更新（否则会一直提示可升级）', () => {
    expect(isNewerVersion('1.0.0', '1.0.0')).toBe(false)
    expect(isNewerVersion('', '1.0.0')).toBe(false)
    expect(isNewerVersion('1.0.0', '')).toBe(false)
  })

  it('非纯数字段退化为字符串不等比较（宁可多提示，不误导为最新）', () => {
    expect(isNewerVersion('1.0.0b1', '1.0.0')).toBe(true)
    expect(isNewerVersion('1.0.0', '1.0.0b1')).toBe(true)
  })
})

describe('isTerminalJob', () => {
  it('只有 succeeded / failed 是终态', () => {
    expect(isTerminalJob('succeeded')).toBe(true)
    expect(isTerminalJob('failed')).toBe(true)
    expect(isTerminalJob('running')).toBe(false)
    expect(isTerminalJob('queued')).toBe(false)
  })
})

describe('HTTP 包装解析', () => {
  afterEach(() => vi.restoreAllMocks())

  it('listDeps 解析网关 {success, data} 包装', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({
      data: {
        success: true,
        data: { python: '3.12.0', executable: '/venv/bin/python', packages: [{ name: 'fastapi', version: '0.115.0' }], count: 1 },
      },
    } as never)

    const result = await listDeps()
    expect(result.count).toBe(1)
    expect(result.packages[0]!.name).toBe('fastapi')
  })

  it('listDeps 对缺字段的响应做兜底（不让模板拿到 undefined）', async () => {
    vi.spyOn(api, 'get').mockResolvedValue({ data: { success: true, data: {} } } as never)

    const result = await listDeps()
    expect(result).toEqual({ python: '', executable: '', packages: [], count: 0 })
  })

  it('searchPypi 把 q 作为查询参数发出并解析 results', async () => {
    const get = vi.spyOn(api, 'get').mockResolvedValue({
      data: { success: true, data: { query: 'requests', results: [{ name: 'requests', version: '2.32.3', summary: 'HTTP' }], count: 1, found: true } },
    } as never)

    const result = await searchPypi('requests')
    expect(get).toHaveBeenCalledWith('/v1/admin/deps/search', { params: { q: 'requests' } })
    expect(result.results).toHaveLength(1)
    expect(result.found).toBe(true)
  })

  it('installDep 返回 job（缺 job 时抛错而不是静默）', async () => {
    vi.spyOn(api, 'post').mockResolvedValue({
      data: { success: true, data: { job: { id: 'j1', kind: 'install', target: 'requests', status: 'queued', message: '', progress: 0, output: '', created_at: 0 } } },
    } as never)
    expect((await installDep('requests')).id).toBe('j1')

    vi.spyOn(api, 'post').mockResolvedValue({ data: { success: true, data: {} } } as never)
    await expect(installDep('requests')).rejects.toThrow()
  })

  it('uninstallDep / getDepJob 走对应路径', async () => {
    const post = vi.spyOn(api, 'post').mockResolvedValue({
      data: { success: true, data: { ok: true, name: 'requests', output: '' } },
    } as never)
    expect((await uninstallDep('requests')).ok).toBe(true)
    expect(post).toHaveBeenCalledWith('/v1/admin/deps/uninstall', { name: 'requests' })

    const get = vi.spyOn(api, 'get').mockResolvedValue({
      data: { success: true, data: { id: 'j1', status: 'succeeded' } },
    } as never)
    expect((await getDepJob('j1')).status).toBe('succeeded')
    expect(get).toHaveBeenCalledWith('/v1/admin/deps/jobs/j1')
  })
})
