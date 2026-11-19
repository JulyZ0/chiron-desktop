<script setup lang="ts">
import { ref, computed, h, watch, onBeforeUnmount } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { useThemeStore } from '../../stores/theme'
import {
  Layout,
  LayoutSider,
  LayoutHeader,
  LayoutContent,
  Menu,
  MenuItem,
  MenuItemGroup,
  Breadcrumb,
  BreadcrumbItem,
  Button,
  Dropdown,
} from 'ant-design-vue'
import {
  DashboardOutlined,
  KeyOutlined,
  DatabaseOutlined,
  ThunderboltOutlined,
  SettingOutlined,
  ClockCircleOutlined,
  UserOutlined,
  LogoutOutlined,
  TeamOutlined,
  SafetyOutlined,
  MedicineBoxOutlined,
  CloudServerOutlined,
  BookOutlined,
  ShareAltOutlined,
  ShopOutlined,
  BarChartOutlined,
  IdcardOutlined,
  WalletOutlined,
  PayCircleOutlined,
  ReconciliationOutlined,
  ControlOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  BulbOutlined,
  DownloadOutlined,

  MailOutlined,
} from '@ant-design/icons-vue'

import { useI18n } from 'vue-i18n'
import type { MenuProps } from 'ant-design-vue'
const { t } = useI18n()
const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const themeStore = useThemeStore()

// 折叠态：桌面端用 v-model，移动端用抽屉。
// 折叠选择要**记住**：刷新后回到用户上次的状态，而不是每次重新展开。
const ADMIN_NAV_COLLAPSED_KEY = 'chiron:admin-nav:collapsed:v1'
function readCollapsed(): boolean {
  try {
    return localStorage.getItem(ADMIN_NAV_COLLAPSED_KEY) === '1'
  } catch {
    return false // 隐私模式：按默认展开
  }
}
const collapsed = ref(readCollapsed())
watch(collapsed, value => {
  try {
    localStorage.setItem(ADMIN_NAV_COLLAPSED_KEY, value ? '1' : '0')
  } catch {
    /* 存不下只在本次运行内生效 */
  }
})
// 移动端抽屉可见性（< 960px 触发抽屉模式）
const isMobile = ref(false)
const drawerOpen = ref(false)

function checkMobile() {
  isMobile.value = window.innerWidth < 960
  if (!isMobile.value) drawerOpen.value = false
}
if (typeof window !== 'undefined') {
  checkMobile()
  window.addEventListener('resize', checkMobile)
}
onBeforeUnmount(() => {
  window.removeEventListener('resize', checkMobile)
})

// 路由切换时关闭移动端抽屉
watch(() => route.path, () => { drawerOpen.value = false })

const breadcrumbs = computed(() => {
  const matched = route.matched.filter(r => r.meta?.title || r.name)
  return matched.map(item => ({
    path: item.path,
    title: (item.meta?.title as string) || (item.name as string) || '',
  }))
})

