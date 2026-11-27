'use strict'
/**
 * 迷你输入框（F10）的页面脚本。
 *
 * 只做三件事：把文本交给主进程、Esc 退出、每次显示时清空并聚焦。
 * **不在这里发 HTTP**：迷你窗口没有登录态（token 在 preload 的 additionalArguments
 * 里、属于主界面），让它自己调网关等于复制一整套鉴权与刷新逻辑。
 */

const input = document.getElementById('qa-input')
const sendButton = document.getElementById('qa-send')

let submitting = false

function reset() {
  input.value = ''
  submitting = false
  input.focus()
}

function close() {
  window.quickAsk?.cancel?.()
}

function submit() {
  if (submitting) return
  const text = input.value.trim()
  if (!text) {
    close()
    return
  }
  submitting = true
  // 主进程负责隐藏窗口并把文本转给主界面；失败（例如没有主窗口）时不静默：
  // 恢复按钮，用户至少能看见"没发出去"，可以重试或复制文本。
  Promise.resolve(window.quickAsk?.submit?.(text))
    .then(result => {
      if (result && result.ok === false) {
        submitting = false
        input.focus()
      }
    })
    .catch(() => {
      submitting = false
      input.focus()
    })
}

input.addEventListener('keydown', event => {
  if (event.isComposing || event.keyCode === 229) return
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    submit()
    return
  }
  if (event.key === 'Escape') {
    event.preventDefault()
    close()
  }
})

sendButton.addEventListener('click', submit)

window.quickAsk?.onReset?.(reset)
reset()
