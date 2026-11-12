<script lang="ts" setup>
import { computed, h, nextTick, onMounted, onUnmounted, ref, watch, type Component as VueComponent, type VNode } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useThemeStore } from '../stores/theme'
import { useAuthStore } from '../stores/auth'
import { useDeviceStore } from '../stores/device'

// Ant Design Vue 组件
import {
  Avatar,
  
  Badge,
  Dropdown,
  Menu,
  Modal,
  message,
} from 'ant-design-vue'
// Ant Design 图标
import {
  BellOutlined,
  HomeOutlined,
  MessageOutlined,
  UserOutlined,
  BlockOutlined,
  PictureOutlined,
  BookOutlined,
  ThunderboltOutlined,
  CreditCardOutlined,
  SettingOutlined,
  HistoryOutlined,
  RobotOutlined,
  AppstoreOutlined,
  
  ApiOutlined,
} from '@ant-design/icons-vue'
import CommandPalette from './CommandPalette.vue'
import BrandMark from './shell/BrandMark.vue'
import ThemeSwitcher from './ThemeSwitcher.vue'
import LanguageSwitcher from './common/LanguageSwitcher.vue'
import SettingsPanel from './settings/SettingsPanel.vue'
import { buildAccountMenuItems, accountMenuRoute, type AccountMenuItem } from './accountMenu'
import { executeQuickCommand } from "@/components/WorkstationNav.vue"
import {
  WORKSTATION_DESCRIPTIONS,
  WORKSTATION_LABELS,
  WORKSTATION_ROUTES,
} from '../types/workstation'

import { useI18n } from 'vue-i18n'
import { useTakeoverPresence } from '../composables/useTakeoverPresence'
import { useKeyboardShortcuts } from '../composables/useKeyboardShortcuts'
import { SHORTCUT_CATALOG, type ShortcutId } from '../config/shortcutCatalog'
const { t: tr } = useI18n()
// Super-agent 接管的让步信号之一：让引擎知道"我还在这一页"，避免在打字间隙被抢走会话
useTakeoverPresence()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()
// 本地模式：计费/模型配置/管理后台都是云侧页面（路由上也会被 cloudOnly 拦住）
const deviceStore = useDeviceStore()

/**
 * 聊天页：右上角这个**固定定位**胶囊（top:12 / right:12 / 落在 --z-drawer 层）会盖住
 * ChatView 消息区工具栏右端的「更多操作」。
 *
 * 侧栏在右侧时 chat-main 变窄、两者不重叠，所以平时看不出来；一旦交换布局把
 * 侧栏移到左侧，chat-main 变宽、工具栏右端顶到页面右边缘，就正好被它盖住。
 * 该页的主题 / 用户入口改由侧栏底部（ChatSidePanel 的 .panel-foot）承载。
 */
const isChatPage = computed(() => route.path === '/chat')

// 监听 API 错误
function handleApiError(e: Event) {
  const detail = (e as CustomEvent).detail
  message.error(detail.message || tr('errors.request_failed_2'))
}

onMounted(() => {
  window.addEventListener('api:error', handleApiError)
  if (authStore.token) {
    authStore.fetchProfile()
  }
})
onUnmounted(() => {
  window.removeEventListener('api:error', handleApiError)
  document.removeEventListener('click', onQuickDocClick)
  window.removeEventListener('keydown', onQuickKeydown)
})

// 导航菜单（品牌下拉）
interface MenuItem {
  key: string
  label: string
  /** 渲染函数而非组件实例：菜单项要按当前主题/尺寸即时渲染图标 */
  icon?: () => VNode
}