// 菜单分组：总览监控 / 访问安全 / 系统 / 平台 四组
const menuGroups = computed(() => [
  {
    key: 'g-monitor', label: t('admin.overview_monitoring'),
    children: [
      { key: '/admin/dashboard', label: t('common.dashboard'), icon: () => h(DashboardOutlined) },
      // 原「性能监控 / 队列监控 / 缓存监控」三页已合并为运行时监控（Tabs）
      { key: '/admin/monitor', label: t('admin.runtime_monitoring'), icon: () => h(ThunderboltOutlined) },
    ],
  },
  {
    key: 'g-access', label: t('common.access_security'),
    children: [
      { key: '/admin/api-keys', label: t('common.api_key_management'), icon: () => h(KeyOutlined) },
      // 分享管理：会话公开链接的运营处置面（谁分享了什么、一键掐断）。
      // 与 API Key 同属"访问安全"——都决定"外面能拿到什么"。
      { key: '/admin/shares', label: t('admin.share_management'), icon: () => h(ShareAltOutlined) },
    ],
  },
  {
    key: 'g-system', label: t('common.system'),
    children: [
      { key: '/admin/settings', label: t('settings.system_settings'), icon: () => h(SettingOutlined) },
      // 原「Redis 管理 + 数据库管理」合并为数据存储（Tabs）
      { key: '/admin/datastores', label: t('common.data_storage'), icon: () => h(DatabaseOutlined) },
      // D1：自诊断 —— 与设置/数据存储同属"系统"类：都是"让部署正确运转"的操作面。
      { key: '/admin/diagnostics', label: t('admin.diagnostics'), icon: () => h(MedicineBoxOutlined) },
      // 服务：这台机器对谁可见（仅本机 / 局域网、对外域名）。
      // 与设置/数据存储同理 —— 它改的是部署形态本身，不是某个业务功能。
      { key: '/admin/service', label: t('admin.service.page_title'), icon: () => h(CloudServerOutlined) },
      // 知识库：索引服务用本地模式还是外部 pageindex 服务（随时可切，引擎即时生效）。
      { key: '/admin/knowledge', label: t('admin.knowledge.page_title'), icon: () => h(BookOutlined) },
      // 资源市场「外部源」：初装默认关闭；开启后本机才开始访问第三方 URL。
      { key: '/admin/market', label: t('admin.market.page_title'), icon: () => h(ShopOutlined) },
      // Python 依赖管理：装/卸的是**引擎解释器**里的包，同样属于"让系统正确运转"。
      { key: '/admin/deps', label: t('admin.deps_title'), icon: () => h(DownloadOutlined) },
    ],
  },
])

// 当前路由命中的菜单项（用于侧边栏 selectedKeys）
const selectedKeys = computed(() => {
  const hit = menuGroups.value
    .flatMap(g => g.children)
    .find(m => route.path === m.key || route.path.startsWith(m.key + '/'))
  return [hit?.key ?? route.path]
})

/** antd Menu / Dropdown 的点击载荷（本页只用 key） */
interface MenuClickInfo {
  key: string | number
}

const userMenuItems = computed<MenuProps['items']>(() => [
  { key: 'profile', label: t('settings.profile'), icon: () => h(UserOutlined) },
  { key: 'toggle-theme', label: themeStore.isDark ? t('common.light_mode') : t('common.dark_mode'), icon: () => h(BulbOutlined) },
  { type: 'divider' as const },
  { key: 'logout', label: t('auth.logout'), icon: () => h(LogoutOutlined) },
])

function handleMenuClick(info: MenuClickInfo) {
  router.push(String(info.key))
}

async function handleUserAction(info: MenuClickInfo) {
  if (info.key === 'logout') {
    // 安全：调 authStore.logout 清后端 httpOnly cookie + 本地 user
    await authStore.logout()
    // 本地模式下这是**锁定**而不是登出：跳转会让解锁后落到首页，丢掉管理员
    // 正在看的那个页面。只有云端真登出才回首页。
    if (authStore.locked) return
    router.push('/')
  } else if (info.key === 'profile') {
    router.push('/profile')
  } else if (info.key === 'toggle-theme') {
    themeStore.toggleTheme()
  }
}
</script>

