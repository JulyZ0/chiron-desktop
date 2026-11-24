/**
 * 敏感落盘门禁（H52）。
 *
 * ## 为什么需要单独一个门
 *
 * `data-dir-acl.cjs` 只能回答"这次加固成没成"，而"没成"以前只被写进一行日志：
 * 运行继续、token 照写、`secrets.bin` 照落。后果是把密钥写进一个可能对未授权
 * 主体开放的目录 —— 比"加固失败"本身严重得多。
 *
 * 本模块把加固结论变成**可查询的状态**，并规定：
 *   - 未评估（没调用过 `applyHardenResult`）⇒ 一律拒绝写入（fail-closed）；
 *   - `ok: false`（含 `skipped`）⇒ 拒绝写入，并给出可读原因；
 *   - `ok: true` ⇒ 允许写入。
 *
 * 拒绝时返回结构化结果而不是抛错：IPC 调用方要能区分"权限没加固好"与"写盘失败"。
 */

/** 拒绝时对外统一的 reason 码（渲染层可据此提示，而不是解析中文消息）。 */
const GATE_REASON = 'data-dir-acl-failed'

/**
 * 创建门禁。
 *
 * @param {object} [options]
 * @param {(level: string, message: string) => void} [options.log] 记录降级（可观测，不静默）
 * @returns {{
 *   applyHardenResult: (result: object) => object,
 *   isAllowed: () => boolean,
 *   state: () => { allowed: boolean, reason: string, skipped: boolean },
 *   refusal: (kind: string) => { ok: false, reason: string, message: string },
 * }}
 */
function createSensitiveStoreGate(options = {}) {
  const log = typeof options.log === 'function' ? options.log : () => {}
  let state = { allowed: false, reason: 'not-evaluated', skipped: false }

  function applyHardenResult(result) {
    const ok = Boolean(result && result.ok)
    const skipped = Boolean(result && result.skipped)
    const reason = String((result && result.reason) || (ok ? 'ok' : 'unknown'))
    state = { allowed: ok, reason, skipped }
    if (!ok) {
      log('error', `敏感目录加固未通过（${reason}${skipped ? ', skipped' : ''}）：拒绝写入 token/secret`)
    }
    return { ...state }
  }

  function refusal(kind) {
    const what = String(kind || 'secret')
    return {
      ok: false,
      reason: GATE_REASON,
      message: `敏感数据目录加固未通过（${state.reason}），已拒绝写入 ${what}`,
    }
  }

  return {
    applyHardenResult,
    isAllowed: () => state.allowed,
    state: () => ({ ...state }),
    refusal,
  }
}

module.exports = { GATE_REASON, createSensitiveStoreGate }