const menuItems = computed<MenuItem[]>(() => {
  const localMode = deviceStore.isLocal()
  const items: MenuItem[] = [
    { key: '/', label: tr('common.home'), icon: () => h(HomeOutlined) },
    { key: WORKSTATION_ROUTES.dialogue, label: WORKSTATION_LABELS.dialogue, icon: () => h(MessageOutlined) },
    { key: WORKSTATION_ROUTES.agent, label: WORKSTATION_LABELS.agent, icon: () => h(UserOutlined) },
    { key: WORKSTATION_ROUTES.skill, label: WORKSTATION_LABELS.skill, icon: () => h(BlockOutlined) },
    { key: '/media', label: tr('common.media'), icon: () => h(PictureOutlined) },
    { key: WORKSTATION_ROUTES.knowledge, label: WORKSTATION_LABELS.knowledge, icon: () => h(BookOutlined) },
    { key: '/memory', label: tr('memory.memory_2'), icon: () => h(HistoryOutlined) },
    { key: WORKSTATION_ROUTES.plugin, label: WORKSTATION_LABELS.plugin, icon: () => h(ThunderboltOutlined) },
    // 计费页已收窄为「本地用量统计」，本地模式同样需要入口，不再按模式隐藏。
    { key: '/billing', label: tr('billing.billing'), icon: () => h(CreditCardOutlined) },
    ...(authStore.isAdmin
      ? [
          // 模型配置：决定对话页模型下拉里能选到什么（后端 /v1/admin/models，需管理员）
          // 本地模式同样要给入口：服务商密钥/端点只有这一页能写进本机网关的库。
          { key: '/models', label: tr('agent.model'), icon: () => h(ApiOutlined) },
          { key: '/admin', label: tr('common.manage'), icon: () => h(SettingOutlined) },
        ]
      : []),
  ]
  return items
})

const currentLabel = computed(() => {
  const hit = menuItems.value.find(m => route.path === m.key || route.path.startsWith(m.key + '/'))
  return hit?.label || ''
})

const selectedKeys = computed(() => {
  const exact = menuItems.value.find(m => route.path === m.key)
  if (exact) return [exact.key]
  // 子页面（如 /billing/prices）高亮它的父菜单项：否则进到子页左侧导航会整列失焦，
  // 看起来像"当前在哪一节"都不见了。与上面 currentLabel 用同一套前缀判定。
  const parent = menuItems.value.find(m => route.path.startsWith(m.key + '/'))
  return [parent?.key ?? route.path]
})

function handleMenuClick(info: { key: string | number }) {
  router.push(String(info.key))
}

// 账号菜单项统一由 components/accountMenu.ts 构造：此前 AppLayout 与
// chat/ChatSidePanel 各写一份并已漂移（聊天页那份缺 api-keys 与通知入口）。
const userMenuItems = computed<AccountMenuItem[]>(() =>
  buildAccountMenuItems((k) => tr(k), { isLocal: deviceStore.isLocal() }),
)

// 设置弹窗与 /profile 页面共用 SettingsPanel，避免两套实现各自漂移
const settingsOpen = ref(false)

async function handleUserMenuClick(info: { key: string | number }) {
  const key = String(info.key)
  if (key === 'logout') {
    await authStore.logout()
    router.push('/')
    return
  }
  if (key === 'settings') {
    settingsOpen.value = true
    return
  }
  // 纯跳转项（profile / notifications / api-keys）统一从共享映射取，
  // 避免新增菜单项时这里漏一个分支导致点了没反应。
  const target = accountMenuRoute(key)
  if (target) {
    router.push(target)
  }
}

// ── 工作台停靠坞：六大工作台全局一键切换 ──
interface DockItem {
  key: string
  label: string
  desc: string
  /** 停靠坞直接 <component :is> 渲染，故存组件本身 */
  icon: VueComponent
}

const dockItems: DockItem[] = [
  { key: WORKSTATION_ROUTES.dialogue, label: WORKSTATION_LABELS.dialogue, desc: WORKSTATION_DESCRIPTIONS.dialogue, icon: MessageOutlined },
  { key: WORKSTATION_ROUTES.agent, label: WORKSTATION_LABELS.agent, desc: WORKSTATION_DESCRIPTIONS.agent, icon: RobotOutlined },
  { key: WORKSTATION_ROUTES.skill, label: WORKSTATION_LABELS.skill, desc: WORKSTATION_DESCRIPTIONS.skill, icon: ThunderboltOutlined },
  { key: WORKSTATION_ROUTES.knowledge, label: WORKSTATION_LABELS.knowledge, desc: WORKSTATION_DESCRIPTIONS.knowledge, icon: BookOutlined },
  { key: WORKSTATION_ROUTES.plugin, label: WORKSTATION_LABELS.plugin, desc: WORKSTATION_DESCRIPTIONS.plugin, icon: AppstoreOutlined },
]