<template>
  <Layout class="admin-root">
    <!-- 桌面端固定 Sider -->
    <LayoutSider
      v-if="!isMobile"
      v-model:collapsed="collapsed"
      collapsible
      :trigger="null"
      :width="240"
      :collapsed-width="72"
      class="admin-sider"
    >
      <div class="sider-menu">
        <Menu
          mode="inline"
          :selected-keys="selectedKeys"
          :inline-collapsed="collapsed"
          @click="handleMenuClick"
        >
          <template
            v-for="g in menuGroups"
            :key="g.key"
          >
            <MenuItemGroup :title="collapsed ? '' : g.label">
              <MenuItem
                v-for="m in g.children"
                :key="m.key"
              >
                <template #icon>
                  <component :is="m.icon" />
                </template>
                {{ m.label }}
              </MenuItem>
            </MenuItemGroup>
          </template>
        </Menu>
      </div>

      <!-- 用户菜单（资料 / 主题 / 登出）：放在侧边栏底部而不是右上角。
           右上角按要求不再出现头像；但登出与主题切换必须保留可达路径，
           所以把入口下移到导航栏底部，与导航同处一条竖轴。 -->
      <div class="sider-footer">
        <Dropdown
          :menu="{ items: userMenuItems, onClick: handleUserAction }"
          placement="topLeft"
        >
          <button
            type="button"
            class="sider-user-btn"
            :title="$t('admin.user_menu')"
            :aria-label="$t('admin.user_menu')"
          >
            <UserOutlined />
            <span v-if="!collapsed" class="sider-user-label">{{ $t('admin.user_menu') }}</span>
          </button>
        </Dropdown>
      </div>
    </LayoutSider>

    <!-- 移动端抽屉 Sider -->
    <LayoutSider
      v-else
      v-model:collapsed="drawerOpen"
      :trigger="null"
      :width="260"
      class="admin-sider admin-sider--drawer"
      :class="{ 'admin-sider--drawer-open': drawerOpen }"
    >
      <div class="sider-menu">
        <Menu
          mode="inline"
          :selected-keys="selectedKeys"
          @click="handleMenuClick"
        >
          <template
            v-for="g in menuGroups"
            :key="g.key"
          >
            <MenuItemGroup :title="g.label">
              <MenuItem
                v-for="m in g.children"
                :key="m.key"
              >
                <template #icon>
                  <component :is="m.icon" />
                </template>
                {{ m.label }}
              </MenuItem>
            </MenuItemGroup>
          </template>
        </Menu>
      </div>

      <!-- 抽屉模式与桌面端一致：用户入口在导航底部 -->
      <div class="sider-footer">
        <Dropdown
          :menu="{ items: userMenuItems, onClick: handleUserAction }"
          placement="topLeft"
        >
          <button
            type="button"
            class="sider-user-btn"
            :title="$t('admin.user_menu')"
            :aria-label="$t('admin.user_menu')"
          >
            <UserOutlined />
            <span class="sider-user-label">{{ $t('admin.user_menu') }}</span>
          </button>
        </Dropdown>
      </div>
    </LayoutSider>
    <div
      v-if="isMobile && drawerOpen"
      class="admin-drawer-mask"
      aria-hidden="true"
      @click="drawerOpen = false"
    />

    <Layout class="admin-main">
      <LayoutHeader class="admin-header">
        <div class="header-left">
          <Button
            type="text"
            class="header-collapse-btn"
            :title="$t('common.expand_or_collapse_navigation')"
            :aria-label="collapsed ? $t('common.expand_sidebar') : $t('common.collapse_sidebar')"
            @click="isMobile ? (drawerOpen = !drawerOpen) : (collapsed = !collapsed)"
          >
            <component :is="isMobile ? (drawerOpen ? MenuUnfoldOutlined : MenuFoldOutlined) : (collapsed ? MenuUnfoldOutlined : MenuFoldOutlined)" />
          </Button>
          <Breadcrumb class="header-breadcrumb">
            <BreadcrumbItem
              v-for="(item, index) in breadcrumbs"
              :key="item.path"
              :class="{ 'u-hide-sm': index > 0 && index < breadcrumbs.length - 1 }"
            >
              {{ item.title }}
            </BreadcrumbItem>
          </Breadcrumb>
        </div>
      </LayoutHeader>
      <LayoutContent class="admin-content">
        <router-view v-slot="{ Component }">
          <!-- 内容区错误边界：某个页面组件抛错时只替换内容区，侧边导航保持可用 -->
          <ErrorBoundary>
            <Transition
              name="fade"
              mode="out-in"
            >
              <component :is="Component" />
            </Transition>
          </ErrorBoundary>
        </router-view>
      </LayoutContent>
    </Layout>
  </Layout>
