import { describe, expect, it, vi } from 'vitest'

import {
  collectDroppedEntries,
  dirKey,
  dragHasFiles,
  ensureDropFolders,
  folderIdOf,
  isIgnoredDropDir,
  isIgnoredDropName,
  MAX_DROP_DEPTH,
  planDrop,
  planFolderCreation,
  runWithConcurrency,
  type DroppedEntry,
  type ExistingFolder,
} from '../dropUpload'

/**
 * 拖拽上传的目录展开。
 *
 * 这些用例存在的理由：这里所有错误都是**静默**的 —— 少读了 100 条之后的文件、
 * 目录层级建错、父目录没建就建子目录，全都不会抛异常，只会让"传进来的东西
 * 比拖进去的少"。而用户唯一的判断依据就是媒体库里看到的那几个条目，
 * 所以边界必须在这里钉死。
 */

const file = (name: string) => new File(['x'], name)

const entry = (name: string, dirs: string[]): DroppedEntry => ({
  file: file(name),
  dirs,
})

describe('系统噪声文件', () => {
  it('忽略 .DS_Store / Thumbs.db / desktop.ini（大小写不敏感）', () => {
    expect(isIgnoredDropName('.DS_Store')).toBe(true)
    expect(isIgnoredDropName('Thumbs.db')).toBe(true)
    expect(isIgnoredDropName('desktop.INI')).toBe(true)
    expect(isIgnoredDropName('report.pdf')).toBe(false)
  })

  it('忽略 __MACOSX 整棵子树', () => {
    expect(isIgnoredDropDir('__MACOSX')).toBe(true)
    expect(isIgnoredDropDir('src')).toBe(false)
  })
})

describe('planFolderCreation（父目录先建）', () => {
  it('深路径展开成完整目录链，且父目录排在子目录之前', () => {
    const dirs = planFolderCreation([entry('x.png', ['a', 'b'])])

    // 只建 `a/b` 会让后端保证不了父目录存在 —— ensureFolderChain 直接 400
    expect(dirs).toEqual([['a'], ['a', 'b']])
  })

  it('同层同级目录去重，且顺序可预测', () => {
    const dirs = planFolderCreation([
      entry('x.png', ['a', 'b']),
      entry('y.png', ['a', 'c']),
      entry('z.png', ['a', 'b']),
    ])

    expect(dirs).toEqual([['a'], ['a', 'b'], ['a', 'c']])
  })

  it('落在当层的文件不产生任何目录', () => {
    expect(planFolderCreation([entry('x.png', [])])).toEqual([])
  })
})

describe('planDrop（深度上限）', () => {
  it('超过 16 层（含当前所在层级）的文件被剔除并计数', () => {
    // 后端 `splitAssetPath` 的判据是 `len(folders) > 16`，所以恰好 16 段是**合法**的，
    // 第 17 段才 400。用例必须踩在 17 上，否则断言会与实现口径错位。
    const atLimit = Array.from({ length: MAX_DROP_DEPTH }, (_, i) => `d${i}`)
    const overLimit = Array.from({ length: MAX_DROP_DEPTH + 1 }, (_, i) => `d${i}`)
    const { entries, droppedDeep, folders } = planDrop([
      entry('ok.png', ['a']),
      entry('at-limit.png', atLimit),
      entry('too-deep.png', overLimit),
    ])

    expect(entries.map((e) => e.file.name)).toEqual(['ok.png', 'at-limit.png'])
    expect(droppedDeep).toBe(1)
    // `at-limit.png` 也参与目录规划：16 段目录链本身合法，只是不该出现第 17 段
    expect(folders).toHaveLength(MAX_DROP_DEPTH + 1)
    expect(folders[0]).toEqual(['a'])
    expect(folders[folders.length - 1]).toEqual(atLimit)
  })

  it('当前已在第 15 层时，连一层子目录都不该建', () => {
    const { entries, droppedDeep } = planDrop([entry('x.png', ['a'])], 15)

    // 15 + 1 = 16 仍在允许范围内 —— 边界值必须能过
    expect(droppedDeep).toBe(0)
    expect(entries).toHaveLength(1)

    const at = planDrop([entry('x.png', ['a', 'b'])], 15)
    // 15 + 2 = 17 > 16
    expect(at.droppedDeep).toBe(1)
  })
})