const showDock = computed(() => !!authStore.token && route.path !== '/')

function isDockActive(key: string) {
  return route.path === key || route.path.startsWith(key + '/')
}

function goDock(key: string) {
  if (route.path !== key) router.push(key)
}

/**
 * 工作台切换快捷键（mod+1..5）。
 *
 * 这五个按钮在标题栏里只剩 32px 图标，鼠标点选要瞄准；给键盘通路后才好切。
 * 绑定放在布局层而不是各页面：切换目标是**路由**，与会话状态无关，
 * 放在这里才不会出现"某个页面里快捷键失效"。
 */
function specOfDock(id: ShortcutId) {
  const spec = SHORTCUT_CATALOG.find(s => s.id === id)
  // 目录是常量，走到这里说明目录本身写错了；开发者错误信息用英文，不进 UI
  if (!spec) throw new Error(`shortcutCatalog is missing "${id}"`)
  return spec
}

useKeyboardShortcuts({
  userId: authStore.user?.id ?? null,
  bindings: [
    { ...specOfDock('goto-workspace-dialogue'), handler: () => goDock(WORKSTATION_ROUTES.dialogue) },
    { ...specOfDock('goto-workspace-agent'), handler: () => goDock(WORKSTATION_ROUTES.agent) },
    { ...specOfDock('goto-workspace-skill'), handler: () => goDock(WORKSTATION_ROUTES.skill) },
    { ...specOfDock('goto-workspace-knowledge'), handler: () => goDock(WORKSTATION_ROUTES.knowledge) },
    { ...specOfDock('goto-workspace-plugin'), handler: () => goDock(WORKSTATION_ROUTES.plugin) },
  ],
})

// ── 停靠坞快速命令弹层 ──
const quickOpen = ref(false)
const quickInput = ref('')
const quickLoading = ref(false)
const quickPanelEl = ref<HTMLElement | null>(null)
const quickBtnEl = ref<HTMLElement | null>(null)

function onQuickDocClick(e: MouseEvent) {
  const t = e.target as Node
  if (quickPanelEl.value?.contains(t) || quickBtnEl.value?.contains(t)) return
  quickOpen.value = false
}

function onQuickKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') closeQuickCommand()
}

function toggleQuickCommand() {
  quickOpen.value = !quickOpen.value
}

function closeQuickCommand() {
  quickOpen.value = false
  quickInput.value = ''
}

watch(quickOpen, (open) => {
  if (open) {
    document.addEventListener('click', onQuickDocClick)
    window.addEventListener('keydown', onQuickKeydown)
    nextTick(() => {
      const input = quickPanelEl.value?.querySelector('input') as HTMLInputElement | null
      input?.focus()
    })
  } else {
    document.removeEventListener('click', onQuickDocClick)
    window.removeEventListener('keydown', onQuickKeydown)
  }
})

watch(() => route.path, () => {
  if (quickOpen.value) quickOpen.value = false
})

async function runQuickCommand() {
  const command = quickInput.value.trim()
  if (!command || quickLoading.value) return
  quickLoading.value = true
  try {
    await executeQuickCommand(command)
    closeQuickCommand()
  } catch {
    // 错误已由统一处理器处理
  } finally {
    quickLoading.value = false
  }
}
</script>

