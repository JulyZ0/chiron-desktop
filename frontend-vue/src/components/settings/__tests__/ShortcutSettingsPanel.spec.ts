import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'

/**
 * 改键面板的回归网。
 *
 * 钉住的是一个**真实存在过的必现缺陷**：录制态与普通态是两个不同元素
 * （`v-if` / `v-else`），点「改键」后旧按钮被卸载、新按钮挂载，焦点落回 `<body>`，
 * 于是挂在按钮上的 `@keydown` 永远收不到按键 —— 用户眼里就是"点了改键，按什么都没反应"。
 *
 * 所以这里的断言刻意**不 focus 按钮**：输入事件直接派发到 `document.body`，
 * 与焦点在哪儿无关。若哪天有人把监听挪回按钮自己的 `@keydown`，这条会立刻红。
 */

vi.mock('../../../stores/auth', () => ({
  useAuthStore: () => ({ user: { id: 'u-test' } }),
}))

const { default: ShortcutSettingsPanel } = await import('../ShortcutSettingsPanel.vue')
const { SHORTCUT_CATALOG } = await import('../../../config/shortcutCatalog')

const STORAGE_KEY = 'chiron:shortcuts:u-test'

/** 派发一次真实会冒泡的 keydown（capture 阶段从 document 一路到 body）。 */
function press(key: string, mods: KeyboardEventInit = {}) {
  const ev = new KeyboardEvent('keydown', { key, cancelable: true, bubbles: true, ...mods })
  document.body.dispatchEvent(ev)
  return ev
}

beforeEach(() => {
  localStorage.clear()
})

describe('ShortcutSettingsPanel · 改键', () => {
  it('点「改键」后按键即生效，且不依赖按钮焦点', async () => {
    const wrapper = mount(ShortcutSettingsPanel)
    // 目录里第一条默认可改的是 open-command-palette（mod+k）；不可改的那条按钮是 disabled
    const spec = SHORTCUT_CATALOG.find(s => s.customizable !== false && s.defaultKeys === 'mod+k')!
    expect(spec).toBeTruthy()

    const button = wrapper
      .findAll('.shortcut-key')
      .find(b => !b.attributes('disabled'))!
    await button.trigger('click')
    await nextTick()

    // 刻意不 focus 任何元素：模拟"点击后焦点落回 body"的真实情形
    press('j', { ctrlKey: true, shiftKey: true })
    await nextTick()

    const stored = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    expect(stored[spec.id]).toBe('mod+shift+j')
    // 录到键之后就该退出录制态
    expect(wrapper.find('.shortcut-key.recording').exists()).toBe(false)
  })

  it('Esc 取消录制，不写入任何键', async () => {
    const wrapper = mount(ShortcutSettingsPanel)
    const button = wrapper.findAll('.shortcut-key').find(b => !b.attributes('disabled'))!
    await button.trigger('click')
    await nextTick()

    press('Escape')
    await nextTick()

    expect(wrapper.find('.shortcut-key.recording').exists()).toBe(false)
    expect(localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('录制期间吞掉全局快捷键：按键不会再冒泡到 window 上的注册表', async () => {
    const wrapper = mount(ShortcutSettingsPanel)
    const button = wrapper.findAll('.shortcut-key').find(b => !b.attributes('disabled'))!
    await button.trigger('click')
    await nextTick()

    const globalHandler = vi.fn()
    window.addEventListener('keydown', globalHandler)
    press('j', { ctrlKey: true })
    window.removeEventListener('keydown', globalHandler)

    expect(globalHandler).not.toHaveBeenCalled()
  })
})