describe('folderIdOf（逐段匹配已有目录）', () => {
  const existing: ExistingFolder[] = [
    { id: 'a', name: 'a', parent_id: '' },
    { id: 'ab', name: 'b', parent_id: 'a' },
    // 根下另有一个同名 b：不区分起点就会串到它身上
    { id: 'b-root', name: 'b', parent_id: '' },
  ]

  it('按段走到叶子', () => {
    expect(folderIdOf(existing, ['a', 'b'])).toBe('ab')
  })

  it('起点是当前所在目录，不会命中根下的同名目录', () => {
    // 站在 a 里面拖入 b/
    expect(folderIdOf(existing, ['b'], 'a')).toBe('ab')
    // 站在根里拖入 b/
    expect(folderIdOf(existing, ['b'], '')).toBe('b-root')
  })

  it('路径不存在返回 null', () => {
    expect(folderIdOf(existing, ['a', 'zzz'])).toBeNull()
  })

  it('空目录链返回起点自身（无子目录要建）', () => {
    expect(folderIdOf(existing, [], 'a')).toBe('a')
    expect(folderIdOf(existing, [])).toBeNull()
  })
})

describe('ensureDropFolders（建目录编排）', () => {
  it('已存在的目录直接复用，不重复 POST', async () => {
    const create = vi.fn(async () => null)
    const res = await ensureDropFolders(
      [['a']],
      [{ id: 'a1', name: 'a', parent_id: '' }],
      create,
    )

    expect(create).not.toHaveBeenCalled()
    expect(res.ids.get('a')).toBe('a1')
    expect(res.failed).toEqual([])
  })

  it('父目录失败时子孙不再发请求（避免一屏重复报错）', async () => {
    const create = vi.fn(async (name: string) => {
      if (name === 'a') return null // 父失败
      return { id: `id-${name}`, name, parent_id: 'x' }
    })

    const res = await ensureDropFolders([['a'], ['a', 'b'], ['a', 'b', 'c']], [], create)

    // 只尝试了 a 一次；b、c 因为父不存在被跳过，但都记进 failed
    expect(create).toHaveBeenCalledTimes(1)
    expect(res.failed).toEqual(['a', 'a/b', 'a/b/c'])
    expect(res.ids.size).toBe(0)
  })

  it('建好的目录 id 会被后续子目录当作 parent 使用', async () => {
    const calls: Array<[string, string]> = []
    const create = async (name: string, parentId: string) => {
      calls.push([name, parentId])
      return { id: `id-${name}`, name, parent_id: parentId }
    }

    const res = await ensureDropFolders([['a'], ['a', 'b']], [], create)

    expect(calls).toEqual([['a', ''], ['b', 'id-a']])
    expect(res.ids.get('a/b')).toBe('id-b')
    expect(res.failed).toEqual([])
  })

  it('从子目录起拖时，顶层目录挂在 baseParentId 下', async () => {
    const calls: Array<[string, string]> = []
    const create = async (name: string, parentId: string) => {
      calls.push([name, parentId])
      return { id: `id-${name}`, name, parent_id: parentId }
    }

    await ensureDropFolders([['report']], [], create, 'current')

    expect(calls).toEqual([['report', 'current']])
  })

  it('起点目录下已有同名目录时复用，不会串到根下的同名目录', async () => {
    // 站在 `a` 里拖入 `b/`：库中 `a/b` 与根下 `b` 都存在。
    // 复用判定若不带上 baseParentId，会命中根下的 `b-root`，文件被传到 a 之外。
    const existing: ExistingFolder[] = [
      { id: 'a', name: 'a', parent_id: '' },
      { id: 'ab', name: 'b', parent_id: 'a' },
      { id: 'b-root', name: 'b', parent_id: '' },
    ]
    const create = vi.fn(async () => null)

    const res = await ensureDropFolders([['b']], existing, create, 'a')

    expect(create).not.toHaveBeenCalled()
    expect(res.ids.get('b')).toBe('ab')
    expect(res.failed).toEqual([])
  })

  it('单个目录失败不影响其他分支', async () => {
    const create = async (name: string) => (name === 'bad' ? null : {
      id: `id-${name}`,
      name,
      parent_id: '',
    })

    const res = await ensureDropFolders([['bad'], ['good']], [], create)

    expect(res.failed).toEqual(['bad'])
    expect(res.ids.get('good')).toBe('id-good')
  })
})

describe('dirKey', () => {
  it('用 / 拼路径', () => {
    expect(dirKey(['a', 'b'])).toBe('a/b')
    expect(dirKey([])).toBe('')
  })
})

