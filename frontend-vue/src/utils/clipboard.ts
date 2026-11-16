/**
 * 复制文本 —— 全站**唯一**的复制入口。
 *
 * ## 为什么不能直接用 `navigator.clipboard.writeText()`
 *
 * 桌面端（`file://` 页面）里它有三道闸门，任何一道没开都是**静默失败**：
 *   1. Chromium 权限：`navigator.clipboard.writeText()` 申请的是
 *      `clipboard-sanitized-write`（不是 `clipboard-write`），主进程的
 *      `setPermissionCheckHandler` 不在白名单里就直接判 false；
 *   2. 焦点：文档没有焦点时抛 `NotAllowedError: Document is not focused` ——
 *      点按钮前刚切过窗口、或用键盘触发的路径都会撞上；
 *   3. 安全上下文：非安全来源（局域网 http 访问）里 `navigator.clipboard`
 *      干脆不存在。
 *
 * 而调用点此前普遍写成 `.then()` 且不接 catch，于是"点了没反应"没有任何线索 ——
 * 用户只能得出"所有复制功能都坏了"。
 *
 * ## 三级降级
 *
 *   1. `navigator.clipboard.writeText` —— 标准通道，网页版正常走这里；
 *   2. `window.chironDesktop.copyText` —— 桌面壳的主进程通道
 *      （Electron 的 `clipboard` 模块），**不经**页面权限与焦点检查；
 *   3. `document.execCommand('copy')` —— 最后兜底，虽已废弃但同步可用，
 *      在极老的壳与受限环境里仍有效。
 *
 * 返回布尔而不是抛错：绝大多数调用点只关心"成没成"，要抛错的用
 * `copyTextOrThrow()`（保持调用点原有的 try/catch 语义）。
 */

/** 桌面壳是否暴露了主进程复制通道（老 preload 没有） */
function desktopCopy(text: string): Promise<boolean> | null {
  const host = (globalThis as { chironDesktop?: { copyText?: (v: string) => Promise<{ ok?: boolean }> } })
    .chironDesktop
  if (!host || typeof host.copyText !== 'function') return null
  return host
    .copyText(text)
    .then(result => result?.ok === true)
    .catch(() => false)
}

/** `execCommand('copy')` 兜底：同步、不依赖权限，但要求文档可写 */
function legacyCopy(text: string): boolean {
  if (typeof document === 'undefined' || !document.body) return false
  try {
    const area = document.createElement('textarea')
    // 必须留在布局里才能被选中，但又要看不见：`display:none` 会让选区失效，
    // 所以用零尺寸 + 不可交互的定位把它藏起来。
    area.value = text
    area.setAttribute('readonly', '')
    area.style.position = 'fixed'
    area.style.top = '-1000px'
    area.style.left = '-1000px'
    area.style.opacity = '0'
    document.body.appendChild(area)
    const previous = document.activeElement as HTMLElement | null
    area.select()
    const ok = document.execCommand('copy')
    document.body.removeChild(area)
    // 把焦点还给用户刚才在操作的元素：复制不该改变焦点（否则随后的键盘操作落空）
    previous?.focus?.()
    return ok
  } catch {
    return false
  }
}

/**
 * 复制文本到剪贴板。三条通道依次尝试，全部失败才返回 `false`。
 *
 * 不使用 `Promise.all` / 提前返回：顺序是刻意的，标准通道成功时不该再碰
 * 主进程（那会多一次 IPC 往返）。
 */
export async function copyText(text: string): Promise<boolean> {
  const value = String(text ?? '')

  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(value)
      return true
    }
  } catch {
    // 权限被拒 / 文档失焦 / 非安全上下文：继续降级，不在这里报错
  }

  const viaDesktop = desktopCopy(value)
  if (viaDesktop && (await viaDesktop)) return true

  return legacyCopy(value)
}

/**
 * 复制文本，失败时抛错。
 *
 * 给那些**已经**有 `try { … } catch { notify.error(copy_failed) }` 的调用点用：
 * 换掉 `navigator.clipboard.writeText` 即可，不必重写它们的反馈逻辑。
 */
export async function copyTextOrThrow(text: string): Promise<void> {
  if (!(await copyText(text))) throw new Error('copy failed')
}
