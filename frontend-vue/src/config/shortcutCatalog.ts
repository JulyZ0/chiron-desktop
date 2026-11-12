/**
 * 全局快捷键目录：**唯一的事实来源**。
 *
 * 为什么单独一个文件：同一份定义有两个消费者 —— `ChatView.vue` 拿它生成真实绑定，
 * 设置页拿它渲染改键面板。若各自抄一份，改键之后两边就会对不上（设置页显示 A、
 * 实际生效 B），而且新增快捷键必然漏改一处。这里只放**静态元数据**，
 * 不含 handler —— handler 属于各自页面。
 */
import type { ShortcutDef } from '../composables/useKeyboardShortcuts'

/** 分组只影响设置页的排版，不参与匹配。 */
export type ShortcutGroup = 'navigation' | 'conversation' | 'panel'

export interface ShortcutSpec extends ShortcutDef {
  /** 文案的 i18n key（settings 域），三语言必须都有 */
  labelKey: string
  group: ShortcutGroup
}

/**
 * 声明顺序 = **Esc 的逐层关闭优先级**（见 useKeyboardShortcuts 文件头约定 1）。
 * 调整顺序即调整优先级，改动前先想清楚"用户此刻最想关掉什么"。
 *
 * 用 `as const satisfies` 而不是普通类型标注：这样 `id` 保留字面量类型，
 * 下面的 :type:`ShortcutId` 才能在**编译期**挡住写错的 id（如 `'close-pannel'`），
 * 而不是等到运行时才发现那条第快捷键静默失效。
 */
const CATALOG = [
  // 速查表自己是最上层浮层：Esc 必须先关它，否则打开速查表后按 Esc 反应的是下层
  {
    id: 'close-shortcuts-help',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_shortcuts_help',
    group: 'panel',
    customizable: false,
  },
  {
    id: 'open-command-palette',
    defaultKeys: 'mod+k',
    labelKey: 'settings.shortcut_open_command_palette',
    group: 'navigation',
  },
  {
    // **不能是 `mod+k`**：命令面板（CommandPalette）一直硬编码占用 Ctrl/⌘+K，而两者
    // 都监听 window、`preventDefault` 互相拦不住 —— 同一个键会同时弹出命令面板和
    // 会话列表，用户看到两个浮层叠在一起。命令面板保留 mod+k（VS Code / Linear 的
    // 通用直觉），这里让到 mod+shift+k。
    id: 'open-session-list',
    defaultKeys: 'mod+shift+k',
    labelKey: 'settings.shortcut_open_session_list',
    group: 'navigation',
  },
  {
    id: 'find-in-transcript',
    defaultKeys: 'mod+f',
    labelKey: 'settings.shortcut_find_in_transcript',
    group: 'navigation',
  },
  {
    id: 'show-shortcuts',
    defaultKeys: 'shift+?',
    labelKey: 'settings.shortcut_show_shortcuts',
    group: 'navigation',
  },
  // ── 以下四条同为 Esc，顺序即优先级 ──
  {
    id: 'close-session-map',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_session_map',
    group: 'conversation',
  },
  {
    id: 'stop-generation',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_stop_generation',
    group: 'conversation',
  },
  {
    id: 'close-overlays',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_overlays',
    group: 'panel',
  },
  {
    id: 'close-panel',
    defaultKeys: 'escape',
    labelKey: 'settings.shortcut_close_panel',
    group: 'panel',
  },
  // ── 工作台切换 ──
  // 这五个按钮在标题栏里只有图标（32px），没有文字也没有键盘通路；
  // 给它们 mod+1..5 之后，不用瞄准 32px 的小方块也能切工作台。
  // 键位对齐 VS Code 的 Ctrl+1..9 直觉；五个都在同一个域内，用户改一个就会
  // 在设置页看到冲突提示（`conflictOf` 按组合键比对）。
  {
    id: 'goto-workspace-dialogue',
    defaultKeys: 'mod+1',
    labelKey: 'settings.shortcut_goto_dialogue',
    group: 'navigation',
  },
  {
    id: 'goto-workspace-agent',
    defaultKeys: 'mod+2',
    labelKey: 'settings.shortcut_goto_agent',
    group: 'navigation',
  },
  {
    id: 'goto-workspace-skill',
    defaultKeys: 'mod+3',
    labelKey: 'settings.shortcut_goto_skill',
    group: 'navigation',
  },
  {
    id: 'goto-workspace-knowledge',
    defaultKeys: 'mod+4',
    labelKey: 'settings.shortcut_goto_knowledge',
    group: 'navigation',
  },
  {
    id: 'goto-workspace-plugin',
    defaultKeys: 'mod+5',
    labelKey: 'settings.shortcut_goto_plugin',
    group: 'navigation',
  },
] as const satisfies readonly ShortcutSpec[]

/**
 * 对外暴露的目录。
 *
 * 这里刻意**再导出一次宽类型**（而不是直接用 `CATALOG`）：
 * `as const` 会让每个成员保留精确形状 —— 没写 `customizable` 的成员就不存在这个属性，
 * 于是 `spec.customizable` 在联合类型上会报 TS2339。宽类型 `ShortcutSpec` 声明了
 * `customizable?: boolean`，消费者才能统一读取。
 *
 * 字面量 id 的信息不丢：`ShortcutId` 仍从 `CATALOG` 上取。
 */
export const SHORTCUT_CATALOG: readonly ShortcutSpec[] = CATALOG

/** 目录里所有合法 id；`specOf()` 用它做编译期校验。 */
export type ShortcutId = (typeof CATALOG)[number]['id']

export const SHORTCUT_GROUP_LABEL_KEY: Record<ShortcutGroup, string> = {
  navigation: 'settings.shortcuts_group_navigation',
  conversation: 'settings.shortcuts_group_conversation',
  panel: 'settings.shortcuts_group_panel',
}