describe('runWithConcurrency', () => {
  it('结果按输入顺序返回，且不超过并发上限', async () => {
    let active = 0
    let peak = 0
    const items = Array.from({ length: 10 }, (_, i) => i)

    const results = await runWithConcurrency(items, 3, async (n) => {
      active++
      peak = Math.max(peak, active)
      // 让出事件循环，保证并发真的被用上
      await new Promise((r) => setTimeout(r, 1))
      active--
      return n * 2
    })

    expect(peak).toBeLessThanOrEqual(3)
    expect(results.map((r) => (r.ok ? r.value : null))).toEqual(items.map((n) => n * 2))
  })

  it('单个任务失败不中断整批，并标记那一项', async () => {
    const results = await runWithConcurrency([1, 2, 3], 2, async (n) => {
      if (n === 2) throw new Error('boom')
      return n
    })

    expect(results[0]).toEqual({ ok: true, value: 1 })
    expect(results[1].ok).toBe(false)
    expect(results[2]).toEqual({ ok: true, value: 3 })
  })
})

describe('collectDroppedEntries（真实 DataTransfer 形状）', () => {
  /** 造一个目录条目：`children` 是「每一批」的返回，用来模拟 100 条上限。 */
  function fakeDir(name: string, batches: FileSystemEntry[][]): FileSystemDirectoryEntry {
    let cursor = 0
    return {
      name,
      isDirectory: true,
      isFile: false,
      createReader: () =>
        ({
          readEntries: (cb: (entries: FileSystemEntry[]) => void) => {
            const batch = batches[cursor] ?? []
            cursor++
            // 真实实现是异步回调；这里同步回调也能覆盖"读空为止"的逻辑
            cb(batch)
          },
        }) as unknown as FileSystemDirectoryReader,
    } as unknown as FileSystemDirectoryEntry
  }

  function fakeFile(name: string, content = 'x'): FileSystemFileEntry {
    return {
      name,
      isDirectory: false,
      isFile: true,
      file: (cb: (f: File) => void) => cb(new File([content], name)),
    } as unknown as FileSystemFileEntry
  }

  /** DataTransfer 的最小形状：`items` 带 `webkitGetAsEntry`。 */
  function fakeDataTransfer(roots: FileSystemEntry[]): DataTransfer {
    return {
      items: roots.map((r) => ({ webkitGetAsEntry: () => r })) as unknown as DataTransferItemList,
      files: [] as unknown as FileList,
      types: ['Files'] as unknown as DataTransfer['types'],
    } as DataTransfer
  }

  it('★ 目录必须一直读到空数组：一次最多 100 条', async () => {
    // 第一批 100 条 + 第二批 1 条：只读一次就会静默丢掉最后那个文件
    const first = Array.from({ length: 100 }, (_, i) => fakeFile(`f${i}.txt`))
    const second = [fakeFile('tail.txt')]
    const dt = fakeDataTransfer([fakeDir('big', [first, second])])

    const entries = await collectDroppedEntries(dt)

    expect(entries).toHaveLength(101)
    expect(entries.some((e) => e.file.name === 'tail.txt')).toBe(true)
  })

  it('递归展开子目录，并记录完整目录链', async () => {
    const nested = fakeDir('inner', [[fakeFile('deep.png')]])
    const outer = fakeDir('outer', [[nested]])
    const dt = fakeDataTransfer([outer])

    const entries = await collectDroppedEntries(dt)

    expect(entries).toHaveLength(1)
    expect(entries[0].dirs).toEqual(['outer', 'inner'])
  })

  it('系统噪声文件与 __MACOSX 被过滤', async () => {
    const dir = fakeDir('proj', [[
      fakeFile('.DS_Store'),
      fakeFile('real.png'),
      fakeDir('__MACOSX', [[fakeFile('._real.png')]]),
    ]])

    const entries = await collectDroppedEntries(fakeDataTransfer([dir]))

    expect(entries.map((e) => e.file.name)).toEqual(['real.png'])
  })

  it('浏览器不给 entry 时退化为 dataTransfer.files', async () => {
    const dt = {
      items: [] as unknown as DataTransferItemList,
      files: [new File(['x'], 'a.png'), new File(['x'], '.DS_Store')] as unknown as FileList,
      types: ['Files'] as unknown as DataTransfer['types'],
    } as DataTransfer

    const entries = await collectDroppedEntries(dt)

    expect(entries.map((e) => e.file.name)).toEqual(['a.png'])
    expect(entries[0].dirs).toEqual([])
  })

  it('直接拖单个文件时落在当层', async () => {
    const entries = await collectDroppedEntries(fakeDataTransfer([fakeFile('a.png')]))

    expect(entries).toEqual([{ file: expect.any(File), dirs: [] }])
  })
})

describe('dragHasFiles', () => {
  it('只有带 Files 类型才算拖文件', () => {
    expect(dragHasFiles({ types: ['Files'] } as unknown as DataTransfer)).toBe(true)
    expect(dragHasFiles({ types: ['text/plain'] } as unknown as DataTransfer)).toBe(false)
    expect(dragHasFiles(null)).toBe(false)
  })
})