</template>

<style scoped>
/* ── 设计系统接入：Sider 使用 CSS 变量，不再硬编码颜色 ── */
.admin-root { height: var(--app-height); background: var(--bg-page); }

.admin-sider {
  background: var(--bg-card) !important;
  border-inline-end: var(--hairline-width) solid var(--border);
  position: relative;
  z-index: var(--z-dropdown);
}
.admin-sider :deep(.ant-layout-sider-children) { display: flex; flex-direction: column; }

/* 移动端抽屉：脱离布局，固定定位 */
.admin-sider--drawer {
  position: fixed !important;
  top: 0; left: 0; bottom: 0;
  transform: translateX(-100%);
  transition: transform var(--dur-normal) ease;
  z-index: var(--z-modal);
  box-shadow: var(--shadow-lg);
}
.admin-sider--drawer-open { transform: translateX(0); }
.admin-drawer-mask {
  position: fixed;
  inset: 0;
  background: var(--bg-overlay);
  z-index: var(--z-overlay);
}

/* 菜单滚动区 */
.sider-menu { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 8px 8px 16px; }
.sider-menu :deep(.ant-menu) {
  background: transparent !important;
  border: none !important;
}
.sider-menu :deep(.ant-menu-item-group-title) {
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--text-muted);
  padding: 14px 12px 4px;
}
.sider-menu :deep(.ant-menu-item) {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  line-height: 36px;
  margin: 2px 0;
  border-radius: var(--radius-md);
  font-size: 13px;
  color: var(--text-secondary);
}
.sider-menu :deep(.ant-menu-item:hover) {
  background: var(--bg-hover) !important;
  color: var(--text-primary) !important;
}
.sider-menu :deep(.ant-menu-item-selected) {
  background: var(--primary-bg) !important;
  color: var(--primary) !important;
  font-weight: var(--fw-semibold);
}
.sider-menu :deep(.ant-menu-item-selected::after) { display: none; }

/* ── Header ── */
.admin-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 16px 0 12px;
  height: 56px;
  background: var(--bg-card) !important;
  border-bottom: var(--hairline-width) solid var(--border);
  flex-shrink: 0;
}
.header-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.header-collapse-btn {
  flex-shrink: 0;
  width: 40px;
  height: 40px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
}
.header-collapse-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.header-breadcrumb { flex: 1; min-width: 0; }
.header-breadcrumb :deep(.ant-breadcrumb) {
  overflow: hidden;
  white-space: nowrap;
}
.header-breadcrumb :deep(.ant-breadcrumb-link) {
  font-size: 13px;
  color: var(--text-tertiary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.header-breadcrumb :deep(.ant-breadcrumb-link:last-child) {
  color: var(--text-primary);
  font-weight: var(--fw-medium);
}

/* 侧边栏底部用户入口：右上角不再有头像，登出/主题的可达路径下移到这里。 */
.sider-footer {
  flex-shrink: 0;
  padding: 8px;
  border-block-start: var(--hairline-width) solid var(--border);
}
.sider-user-btn {
  width: 100%;
  min-height: 36px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding-inline: 12px;
  border: 0;
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--text-secondary);
  font-size: 13px;
  cursor: pointer;
}
.sider-user-btn:hover { color: var(--text-primary); background: var(--bg-hover); }
.sider-user-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* ── Content ── */
.admin-content {
  padding: clamp(12px, 2.5vw, 24px);
  overflow: auto;
  background: var(--bg-page);
}

/* 过渡 */
.fade-enter-active, .fade-leave-active { transition: opacity var(--dur-normal) ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* 移动端抽屉菜单：菜单项提高至 40px 触控高度 */
@media (max-width: 960px) {
  .sider-menu :deep(.ant-menu-item) {
    height: 40px;
    line-height: 40px;
  }
}
</style>