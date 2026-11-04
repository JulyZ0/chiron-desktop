// localStorage polyfill — jsdom 30 + vitest 4 兼容性修复
// jsdom 30 的 localStorage 实现可能缺失 clear/setItem，提供内存态 polyfill
if (typeof window !== 'undefined') {
  const store: Record<string, string> = {}
  const localStoragePolyfill = {
    getItem: (k: string): string | null => store[k] ?? null,
    setItem: (k: string, v: string): void => { store[k] = String(v) },
    removeItem: (k: string): void => { delete store[k] },
    clear: (): void => { Object.keys(store).forEach((k) => delete store[k]) },
    key: (i: number): string | null => Object.keys(store)[i] ?? null,
    get length(): number { return Object.keys(store).length },
  }
  // 仅当原生 localStorage 不可用时替换
  if (!window.localStorage || typeof window.localStorage.setItem !== 'function') {
    Object.defineProperty(window, 'localStorage', {
      value: localStoragePolyfill,
      configurable: true,
      writable: true,
    })
  }
}

// vue-i18n 全局注册（仅供组件测试）：
// 迁移后的组件会在 setup 阶段调用 useI18n()，缺少 i18n 实例会直接抛错。在此统一注册，
// 避免每个 spec 各自 mount(..., { global: { plugins: [i18n] } }) 而漏掉某个文件。
// （初始语言在测试环境固定为源语言 zh-CN，见 src/i18n/index.ts 的 initialLocale）
import { config } from '@vue/test-utils'
import { i18n } from './i18n'

config.global.plugins = [i18n]