<template>
  <div class="app-shell">
    <!-- 标题栏行内的左侧区：品牌胶囊 + 工作台菜单按钮。
         两者都在自绘标题栏那一条里（与右侧最小化/最大化/关闭同高），
         不再是浮在内容上方的独立胶囊 —— 那样会遮挡页面顶部操作。 -->
    <header
      :title="currentLabel || $t('common.navigation_menu')"
      class="topbar"
    >
      <Dropdown
        placement="bottomLeft"
        trigger="click"
      >
        <button
          class="brand-btn"
          :title="$t('common.navigation_menu')"
          :aria-label="$t('common.navigation_menu')"
          type="button"
        >
          <!-- 整个按钮就是一个 SVG 品牌图形：不再是「MC 方块 + Chiron 文字 + 箭头」。
               图形内部全用 currentColor，父级 color 走主题变量 ⇒ 配色随明暗主题自动变化。 -->
          <BrandMark :label="$t('common.navigation_menu')" />
        </button>
        <template #overlay>
          <Menu
            :items="menuItems"
            :selected-keys="selectedKeys"
            class="nav-menu"
            @click="handleMenuClick"
          />
        </template>
      </Dropdown>

      <!-- 工作台切换：原左侧竖排停靠坞的五个按钮，挪进标题栏当菜单按钮。
           停靠坞整条移除后内容区不再需要左边距 —— 那 82px 留白原本只是为了让开它。 -->
      <nav
        v-if="showDock"
        :aria-label="$t('common.workstation_dock')"
        class="dock"
      >
        <button
          v-for="item in dockItems"
          :key="item.key"
          :aria-label="item.label"
          :class="{ active: isDockActive(item.key) }"
          class="dock-item"
          type="button"
          @click="goDock(item.key)"
        >
          <component
            :is="item.icon"
            class="dock-icon"
          />
          <span
            class="dock-tip"
            role="tooltip"
          >
            <span class="dock-tip-name">{{ item.label }}</span>
            <span class="dock-tip-desc">{{ item.desc }}</span>
          </span>
        </button>
      </nav>
    </header>

    <!-- 右上角用户胶囊（聊天页隐藏，理由见 isChatPage 的注释） -->
    <div
      v-if="!isChatPage"
      class="topbar-actions"
    >
      <LanguageSwitcher />
      <ThemeSwitcher />
      <div
        v-if="authStore.user"
        class="user-fab"
      >
        <!-- 与上方品牌下拉同构：显式 trigger="click" + overlay 插槽。
             此前是 `:menu` 单属性且未指定 trigger —— Dropdown 默认 hover 触发，
             所以点击头像不会有任何反应。 -->
        <Dropdown
          trigger="click"
          placement="bottomRight"
        >
          <Avatar
            :size="30"
            :style="{ backgroundColor: 'var(--primary)' }"
            class="user-fab-avatar"
          >
            {{ authStore.user.name?.charAt(0)?.toUpperCase() || 'U' }}
          </Avatar>
          <template #overlay>
            <Menu
              :items="userMenuItems"
              @click="handleUserMenuClick"
            />
          </template>
        </Dropdown>
      </div>
    </div>

    <!-- 全宽内容区。`scroll-fade`：所有工作台页面共用这一个滚动容器，
         所以滚动渐隐在这里挂一次就覆盖全部（滚到顶/底时遮罩自动收为 0）。 -->
    <main
      :class="{ 'app-content--docked': showDock }"
      class="app-content scroll-fade"
    >
      <router-view v-slot="{ Component }">
        <!-- 内容区错误边界：某个页面组件抛错时只替换内容区，侧栏/顶栏保持可用 -->
        <ErrorBoundary>
          <Transition
            mode="out-in"
            name="fade"
            appear
          >
            <component :is="Component" />
          </Transition>
        </ErrorBoundary>
      </router-view>
    </main>

    <!-- 全局命令面板 -->
    <CommandPalette />

    <!-- 设置弹窗：与 /profile 页面共用 SettingsPanel，两条入口一份实现。
         destroy-on-close 保证每次打开都重新挂载并拉取最新数据。 -->
    <Modal
      v-model:open="settingsOpen"
      :title="$t('settings.settings')"
      :footer="null"
      :width="720"
      destroy-on-close
    >
      <SettingsPanel />
    </Modal>
  </div>
</template>

<style scoped>
.app-shell {
  position: relative;
  /* 自绘标题栏在视口顶部占一条，可用高度由 --app-height 统一给出（其它环境为 100dvh） */
  height: var(--app-height);
  background: var(--bg-page);
  color: var(--text-primary);
  --panel-bg: var(--bg-elevated);
  --panel-border: var(--border-default);
  --hover-bg: var(--bg-surface-hover);
  --sidebar-bg: var(--bg-sidebar);
}

