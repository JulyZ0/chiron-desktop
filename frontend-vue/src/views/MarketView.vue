<script setup lang="ts">
/**
 * 资源市场（/market）。
 *
 * 页面结构是「源 → 类型 → 条目」三级，顺序不能颠倒：
 *  1) **源是最外层 Tab** —— `(source, id)` 才是条目主键。同名条目在不同源是不同东西，
 *     把多源条目混排会让用户不知道自己装的是哪一个。内置源恒排第一且默认选中：
 *     它随二进制发布、不需要联网，是外部源全挂时页面仍有内容的底线。
 *  2) 四类资源用分段控件（agent / skill / knowledge / mcp），切换只改查询参数，不切路由。
 *  3) 条目卡片必须能一眼看出「从哪来、有没有签名、装没装」—— 这三件事决定了用户接下来
 *     要不要点安装。
 *
 * 两个产品口径直接写在交互里：
 *  - **签名不强制**：`signed === false` 只显示徽标 + 一句提醒，安装按钮照样可用。
 *    内置条目同样 `signed === false`，但**不显示**未签名警示 —— 它随二进制发布，
 *    签名没有额外信息量（见 internal/market/catalog.go 的 Item.Signed 注释）。
 *  - **装完默认禁用**：外部源条目装成功 ≠ 可用。后端返回 `enabled: false` 时必须明确告诉
 *    用户「去对应页面启用」，否则会被当成装坏了。
 *
 * 凭据只走内存：`requires.env` 的值存在组件内的 `detailEnv`，只随安装请求提交一次，
 * 不进 localStorage、不写任何持久化层。
 */
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  Alert, Button, Drawer, Input, Modal, Pagination, Segmented, Select, TabPane, Tabs, Tag, message,
} from 'ant-design-vue'
import {
  AppstoreOutlined, ReloadOutlined, SearchOutlined,
} from '@ant-design/icons-vue'
import PageSkeleton from '../components/common/PageSkeleton.vue'
import EmptyState from '../components/common/EmptyState.vue'
import {
  BUILTIN_SOURCE_ID,
  MARKET_KINDS,
  describeMarketError,
  getMarketItem,
  installMarketItem,
  listInstalledMarketItems,
  listMarketCatalog,
  marketItemKey,
  marketWarningKey,
  sortSourcesWithBuiltinFirst,
  uninstallMarketItem,
  type MarketInstalledItem,
  type MarketItem,
  type MarketKind,
  type MarketSource,
  type MarketWarning,
} from '../api/market'
import { hasMessage } from '../i18n'

import { useI18n } from 'vue-i18n'
const { t: tr } = useI18n()

// ── 状态 ──

const loading = ref(true)
const loadFailed = ref(false)

