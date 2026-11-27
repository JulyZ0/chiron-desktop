'use strict'
/**
 * 迷你输入框的**窄 preload**（F10）。
 *
 * 与主界面的 preload 刻意分开：这个窗口只该有"提交一段文本 / 关闭自己"两项能力。
 * 共用主 preload 会把 token 读写、清空数据、开任意路径一并暴露给一个只放
 * textarea 的页面 —— H47 的每窗口能力表就是为这种场景准备的。
 */
const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('quickAsk', {
  /**
   * 提交文本。主进程会校验来源窗口、裁剪长度并把内容转给主界面。
   * 返回 `{ ok, reason? }`；失败时页面保留输入，用户可重试。
   */
  submit: text => ipcRenderer.invoke('desktop:quick-ask-submit', { text: String(text || '') }),
  /** 关闭（隐藏）迷你窗口。 */
  cancel: () => ipcRenderer.send('desktop:quick-ask-cancel'),
  /** 每次显示时重置输入（主进程在 show 之后发）。返回取消订阅函数。 */
  onReset: callback => {
    if (typeof callback !== 'function') return () => {}
    const handler = () => callback()
    ipcRenderer.on('desktop:quick-ask-reset', handler)
    return () => ipcRenderer.removeListener('desktop:quick-ask-reset', handler)
  },
})
