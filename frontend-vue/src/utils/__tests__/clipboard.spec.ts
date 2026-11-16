/**
 * 复制降级链的单测。
 *
 * 这条链承载的是"桌面端所有复制按钮"—— 它一旦回归，现象不是报错而是**静默无反应**
 * （调用点普遍只 catch 后弹一句"复制失败"，甚至什么都不做）。所以这里把三级通道
 * 的**顺序**与**短路条件**都钉住：标准通道成功时不该再碰 IPC，标准通道失败要能
 * 真的降级，三级全挂才返回 false。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText, copyTextOrThrow } from '../clipboard'

/** 替换/移除标准通道（`navigator.clipboard.writeText`） */
function setStandardClipboard(impl: ((value: string) => Promise<void>) | null): void {
  if (impl === null) {
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    return
  }
  // 用普通函数包一层：`navigator.clipboard.writeText` 在真实环境里是方法，
  // 直接赋 vi.fn() 也能用，但包一层就不依赖 `this` 绑定
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText: (value: string) => impl(value) },
    configurable: true,
  })
}

/** 替换/移除桌面壳的主进程通道 */
function setDesktopCopy(impl: ((value: string) => Promise<unknown>) | null): void {
  const host = globalThis as { chironDesktop?: { copyText?: unknown } }
  if (impl === null) {
    delete host.chironDesktop
    return
  }
  host.chironDesktop = { copyText: impl }
}

/** 替换/移除 `document.execCommand` 兜底 */
function setExecCommand(impl: ((command: string) => boolean) | null): void {
  const doc = document as unknown as { execCommand?: unknown }
  if (impl === null) {
    delete doc.execCommand
    return
  }
  doc.execCommand = impl
}

afterEach(() => {
  setStandardClipboard(null)
  setDesktopCopy(null)
  setExecCommand(null)
  vi.restoreAllMocks()
})

describe('copyText', () => {
  it('标准通道可用时直接沿用，不再走 IPC', async () => {
    const standard = vi.fn(async () => {})
    const desktop = vi.fn(async () => ({ ok: true }))
    setStandardClipboard(standard)
    setDesktopCopy(desktop)

    await expect(copyText('hello')).resolves.toBe(true)
    expect(standard).toHaveBeenCalledWith('hello')
    expect(desktop).not.toHaveBeenCalled()
  })

  it('标准通道抛错（文档失焦）时降级到主进程通道', async () => {
    // 这是桌面端最常见的失败：`NotAllowedError: Document is not focused`
    setStandardClipboard(async () => {
      throw new Error('NotAllowedError: Document is not focused')
    })
    const desktop = vi.fn(async () => ({ ok: true }))
    setDesktopCopy(desktop)

    await expect(copyText('hello')).resolves.toBe(true)
    expect(desktop).toHaveBeenCalledWith('hello')
  })

  it('主进程通道返回 ok:false 时继续降级到 execCommand', async () => {
    setStandardClipboard(async () => {
      throw new Error('denied')
    })
    setDesktopCopy(async () => ({ ok: false }))
    const exec = vi.fn(() => true)
    setExecCommand(exec)

    await expect(copyText('hello')).resolves.toBe(true)
    expect(exec).toHaveBeenCalledWith('copy')
  })

  it('主进程通道自身抛错时不冒泡，仍走 execCommand', async () => {
    setStandardClipboard(null)
    setDesktopCopy(async () => {
      throw new Error('ipc exploded')
    })
    setExecCommand(() => true)

    await expect(copyText('hello')).resolves.toBe(true)
  })

  it('老 preload 没有 copyText 时直接走 execCommand', async () => {
    setStandardClipboard(null)
    // chironDesktop 存在但缺 copyText：不能因此抛 TypeError
    ;(globalThis as { chironDesktop?: unknown }).chironDesktop = {}
    setExecCommand(() => true)

    await expect(copyText('hello')).resolves.toBe(true)
  })

  it('三条通道全失败时返回 false', async () => {
    setStandardClipboard(null)
    setDesktopCopy(null)
    setExecCommand(null)

    await expect(copyText('hello')).resolves.toBe(false)
  })

  it('execCommand 返回 false 时也按失败处理', async () => {
    setStandardClipboard(null)
    setDesktopCopy(null)
    setExecCommand(() => false)

    await expect(copyText('hello')).resolves.toBe(false)
  })

  it('兜底用的 textarea 会被清理，不留 DOM 残留', async () => {
    setStandardClipboard(null)
    setDesktopCopy(null)
    setExecCommand(() => true)

    const before = document.body.childElementCount
    await copyText('hello')
    expect(document.body.childElementCount).toBe(before)
  })

  it('非字符串入参统一转成字符串再投递', async () => {
    const desktop = vi.fn(async () => ({ ok: true }))
    setStandardClipboard(null)
    setDesktopCopy(desktop)

    await copyText(null as unknown as string)
    expect(desktop).toHaveBeenCalledWith('')
  })
})

describe('copyTextOrThrow', () => {
  it('成功时不抛错', async () => {
    setStandardClipboard(async () => {})
    await expect(copyTextOrThrow('hello')).resolves.toBeUndefined()
  })

  it('失败时抛错，供调用点沿用原来的 try/catch 反馈', async () => {
    setStandardClipboard(null)
    setDesktopCopy(null)
    setExecCommand(() => false)

    await expect(copyTextOrThrow('hello')).rejects.toThrow('copy failed')
  })
})