const sources = ref<MarketSource[]>([])
const items = ref<MarketItem[]>([])
const warnings = ref<MarketWarning[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(12)

/** 当前选中的源。默认内置源 —— 它是唯一「不依赖网络」的分组。 */
const activeSource = ref<string>(BUILTIN_SOURCE_ID)
/** 当前类型；`all` 不传给后端（后端把空 kind 当"全部"）。 */
const activeKind = ref<MarketKind | 'all'>('all')
const searchQuery = ref('')
const tagFilter = ref<string | undefined>(undefined)
const tagOptions = ref<string[]>([])

/** 已安装条目：`source/kind/id` → 安装记录（含 enabled，决定卡片上的标签）。 */
const installedMap = ref<Map<string, MarketInstalledItem>>(new Map())

/** 告警条的关闭状态：同一 (source, reason) 只显示一次，关掉后本轮不再冒出来。 */
const dismissedWarnings = ref<Set<string>>(new Set())

/** 正在安装/卸载的条目 key —— 只让被点的那张卡片转圈。 */
const installingKey = ref('')

// ── 详情抽屉 ──

const detailOpen = ref(false)
const detailLoading = ref(false)
const detailItem = ref<MarketItem | null>(null)
/**
 * `requires.env` 的值：**只在内存里**。
 * 不写 localStorage、不进 store、不在组件间传递 —— 凭据落盘与"用户以为填过"都是事故。
 */
const detailEnv = reactive<Record<string, string>>({})

// ── 派生 ──

/**
 * 源 Tab 的数据。
 *
 * 本仓 ant-design-vue 4.2.6 的 `ATabs` **不读** `items` / `tabs` 这类 prop：
 * 它在 render 里无条件用 `slots.default` 里的 `TabPane` 覆盖 `tabs`
 * （`es/tabs/src/Tabs.js` 的 `ATabs.setup` → `"tabs": tabs`）。
 * 所以源 Tab 必须写成 `<TabPane>` 子元素；传 prop 不会报错，只会被当普通 attribute
 * 挂到根节点上、**一个 tab 都不渲染**，属于最难查的静默失效。
 */
const sourceTabs = computed(() =>
  sources.value.map(s => ({
    key: s.id,
    tab: s.id === BUILTIN_SOURCE_ID ? tr('market.source.builtin') : (s.name || s.id),
  })),
)

const kindOptions = computed(() => [
  { label: tr('market.kind.all'), value: 'all' },
  ...MARKET_KINDS.map(k => ({ label: tr(`market.kind.${k}`), value: k })),
])

const tagSelectOptions = computed(() =>
  tagOptions.value.map(tag => ({ label: tag, value: tag })),
)

const activeSourceInfo = computed<MarketSource | undefined>(() =>
  sources.value.find(s => s.id === activeSource.value),
)

const visibleWarnings = computed(() =>
  warnings.value.filter(w => !dismissedWarnings.value.has(marketWarningKey(w))),
)

const isEmpty = computed(() => !loading.value && !loadFailed.value && items.value.length === 0)

// ── 取数 ──

async function loadCatalog() {
  loading.value = true
  loadFailed.value = false
  try {
    const res = await listMarketCatalog({
      source: activeSource.value || undefined,
      kind: activeKind.value === 'all' ? undefined : activeKind.value,
      q: searchQuery.value.trim() || undefined,
      tag: tagFilter.value || undefined,
      page: page.value,
      page_size: pageSize.value,
    })
    // 内置源恒排第一：后端顺序可能是配置顺序，排序口径属于前端产品约定
    sources.value = sortSourcesWithBuiltinFirst(res.sources)
    items.value = res.items
    total.value = res.total
    warnings.value = res.warnings

    // 当前选中的源消失了（被删/被停用且不返回）→ 回到第一项，避免空 Tab 卡死
    if (sources.value.length && !sources.value.some(s => s.id === activeSource.value)) {
      activeSource.value = sources.value[0].id
    }

    // 标签选项随当前结果收敛；已选值始终保留，否则清空筛选后下拉里找不到自己
    const nextTags = new Set<string>()
    for (const it of res.items) for (const tag of it.tags ?? []) nextTags.add(tag)
    if (tagFilter.value) nextTags.add(tagFilter.value)
    tagOptions.value = [...nextTags].sort((a, b) => a.localeCompare(b))
  } catch (e) {
    loadFailed.value = true
    items.value = []
    total.value = 0
    message.error(describeMarketError(e, 'catalog'))
  } finally {
    loading.value = false
  }
}

async function loadInstalled() {
  try {
    const list = await listInstalledMarketItems()
    const next = new Map<string, MarketInstalledItem>()
    for (const it of list) next.set(marketItemKey(it), it)
    installedMap.value = next
  } catch {
    // 已装列表拉取失败只影响「已安装」标签，不拖垮市场目录本身
    installedMap.value = new Map()
  }
}

function refresh() {
  void loadCatalog()
  void loadInstalled()
}

onMounted(() => {
  void loadCatalog()
  void loadInstalled()
})

// 搜索防抖：不加防抖会每敲一个字符打一次目录请求（外部源还会被真的去抓）
let searchTimer: ReturnType<typeof setTimeout> | undefined
function onSearch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    page.value = 1
    void loadCatalog()
  }, 300)
}
onUnmounted(() => {
  if (searchTimer) clearTimeout(searchTimer)
})

