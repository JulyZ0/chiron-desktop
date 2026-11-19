<script setup lang="ts">
/**
 * 快捷键设置面板 —— 设置页「快捷键」标签页的内容。
 *
 * 键位定义**只从** `config/shortcutCatalog.ts` 读，不在这里另抄一份：
 * 目录是 ChatView 与设置页共同的事实来源，抄两份必然出现"设置页显示 A、实际生效 B"。
 *
 * 本组件**不注册任何全局 keydown**（用 `useShortcutPreferences` 而非 `useKeyboardShortcuts`）：
 * 在设置页里按下 Ctrl+K 不该把用户送到会话页去。
 *
 * 改键状态是模块级共享的（见 `useKeyboardShortcuts.ts` 的 `overrideStores`），
 * 所以在这里改完，回到会话页立刻生效，不需要重新挂载。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { Button, message } from 'ant-design-vue'
import { useI18n } from 'vue-i18n'
import {
  comboLabel,
  eventToCombo,
  useShortcutPreferences,
} from '../../composables/useKeyboardShortcuts'
import {
  SHORTCUT_CATALOG,
  SHORTCUT_GROUP_LABEL_KEY,
  type ShortcutGroup,
  type ShortcutSpec,
} from '../../config/shortcutCatalog'
import { useAuthStore } from '../../stores/auth'

const { t } = useI18n()
const authStore = useAuthStore()

const prefs = useShortcutPreferences({
  specs: SHORTCUT_CATALOG,
  userId: authStore.user?.id ?? null,
})

const isMac = typeof navigator !== 'undefined'
  && /mac|iphone|ipad/i.test(navigator.platform || navigator.userAgent)

/** 正在录制新键位的条目 id；同一时刻只录一条 */
const recordingId = ref<string | null>(null)

const GROUPS: readonly ShortcutGroup[] = ['navigation', 'conversation', 'panel']
const grouped = computed(() =>
  GROUPS.map(group => ({
    group,
    titleKey: SHORTCUT_GROUP_LABEL_KEY[group],
    items: SHORTCUT_CATALOG.filter(s => s.group === group),
  })).filter(g => g.items.length > 0))

function keysLabel(spec: ShortcutSpec): string {
  return comboLabel(prefs.keysOf(spec.id), isMac)
}

function defaultLabel(spec: ShortcutSpec): string {
  return comboLabel(spec.defaultKeys, isMac)
}

function isChanged(spec: ShortcutSpec): boolean {
  return prefs.keysOf(spec.id) !== spec.defaultKeys
}

function labelOf(id: string): string {
  const spec = SHORTCUT_CATALOG.find(s => s.id === id)
  return spec ? t(spec.labelKey) : id
}

function startRecording(spec: ShortcutSpec) {
  if (spec.customizable === false) return
  recordingId.value = spec.id
}

function stopRecording() {
  recordingId.value = null
}

/**
 * 录制期间的按键处理。
 *
 * 挂在 `document` 的 **capture 阶段**，而不是录制按钮自己的 `@keydown`：
 *
 *  1) 原来的写法有个必现缺陷 —— 录制态与普通态是两个不同的元素（`v-if` / `v-else`），
 *     点「改键」后 Vue 卸载旧按钮、挂载新按钮，焦点随之落回 `<body>`，之后按下的键
 *     **根本不会经过那个按钮**，`@keydown` 永不触发 —— 表现就是"改键点了没反应"。
 *     capture 阶段在 document 上收事件，与焦点在哪里无关。
 *  2) 它还顺带压过 `window` 上的全局注册表（`useKeyboardShortcuts` 在冒泡阶段监听
 *     `window`）：录制时按 Ctrl+K 只该被记成新键，不该同时弹出命令面板。
 */
function onRecordKeydown(e: KeyboardEvent) {
  const id = recordingId.value
  if (!id) return
  const spec = SHORTCUT_CATALOG.find(s => s.id === id)
  if (!spec) return

  // 录制期间的所有按键都归这里，先拦掉浏览器默认行为（如 Ctrl+F 的页面查找）
  // 和默认冒泡（否则全局注册表会跟着响应）。
  e.preventDefault()
  e.stopPropagation()

  if (e.key === 'Escape') {
    // Esc 是"取消录制"，不能拿来当新键 —— 它被逐层关闭占用着一整条链
    stopRecording()
    return
  }
  // 输入法组合期间不记键：此时 e.key 是中间态
  if (e.isComposing) return

  const combo = eventToCombo(e, isMac)
  // 只按了修饰键：用户还没按完，继续等
  if (!combo) return

  const result = prefs.setKeys(spec.id, combo)
  if (result.ok) {
    recordingId.value = null
    message.success(`${t(spec.labelKey)} → ${comboLabel(combo, isMac)}`)
  } else {
    // 冲突时保持录制态，让用户直接再按一个别的组合
    message.warning(t('settings.shortcut_conflict', { name: labelOf(result.conflictWith) }))
  }
}

