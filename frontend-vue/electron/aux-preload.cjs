'use strict'
/**
 * 辅助页（setup / error）的**窄 preload**（PRD 9.2 / H47）。
 *
 * 缺陷形态：这两类页面与主界面共用同一份 `preload.cjs`，于是辅助页的上下文里也有
 * token 读写、清空数据、重启后端、打开任意路径等能力。页面是本地文件，但
 * 「能力面」不该由「当前加载的是哪个 HTML」决定 —— 一旦模板注入或未来的跳转把
 * 这段上下文带出去，暴露的是整套特权。
 *
 * 这里只保留 setup/error 真正用到的四个出口（与 `setup.html` 的调用点一一对应）：
 *   getDeviceSettings / setApiUrl / setMode / testApiUrl（+ restart / getAppInfo）。
 * 其余一律不提供 —— 是"不建通道"，而不是"建了再拦"。
 */
const { contextBridge, ipcRenderer } = require('electron')

const invoke = (channel, ...args) => ipcRenderer.invoke(channel, ...args)

contextBridge.exposeInMainWorld('chironDesktop', {
  isDesktop: true,
  auxPage: true,
  getAppInfo: () => invoke('desktop:get-app-info'),
  getDeviceSettings: () => invoke('desktop:get-device-settings'),
  setApiUrl: (url) => invoke('desktop:set-api-url', url),
  setMode: (mode) => invoke('desktop:set-mode', mode),
  testApiUrl: (url) => invoke('desktop:test-api-url', url),
  restart: () => invoke('desktop:restart'),
})