// ── 交互：筛选 ──

function onSourceChange(key: string | number) {
  const next = String(key)
  if (next === activeSource.value) return
  activeSource.value = next
  // 换源等于换一批条目：页码与告警的关闭状态都不该带过去
  page.value = 1
  dismissedWarnings.value = new Set()
  void loadCatalog()
}

function onKindChange(value: string | number) {
  const next = String(value) as MarketKind | 'all'
  if (next === activeKind.value) return
  activeKind.value = next
  page.value = 1
  void loadCatalog()
}

function onTagChange() {
  page.value = 1
  void loadCatalog()
}

function onPageChange() {
  void loadCatalog()
}

function dismissWarning(w: MarketWarning) {
  const next = new Set(dismissedWarnings.value)
  next.add(marketWarningKey(w))
  dismissedWarnings.value = next
}

// ── 展示辅助 ──

function sourceOf(id: string): MarketSource | undefined {
  return sources.value.find(s => s.id === id)
}

/** 源名：内置源用本地化固定名，外部源用后端给的 name（可重名，故旁边始终有类型徽标）。 */
function sourceLabel(id: string): string {
  if (id === BUILTIN_SOURCE_ID) return tr('market.source.builtin')
  return sourceOf(id)?.name || id
}

function sourceTypeLabel(id: string): string {
  const type = sourceOf(id)?.type ?? 'user'
  return tr(`market.source.type_${type}`)
}

/**
 * 是否显示「未签名」警示。
 *
 * 内置条目 `signed === false` 是**正常状态**（随二进制发布，没有发布方签名这个概念），
 * 给它挂未签名徽标会让首屏每个条目都带一条警告 —— 警报疲劳之后真的未签名条目也被忽略。
 */
function isUnsigned(it: MarketItem): boolean {
  return !it.signed && it.source !== BUILTIN_SOURCE_ID
}

function isInstalled(it: { source: string; kind: MarketKind; id: string }): boolean {
  return installedMap.value.has(marketItemKey(it))
}

function installedEnabled(it: { source: string; kind: MarketKind; id: string }): boolean {
  return installedMap.value.get(marketItemKey(it))?.enabled === true
}

/**
 * 告警文案。`reason` 是稳定机器码，一律本地化 —— 后端 `detail` 里带 URL 和英文异常，
 * 直接展示等于把内部实现甩给用户。
 *
 * 需要 `{n}` 的原因码（条目被截断、未签名计数）从 detail 里取第一个数字；
 * 取不到就退回通用文案 —— vue-i18n 缺参会把 `{n}` 原样渲染出来（`market.unsigned_badge`
 * 那种半成品体验必须避免）。
 */
const REASONS_NEEDING_N = new Set(['unsigned_items', 'items_truncated'])

function warningText(w: MarketWarning): string {
  const source = sourceLabel(w.source)
  const params: Record<string, unknown> = { source }
  const digits = /(\d+)/.exec(w.detail ?? '')
  if (digits) params.n = digits[1]
  if (REASONS_NEEDING_N.has(w.reason) && params.n === undefined) {
    return tr('market.warn.generic', { source })
  }
  const key = `market.warn.${w.reason}`
  return hasMessage(key) ? tr(key, params) : tr('market.warn.generic', { source })
}

// ── 详情抽屉 ──

function resetDetailEnv() {
  for (const key of Object.keys(detailEnv)) delete detailEnv[key]
}

