'use strict'
/**
 * IPC 发送方身份判定（PRD 9.2 / H47）。
 *
 * 缺陷形态：主进程只按**页面 URL** 判断"这条 IPC 是不是我们的界面发的"。
 * 三个后果：
 *   1. 只比 URL ⇒ **任何 iframe/子框架**都能发同一批特权调用（`event.senderFrame`
 *      就是为这件事准备的，但旧实现取的是 `senderFrame?.url || getURL()`）；
 *   2. setup/error 这类**辅助页**与主界面共用同一个 preload ⇒ 它们的页面里也有
 *      token 读写、清空数据、打开任意路径等能力；
 *   3. 没有"每窗口能力表"：任何信任窗口可调用任意 privileged IPC。
 *
 * 这一层是纯函数，便于把每条判据单独钉住（真窗口仍需 Electron 才能验证）。
 */

/**
 * @typedef {object} SenderLike
 * @property {() => string} [getURL] 取 webContents 的当前 URL（测试可省）
 * @property {number} [id] webContents id（测试可省）
 */

/**
 * 校验一条 IPC 事件的来源。
 *
 * @param {object} event Electron 的 IpcMainInvokeEvent 形状（可裁剪）
 * @param {object} options
 * @param {(url: string) => boolean} options.isTrustedUrl 该 URL 是否属于本应用
 * @param {Set<number>|number[]} [options.allowedWebContents] 允许的窗口 id（省略=不校验）
 * @param {boolean} [options.requireMainFrame] 是否要求主框架（默认 true）
 * @returns {{ok: boolean, reason?: string, url: string, webContentsId: number}}
 */
function checkSender(event, options = {}) {
  const isTrustedUrl = options.isTrustedUrl
  const requireMainFrame = options.requireMainFrame !== false
  const allowed = options.allowedWebContents

  const frame = event && event.senderFrame
  const sender = event && event.sender
  const webContentsId = Number((sender && sender.id) ?? NaN)

  // 1) 必须能拿到框架信息。拿不到时**不猜**：`senderFrame` 为 null 常见于
  //    框架已销毁（导航竞态），此时放行等于给已消失的页面留一次特权调用。
  if (!frame) {
    const fallbackUrl = typeof sender?.getURL === 'function' ? sender.getURL() : ''
    return { ok: false, reason: 'no-sender-frame', url: String(fallbackUrl || ''), webContentsId }
  }

  // 2) 只允许**主框架**：iframe 继承了 preload 暴露的通道，但它不是我们的界面。
  if (requireMainFrame && frame.parent !== null && frame.parent !== undefined) {
    return { ok: false, reason: 'not-main-frame', url: String(frame.url || ''), webContentsId }
  }

  // 3) URL 必须是本应用的页面（沿用主进程既有的白名单判定）。
  const url = String(frame.url || '')
  if (typeof isTrustedUrl !== 'function' || !isTrustedUrl(url)) {
    return { ok: false, reason: 'untrusted-url', url, webContentsId }
  }

  // 4) 可选：只接受登记过的窗口（防止"别处 new BrowserWindow"拿到同一套通道）。
  if (allowed) {
    const ids = allowed instanceof Set ? allowed : new Set(allowed)
    if (ids.size > 0 && !ids.has(webContentsId)) {
      return { ok: false, reason: 'unregistered-window', url, webContentsId }
    }
  }

  return { ok: true, url, webContentsId }
}

/**
 * 每窗口能力表：哪些窗口允许调用哪些特权 IPC。
 *
 * `'*'` 表示"该窗口不需要额外限制"（仍要过 `checkSender` 的基础判定）。
 * 辅助页（setup/error）不在表里 ⇒ 一律拒绝，即使它拿到了通道。
 */
const WINDOW_CAPABILITIES = {
  // 主界面：除辅助页专属之外的**全部**特权通道（显式列出，能力面一眼可读）。
  main: new Set([
    'desktop:begin-two-factor',
    'desktop:check-update',
    'desktop:clear-token',
    'desktop:confirm-two-factor',
    'desktop:copy-text',
    'desktop:disable-two-factor',
    'desktop:get-device-bridge-status',
    'desktop:get-device-id',
    'desktop:get-engine-env-settings',
    'desktop:get-hello-availability',
    'desktop:get-model-config',
    'desktop:get-os-account',
    'desktop:get-token',
    'desktop:get-two-factor-status',
    'desktop:grant-project-root',
    'desktop:install-mount-dep',
    'desktop:install-update',
    'desktop:mount-deps-status',
    'desktop:open-path',
    'desktop:open-setup',
    'desktop:pick-directory',
    'desktop:pick-document',
    'desktop:project-roots',
    'desktop:regenerate-recovery-codes',
    'desktop:reset-two-factor',
    'desktop:restart-component',
    'desktop:restart-local-backend',
    'desktop:revoke-project-root',
    'desktop:save-engine-env-settings',
    'desktop:save-model-config',
    'desktop:save-token',
    'desktop:update-status',
    'desktop:verify-lock-password',
    'desktop:wipe-local-data',
  ]),
  // 辅助页（setup/error）：只够"配地址 / 切模式 / 自检 / 重启"。
  setup: new Set([
    'desktop:get-app-info',
    'desktop:get-device-settings',
    'desktop:restart',
    'desktop:set-api-url',
    'desktop:set-mode',
    'desktop:test-api-url',
  ]),
  // error 页比 setup 更窄：只要版本信息与重启（它没有输入框）。
  error: new Set(['desktop:get-app-info', 'desktop:restart']),
  // 迷你输入框（F10）：只够提交一段文本 / 关掉自己。
  // 刻意不给 `get-token` / `save-token` 等任何一条 —— 它只放一个 textarea，
  // 没有理由持有凭据或打开路径。
  quickAsk: new Set(['desktop:quick-ask-submit', 'desktop:quick-ask-cancel']),
}

/**
 * 判断窗口是否可调用该 IPC。
 *
 * 三个取舍写在明处：
 *   1. 表里**没有**的通道一律拒绝（默认关闭）。新增特权 IPC 时必须同时登记，
 *      于是"这个能力给了谁"永远可以从这一个文件读出来；
 *   2. 表里没有的**窗口种类**也一律拒绝（含辅助页拿到主界面通道的情况）；
 *   3. 只做"种类→通道"判定，不做"某条连接→通道"的记忆 —— IPC 是无状态的，
 *      记状态只会引入"记住的那份和当前窗口不一致"的新问题。
 */
function canCall(windowKind, channel) {
  const caps = WINDOW_CAPABILITIES[String(windowKind || '')]
  if (!caps) return false
  return caps.has(String(channel || ''))
}

module.exports = { checkSender, canCall, WINDOW_CAPABILITIES }