/** 录制开关与 document 监听同生共死 —— 漏摘会留下一个持续吞键的监听器。 */
watch(recordingId, (id, prev) => {
  if (prev && !id) document.removeEventListener('keydown', onRecordKeydown, true)
  if (id && !prev) document.addEventListener('keydown', onRecordKeydown, true)
})

function resetOne(spec: ShortcutSpec) {
  prefs.resetKeys(spec.id)
}

function resetAll() {
  prefs.resetAll()
  message.success(t('settings.shortcut_reset_all'))
}

onUnmounted(() => {
  document.removeEventListener('keydown', onRecordKeydown, true)
  recordingId.value = null
})
</script>

<template>
  <div class="shortcut-panel">
    <Alert
      class="shortcut-hint"
      type="info"
      :message="$t('settings.shortcuts_hint')"
      show-icon
    />

    <div
      v-for="section in grouped"
      :key="section.group"
      class="shortcut-group"
    >
      <div class="shortcut-group-title">
        {{ $t(section.titleKey) }}
      </div>

      <div
        v-for="spec in section.items"
        :key="spec.id"
        class="shortcut-row"
      >
        <span class="shortcut-name">{{ $t(spec.labelKey) }}</span>

        <span class="shortcut-actions">
          <Button
            v-if="isChanged(spec)"
            size="small"
            type="link"
            @click="resetOne(spec)"
          >
            {{ $t('settings.shortcut_reset') }}
          </Button>

          <!-- 录制态：按键由 document 上的 capture 监听收取（见 onRecordKeydown 注释），
               所以这里不挂 @keydown —— 焦点在不在这个按钮上都一样能录到。
               点一下按钮本身 = 取消录制。 -->
          <Button
            v-if="recordingId === spec.id"
            size="small"
            type="primary"
            class="shortcut-key recording"
            @click="stopRecording"
          >
            {{ $t('settings.shortcut_recording') }}
          </Button>
          <Button
            v-else
            size="small"
            class="shortcut-key"
            :disabled="spec.customizable === false"
            :title="spec.customizable === false ? $t('settings.shortcut_not_customizable') : $t('settings.shortcut_change')"
            @click="startRecording(spec)"
          >
            {{ keysLabel(spec) }}
          </Button>

          <span
            v-if="spec.customizable === false"
            class="shortcut-hint-inline"
          >{{ $t('settings.shortcut_not_customizable') }}</span>
          <span
            v-else-if="isChanged(spec)"
            class="shortcut-hint-inline"
          >{{ $t('settings.shortcut_reset') }}: {{ defaultLabel(spec) }}</span>
        </span>
      </div>
    </div>

    <div class="shortcut-footer">
      <Button
        size="small"
        @click="resetAll"
      >
        {{ $t('settings.shortcut_reset_all') }}
      </Button>
    </div>
  </div>
</template>

<style scoped>
.shortcut-hint { margin-bottom: 16px; }
.shortcut-group { margin-bottom: 20px; }
.shortcut-group-title {
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
  margin-bottom: 8px;
}
.shortcut-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-block: 6px;
  border-bottom: var(--hairline-width) dashed var(--border-color, var(--border-card));
}
.shortcut-name { color: var(--text-primary); }
.shortcut-actions {
  margin-inline-start: auto;
  display: flex;
  align-items: center;
  gap: 8px;
}
.shortcut-key {
  min-width: 96px;
  font-family: var(--font-mono, 'JetBrains Mono', 'Cascadia Code', 'Fira Code', monospace);
}
.shortcut-key.recording { min-width: 160px; }
.shortcut-hint-inline { color: var(--text-secondary); font-size: 12px; }
.shortcut-footer { margin-top: 8px; }

@media (max-width: 576px) {
  .shortcut-row { flex-wrap: wrap; }
  .shortcut-actions { margin-inline-start: 0; }
}
</style>