async function openDetail(it: MarketItem) {
  detailItem.value = it
  detailOpen.value = true
  resetDetailEnv()
  for (const key of it.requires?.env ?? []) detailEnv[key] = ''

  // 抽屉里的字段以单条详情为准（列表可能被分页裁剪或来自旧快照）
  detailLoading.value = true
  try {
    const detail = await getMarketItem(it.source, it.id)
    detailItem.value = detail.item
    detailOpen.value = true
    for (const key of detail.item.requires?.env ?? []) {
      if (detailEnv[key] === undefined) detailEnv[key] = ''
    }
    // 详情自带的 warnings 与目录级告警有重叠：不覆盖页面顶部的告警条，
    // 否则打开一次抽屉就把「某个源整体挂了」这条信息从页面上抹掉。
  } catch {
    // 详情拉取失败不关闭抽屉：列表里那份数据足以展示名称/描述，只是可能不是最新的
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detailOpen.value = false
  detailItem.value = null
  resetDetailEnv()
}

// ── 安装 / 卸载 ──

/** 需要填凭据的条目：先让用户把值填了，否则装上去一定跑不起来。 */
function askInstall(it: MarketItem) {
  if (it.requires?.env?.length) {
    void openDetail(it)
    return
  }
  confirmInstall(it, {})
}

function confirmInstall(it: MarketItem, env: Record<string, string>) {
  Modal.confirm({
    title: tr('market.install_confirm_title', { name: it.name }),
    content: tr('market.install_confirm_hint'),
    okText: tr('market.install'),
    cancelText: tr('common.cancel'),
    onOk: () => doInstall(it, env),
  })
}

async function doInstall(it: MarketItem, env: Record<string, string>) {
  installingKey.value = marketItemKey(it)
  try {
    const res = await installMarketItem({ source: it.source, kind: it.kind, id: it.id, env })
    await loadInstalled()

    if (res.already_installed) {
      message.info(tr('market.already_installed'))
      return
    }
    message.success(tr('market.install_success', { name: it.name }))
    // 装成功 ≠ 可用：外部源条目默认禁用，必须告诉用户去哪启用
    if (res.enabled === false) message.warning(tr('market.installed_disabled_hint'))
    if (res.requires_approval) message.info(tr('market.detail.requires_approval'))
  } catch (e) {
    message.error(describeMarketError(e, 'install'))
  } finally {
    installingKey.value = ''
  }
}

function askUninstall(it: MarketItem) {
  Modal.confirm({
    title: tr('market.uninstall_confirm_title', { name: it.name }),
    okText: tr('market.uninstall'),
    okButtonProps: { danger: true },
    cancelText: tr('common.cancel'),
    onOk: () => doUninstall(it),
  })
}

async function doUninstall(it: MarketItem) {
  installingKey.value = marketItemKey(it)
  try {
    await uninstallMarketItem(it.source, it.kind, it.id)
    await loadInstalled()
    message.success(tr('market.uninstall_success', { name: it.name }))
  } catch (e) {
    message.error(describeMarketError(e, 'uninstall'))
  } finally {
    installingKey.value = ''
  }
}

/** 抽屉里的安装：把当前表单里的凭据一起提交。 */
function installFromDetail() {
  const it = detailItem.value
  if (!it) return
  confirmInstall(it, { ...detailEnv })
}
</script>

<template>
  <div class="market-view">
    <div class="page-header">
      <div class="header-text">
        <h2 class="page-title">
          {{ $t('market.title') }}
        </h2>
        <p class="page-subtitle">
          {{ $t('market.subtitle') }}
        </p>
      </div>
      <Button
        :loading="loading"
        @click="refresh"
      >
        <template #icon>
          <ReloadOutlined />
        </template>
        {{ $t('common.refresh') }}
      </Button>
    </div>

    <!-- 非致命告警：单源失败不能让整页变红，但也不能静默 -->
    <div
      v-if="visibleWarnings.length"
      class="market-warnings"
    >
      <Alert
        v-for="w in visibleWarnings"
        :key="marketWarningKey(w)"
        type="warning"
        show-icon
        closable
        :message="warningText(w)"
        @close="dismissWarning(w)"
      />
    </div>

    <!-- 源 Tab：内置源恒排第一且默认选中。必须写成 TabPane 子元素（见 sourceTabs 注释） -->
    <Tabs
      v-if="sourceTabs.length"
      :active-key="activeSource"
      class="source-tabs"
      @change="onSourceChange(($event as any))"
    >
      <!-- 面板内容在 Tabs 之外渲染：换源只改查询参数，不需要为每个源复制一份 DOM。
           但 TabPane 子元素仍是必需的 —— ATabs 只从 slots 里收集 tab。 -->
      <TabPane
        v-for="s in sourceTabs"
        :key="s.key"
        :tab="s.tab"
      />
    </Tabs>

    <!-- 当前源的可观测性：坏掉的源要能说清是「停用」「拉取失败」还是「用的是旧快照」 -->
    <div
      v-if="activeSourceInfo"
      class="source-status"
    >
      <Tag
        v-if="!activeSourceInfo.enabled"
        color="default"
      >
        {{ $t('market.source.disabled_source') }}
      </Tag>
      <Tag
        v-if="activeSourceInfo.last_error"
        color="error"
      >
        {{ $t('market.source.error_source') }}
      </Tag>
      <Tag
        v-if="activeSourceInfo.stale"
        color="warning"
      >
        {{ $t('market.source.stale_source') }}
      </Tag>
      <Tag>{{ $t('market.source.count', { n: activeSourceInfo.count }) }}</Tag>
    </div>

    <div class="market-toolbar">
      <Segmented
        :value="activeKind"
        :options="kindOptions"
        @change="onKindChange(($event as any))"
      />
      <div class="toolbar-right">
        <Input
          v-model:value="searchQuery"
          allow-clear
          class="search-input"
          :placeholder="$t('market.search_placeholder')"
          @change="onSearch"
        >
          <template #prefix>
            <SearchOutlined />
          </template>
        </Input>
        <Select
          v-model:value="tagFilter"
          allow-clear
          class="tag-filter"
          :options="tagSelectOptions"
          :placeholder="$t('common.tag')"
          @change="onTagChange"
        />
      </div>
    </div>

    <PageSkeleton
      v-if="loading"
      variant="cards"
      :columns="3"
      :rows="4"
      :header="false"
    />
    <EmptyState
      v-else-if="loadFailed"
      size="page"
      :icon="AppstoreOutlined"
      :description="$t('market.load_failed')"
      :hint="$t('market.load_failed_hint')"
    >
      <Button
        type="primary"
        @click="refresh"
      >
        {{ $t('common.retry') }}
      </Button>
    </EmptyState>
    <EmptyState
      v-else-if="isEmpty"
      size="page"
      :icon="AppstoreOutlined"
      :description="$t('market.empty.title')"
      :hint="$t('market.empty.hint')"
    />

    <div
      v-else
      class="market-grid stagger-in"
    >
      <div
        v-for="it in items"
        :key="marketItemKey(it)"
        class="market-card"
        role="button"
        tabindex="0"
        data-testid="market-card"
        @click="openDetail(it)"
        @keydown.enter.prevent="openDetail(it)"
        @keydown.space.prevent="openDetail(it)"
      >
        <div class="card-top">
          <span class="card-name">{{ it.name }}</span>
          <Tag
            v-if="it.version"
            class="version-tag"
          >
            v{{ it.version }}
          </Tag>
        </div>

        <p class="card-desc">
          {{ it.description || $t('market.detail.no_description') }}
        </p>

        <div
          v-if="it.tags?.length"
          class="card-meta"
        >
          <Tag
            v-for="tag in (it.tags || []).slice(0, 3)"
            :key="tag"
          >
            {{ tag }}
          </Tag>
        </div>

        <div class="card-source">
          <Tag color="blue">
            {{ sourceLabel(it.source) }}
          </Tag>
          <Tag>{{ sourceTypeLabel(it.source) }}</Tag>
          <Tag
            v-if="isUnsigned(it)"
            color="warning"
            data-testid="market-unsigned-badge"
          >
            {{ $t('market.unsigned_badge') }}
          </Tag>
          <Tag
            v-if="isInstalled(it)"
            :color="installedEnabled(it) ? 'green' : 'default'"
          >
            {{ installedEnabled(it) ? $t('market.installed_badge') : $t('market.installed_disabled_badge') }}
          </Tag>
        </div>

        <!-- 未签名只提醒，不阻止安装 -->
        <p
          v-if="isUnsigned(it)"
          class="unsigned-hint"
        >
          {{ $t('market.unsigned_hint') }}
        </p>

        <div class="card-actions">
          <Button
            type="text"
            size="small"
            @click.stop="openDetail(it)"
          >
            {{ $t('common.view_details') }}
          </Button>
          <Button
            v-if="!isInstalled(it)"
            type="primary"
            size="small"
            data-testid="market-install"
            :loading="installingKey === marketItemKey(it)"
            @click.stop="askInstall(it)"
          >
            {{ $t('market.install') }}
          </Button>
          <Button
            v-else
            danger
            size="small"
            data-testid="market-uninstall"
            :loading="installingKey === marketItemKey(it)"
            @click.stop="askUninstall(it)"
          >
            {{ $t('market.uninstall') }}
          </Button>
        </div>
      </div>
    </div>

    <div
      v-if="!loading && total > pageSize"
      class="market-pagination"
    >
      <Pagination
        v-model:current="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-size-options="['12', '24', '48']"
        show-size-changer
        @change="onPageChange"
      />
    </div>

    <!-- 详情抽屉 -->
    <Drawer
      :open="detailOpen"
      :width="420"
      :title="$t('market.detail.title')"
      @close="closeDetail"
    >
      <div
        v-if="detailItem"
        class="detail-body"
      >
        <div class="detail-head">
          <div class="card-top">
            <span class="detail-name">{{ detailItem.name }}</span>
            <Tag v-if="detailItem.version">
              v{{ detailItem.version }}
            </Tag>
          </div>
          <div class="card-source">
            <Tag color="blue">
              {{ sourceLabel(detailItem.source) }}
            </Tag>
            <Tag>{{ sourceTypeLabel(detailItem.source) }}</Tag>
            <Tag
              v-if="isUnsigned(detailItem)"
              color="warning"
            >
              {{ $t('market.unsigned_badge') }}
            </Tag>
          </div>
          <p
            v-if="isUnsigned(detailItem)"
            class="unsigned-hint"
          >
            {{ $t('market.unsigned_hint') }}
          </p>
          <p class="detail-desc">
            {{ detailItem.description || $t('market.detail.no_description') }}
          </p>
        </div>

        <div class="detail-section">
          <div
            v-if="detailItem.author"
            class="detail-row"
          >
            <span class="detail-label">{{ $t('market.detail.author') }}</span>
            <span class="detail-value">{{ detailItem.author }}</span>
          </div>
          <div
            v-if="detailItem.tags?.length"
            class="detail-row"
          >
            <span class="detail-label">{{ $t('common.tag') }}</span>
            <span class="detail-value">
              <Tag
                v-for="tag in detailItem.tags"
                :key="tag"
                class="detail-tag"
              >
                {{ tag }}
              </Tag>
            </span>
          </div>
          <div
            v-if="detailItem.homepage"
            class="detail-row"
          >
            <span class="detail-label">{{ $t('market.detail.homepage') }}</span>
            <a
              class="detail-value"
              :href="detailItem.homepage"
              target="_blank"
              rel="noopener noreferrer"
            >{{ detailItem.homepage }}</a>
          </div>
          <div
            v-if="detailItem.manifest_url"
            class="detail-row"
          >
            <span class="detail-label">{{ $t('market.detail.manifest_url') }}</span>
            <a
              class="detail-value"
              :href="detailItem.manifest_url"
              target="_blank"
              rel="noopener noreferrer"
            >{{ detailItem.manifest_url }}</a>
          </div>
        </div>

        <!-- 前提条件：只提示，不在这里替用户做别的动作 -->
        <div
          v-if="detailItem.requires?.model || detailItem.requires?.approval"
          class="detail-section"
        >
          <Alert
            v-if="detailItem.requires?.model"
            type="info"
            show-icon
            :message="$t('market.detail.requires_model')"
          />
          <Alert
            v-if="detailItem.requires?.approval"
            type="info"
            show-icon
            :message="$t('market.detail.requires_approval')"
          />
        </div>

        <!-- 凭据：值只在本组件内存里，随安装请求提交一次 -->
        <div
          v-if="detailItem.requires?.env?.length"
          class="detail-section"
        >
          <span class="detail-label">{{ $t('market.detail.requires_env') }}</span>
          <div
            v-for="key in detailItem.requires.env"
            :key="key"
            class="env-field"
          >
            <span class="env-key">{{ key }}</span>
            <Input
              v-model:value="detailEnv[key]"
              :placeholder="key"
              autocomplete="off"
            />
          </div>
          <p class="env-hint">
            {{ $t('market.detail.requires_env_hint') }}
          </p>
        </div>

        <div class="detail-actions">
          <Button @click="closeDetail">
            {{ $t('common.close') }}
          </Button>
          <Button
            v-if="!isInstalled(detailItem)"
            type="primary"
            :loading="installingKey === marketItemKey(detailItem)"
            @click="installFromDetail"
          >
            {{ $t('market.install') }}
          </Button>
          <Button
            v-else
            danger
            :loading="installingKey === marketItemKey(detailItem)"
            @click="askUninstall(detailItem)"
          >
            {{ $t('market.uninstall') }}
          </Button>
        </div>
      </div>
      <PageSkeleton
        v-else-if="detailLoading"
        variant="detail"
        :header="false"
      />
    </Drawer>
  </div>
</template>

<style scoped>
.market-view {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 4px 2px 32px;
}

.header-text { min-width: 0; }
.page-subtitle {
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  line-height: 1.5;
}

.market-warnings {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.source-status {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.market-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.search-input { width: 240px; }
.tag-filter { min-width: 140px; }

.market-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.market-card {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 16px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  box-shadow: var(--shadow-md);
  cursor: pointer;
  transition:
    transform var(--dur-normal) ease,
    border-color var(--dur-normal) ease,
    box-shadow var(--dur-normal) ease;
}
.market-card:hover {
  transform: translateY(-2px);
  border-color: var(--primary);
  box-shadow: var(--shadow-lg);
}
.market-card:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}

.card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  min-width: 0;
}
.card-name {
  font-size: 14px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.version-tag { flex: none; }

.card-desc {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.card-meta,
.card-source {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.unsigned-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--warning);
  line-height: 1.5;
}

.card-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  margin-top: auto;
}

.market-pagination {
  display: flex;
  justify-content: flex-end;
}

/* ── 抽屉 ── */
.detail-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.detail-head {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.detail-name {
  font-size: 15px;
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.detail-desc {
  margin: 0;
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
  white-space: pre-wrap;
}
.detail-section {
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-top: var(--hairline-width) solid var(--border);
  padding-top: 12px;
}
.detail-row {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.detail-label {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  font-weight: var(--fw-medium);
}
.detail-value {
  font-size: 13px;
  color: var(--text-primary);
  word-break: break-word;
}
.detail-tag { margin-inline-end: 4px; }

.env-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.env-key {
  font-size: var(--fs-xs);
  font-family: var(--font-mono, monospace);
  color: var(--text-secondary);
}
.env-hint {
  margin: 0;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  line-height: 1.5;
}

.detail-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 8px;
}
</style>