/* ── 标题栏行内的左侧区（品牌胶囊 + 工作台菜单按钮） ──
 *
 * 这一块**不再是自己浮在内容上方的一枚胶囊**：它与右侧最小化/最大化/关闭同在
 * 自绘标题栏那一条里，`--app-titlebar-h` 就是那条的高度。浮起来（原 12px 下移）
 * 会盖住页面顶部的操作按钮 —— 那正是要修的问题。
 *
 * 高度取 `max(..., 32px)`：桌面壳里 `--app-titlebar-h` 是 36px（Windows）/28px
 * （macOS inset），浏览器预览里是 0（没有自绘标题栏），必须留一个能放下按钮的底。
 */
.topbar {
  position: fixed;
  top: 0;
  inset-inline-start: 0;
  z-index: var(--z-drawer);
  height: max(var(--app-titlebar-h), 32px);
  display: flex;
  align-items: center;
  gap: 6px;
  padding-inline: 6px;
  /* 容器仍是拖拽区：这样品牌胶囊与按钮之间的空隙照样能拖动窗口 ——
     整块设 no-drag 会把标题栏左半截变成"死区"，那里既不能拖也不能点。
     真正要点击的按钮各自退出拖拽（见 .brand-btn / .dock-item）。 */
  -webkit-app-region: drag;
}

.brand-btn {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 32px;
  padding: 0 8px 0 4px;
  border: none;
  border-radius: var(--sig-radius-button);
  background: transparent;
  cursor: pointer;
  transition: background var(--dur-fast) ease;
  /* 落在标题栏拖拽区里：不退出拖拽的话按下会被当成拖动窗口，菜单永远弹不出来 */
  -webkit-app-region: no-drag;
}

.brand-btn:hover {
  background: var(--hover-bg);
}

/* ── 标题栏行内的右侧操作组（语言 / 主题 / 用户） ──
 *
 * 与左侧区同一套：贴 0 顶、高度跟随标题栏那一行。
 * `--app-titlebar-controls-w` 由 TitleBar 发布 —— 自绘模式下右侧三个窗口按钮
 * 各 46px 占了 138px，不躲开就会被压在下面；inset 模式（macOS 红绿灯在左）
 * 它是 0，这里就自然贴边。
 */
.topbar-actions {
  position: fixed;
  top: 0;
  inset-inline-end: calc(var(--app-titlebar-controls-w, 0px) + 6px);
  z-index: var(--z-drawer);
  height: max(var(--app-titlebar-h), 32px);
  display: flex;
  align-items: center;
  gap: 8px;
  /* 标题栏那条是拖拽区，按钮必须退出拖拽才能收到点击 */
  -webkit-app-region: no-drag;
}

.user-fab {
  padding: 2px;
  border-radius: 50%;
  background: var(--comp-header-bg);
  backdrop-filter: blur(var(--sig-blur-header));
  -webkit-backdrop-filter: blur(var(--sig-blur-header));
  border: var(--hairline-width) solid var(--border-default);
  box-shadow: var(--sig-shadow-card);
  cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}

/* 通知入口：与 user-fab 同构的圆角方形按钮，保证顶栏视觉重量一致 */
.topbar-notify {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  border-radius: 50%;
  border: var(--hairline-width) solid var(--border-default);
  background: var(--comp-header-bg);
  backdrop-filter: blur(var(--sig-blur-header));
  -webkit-backdrop-filter: blur(var(--sig-blur-header));
  box-shadow: var(--sig-shadow-card);
  color: var(--text-secondary);
  cursor: pointer;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}

.topbar-notify:hover {
  color: var(--text-primary);
  border-color: var(--primary);
}

.topbar-notify-icon {
  font-size: 16px;
}

.user-fab-avatar {
  cursor: pointer;
  display: block;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}

.user-fab-avatar:hover {
  opacity: 0.9;
}

/* 导航下拉菜单 */
.nav-menu {
  min-width: 200px;
  border-radius: var(--sig-radius-button);
  padding: 4px;
  box-shadow: var(--sig-shadow-hover);
  background: var(--panel-bg) !important;
  backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  -webkit-backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  border: var(--hairline-width) solid var(--panel-border);
}

.nav-menu :deep(.ant-dropdown-menu-item) {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  border-radius: var(--sig-radius-button);
  padding: 8px 12px !important;
}

.nav-menu :deep(.ant-dropdown-menu-item-selected) {
  background: var(--primary-bg) !important;
  color: var(--primary) !important;
  font-weight: var(--fw-semibold);
}

.app-content {
  height: var(--app-height);
  overflow-y: auto;
  overflow-x: hidden;
}

/* ── 工作台菜单按钮组（标题栏行内，原左侧竖排停靠坞） ──
 *
 * 现在是标题栏那一条里的横向按钮组，不再是浮在内容左侧的竖排抽屉 ——
 * 抽屉会一直占掉内容区 82px，而它的全部内容就是这五个按钮。
 *
 * **刻意不写 `overflow-x: auto`**：横向滚动会把 `overflow-y` 一并变成 auto，
 * 从而裁掉下方弹出的提示气泡（`.dock-tip` 用绝对定位挂在按钮外）。
 */
.dock {
  display: flex;
  align-items: center;
  gap: 2px;
  min-width: 0;
}

.dock-item {
  position: relative;
  width: 32px;
  height: 32px;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: none;
  border-radius: var(--sig-radius-button);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  transition: background var(--dur-fast) ease, color var(--dur-fast) ease, transform var(--dur-fast) ease;
  /* 同 .brand-btn：落在标题栏拖拽区里，必须退出拖拽才点得到 */
  -webkit-app-region: no-drag;
}

.dock-item:hover {
  background: var(--hover-bg);
  color: var(--text-primary);
}

.dock-item:active {
  transform: scale(0.94);
}

.dock-item:focus-visible,
.dock-command-btn:focus-visible,
.brand-btn:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}

.dock-item.active {
  background: var(--primary);
  color: var(--text-inverse);
  box-shadow: 0 4px 14px var(--primary-bg), inset 0 1px 1px var(--on-viewer-faint);
}

/* 活动指示条：竖排抽屉里是左侧竖条，横排进标题栏后改成**下边缘的短横条** ——
   竖条在标题栏里会悬在按钮左外侧，看着像一条孤立的分隔线。 */
.dock-item.active::before {
  content: '';
  position: absolute;
  left: 50%;
  bottom: 3px;
  width: 16px;
  height: 2px;
  border-radius: 1px;
  background: var(--on-solid);
  transform: translateX(-50%) scaleX(0);
  transform-origin: center;
  animation: dockBarIn var(--dur-normal) ease-out forwards;
}

@keyframes dockBarIn {
  to {
    transform: translateX(-50%) scaleX(1);
  }
}

.dock-icon {
  font-size: 16px;
}

/* 提示气泡：抽屉里弹在右侧，标题栏里改弹在下方 —— 上方是窗口边缘，右侧会盖住别的按钮 */
.dock-tip {
  position: absolute;
  top: calc(100% + 6px);
  left: 50%;
  transform: translateX(-50%) translateY(-4px);
  display: flex;
  flex-direction: column;
  gap: 1px;
  padding: 5px 10px;
  border-radius: var(--sig-radius-button);
  background: var(--panel-bg);
  backdrop-filter: blur(var(--sig-blur-header));
  -webkit-backdrop-filter: blur(var(--sig-blur-header));
  border: var(--hairline-width) solid var(--panel-border);
  box-shadow: var(--sig-shadow-card);
  color: var(--text-primary);
  font-size: 12px;
  font-weight: var(--fw-medium);
  white-space: nowrap;
  opacity: 0;
  pointer-events: none;
  transition: opacity var(--dur-fast) ease, transform var(--dur-fast) ease;
  z-index: var(--z-dock);
}

.dock-tip-name {
  line-height: 16px;
}

.dock-tip-desc {
  font-size: var(--fs-xs);
  font-weight: var(--fw-regular);
  color: var(--text-tertiary);
  line-height: 15px;
}

.dock-item:hover .dock-tip {
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}

@media (hover: none) {
  .dock-tip {
    display: none;
  }
}

/* ── 快速命令 ── */
.dock-command {
  position: relative;
  margin-top: auto;
}

.dock-command-btn {
  width: 44px;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border: 1px dashed var(--panel-border);
  border-radius: var(--sig-radius-button);
  background: var(--hover-bg);
  color: var(--text-secondary);
  font-size: 17px;
  cursor: pointer;
  transition: color var(--dur-fast) ease, border-color var(--dur-fast) ease, background var(--dur-fast) ease;
}

.dock-command-btn:hover,
.dock-command-btn.open {
  color: var(--primary);
  border-color: var(--primary);
  background: var(--primary-bg);
}

.dock-popover {
  position: absolute;
  left: calc(100% + 10px);
  bottom: 0;
  width: 340px;
  padding: 12px;
  border-radius: var(--sig-radius-code);
  background: var(--panel-bg);
  backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  -webkit-backdrop-filter: blur(calc(var(--sig-blur-header) + 4px));
  border: var(--hairline-width) solid var(--panel-border);
  box-shadow: var(--sig-shadow-hover);
  z-index: var(--z-dock);
}

.dock-popover-head {
  margin-bottom: 10px;
}

.dock-popover-title {
  display: block;
  font-size: 13px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}

.dock-popover-hint {
  display: block;
  margin-top: 2px;
  font-size: var(--fs-xs);
  line-height: 16px;
  color: var(--text-tertiary);
}

.dock-command-row {
  display: flex;
  gap: 8px;
}

.dock-command-input {
  flex: 1;
  min-width: 0;
  min-height: 40px;
  padding: 8px 12px;
  border: var(--hairline-width) solid var(--border-default);
  border-radius: var(--sig-radius-card);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
  transition: border-color var(--dur-normal) ease, box-shadow var(--dur-normal) ease;
}

.dock-command-input:focus {
  border-color: var(--primary);
  box-shadow: 0 0 0 3px var(--primary-bg);
}

.dock-command-input::placeholder {
  color: var(--text-quaternary);
}

.dock-command-go {
  flex: none;
  min-height: 40px;
  min-width: 64px;
  padding: 0 14px;
  border: none;
  border-radius: var(--sig-radius-card);
  background: var(--primary);
  color: var(--text-inverse);
  font-size: 13px;
  font-weight: var(--fw-medium);
  cursor: pointer;
  transition: background var(--dur-normal) ease, opacity var(--dur-normal) ease;
}

.dock-command-go:hover:not(:disabled) {
  background: var(--primary-hover);
}

.dock-command-go:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.dock-spinner {
  display: inline-block;
  width: 14px;
  height: 14px;
  border: 2px solid var(--on-viewer-soft);
  border-top-color: var(--on-solid);
  border-radius: 50%;
  animation: dockSpin var(--dur-spin) linear infinite;
}

@keyframes dockSpin {
  to {
    transform: rotate(360deg);
  }
}

.dock-pop-enter-active,
.dock-pop-leave-active {
  transition: opacity var(--dur-normal) ease, transform var(--dur-normal) ease;
}

.dock-pop-enter-from,
.dock-pop-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

/* 停靠坞整条移除后，内容区不再需要为它留左边距 —— 那 82px 原本只是让开竖排抽屉。
   class 仍由模板按 showDock 挂上（令牌脚本与存量样式依赖它），故保留为空规则并注明。 */
.app-content--docked {
  margin-inline-start: 0;
}

/* 窄窗口下同一条标题栏继续用，只做两处收口：按钮缩到 28px，并禁用悬浮气泡
   （空间不够时气泡会挡住相邻按钮，而 aria-label 仍然可读）。 */
@media (max-width: 768px) {
  .dock-item {
    width: 28px;
    height: 28px;
  }

  .dock-tip {
    display: none;
  }
}

@media (prefers-reduced-motion: reduce) {
  .dock-item,
  .dock-command-btn,
  .dock-command-input,
  .dock-command-go,
  .dock-tip {
    transition: none;
  }

  .dock-item.active::before {
    animation: none;
    transform: translateY(-50%) scaleY(1);
  }

  @media (max-width: 768px) {
    .dock-item.active::before {
      animation: none;
      transform: translateX(-50%) scaleX(1);
    }
  }
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--dur-normal) ease;
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

/* 首次进入（appear）走单独的一档：路由切换要快（--dur-normal，否则 out-in
   的淡出+淡入叠起来会让"点导航"显得迟钝），而直接打开某个页面时用 --dur-page
   给一点"落定"的余地。Vue 在有 *-appear-* 类时优先用它，没有才回退到 enter。 */
.fade-appear-active {
  transition: opacity var(--dur-page) var(--ease-out);
}

.fade-appear-from {
  opacity: 0;
}
</style>