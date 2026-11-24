<script setup lang="ts">
/**
 * 资源市场「外部源」配置面板。
 *
 * ## 产品决策（照做，不要在这里"优化"）
 *
 * 1. **初装默认关闭**：`remote_enabled` 默认 false，页面上必须明说"开启后本机将开始访问
 *    第三方 URL"。关闭时下方源列表仍然**可见但不可编辑**（disabled）—— 藏起来会让用户
 *    以为配置丢了，而可见只读能让人看清"关着的时候还有哪些源待启用"。
 * 2. **源总数硬上限 20**：显示 `已配置 N / 20`，满 20 就禁用「添加」。**不给**把上限调大
 *    的输入框 —— 20 是产品决策，不是配置项。
 * 3. `allow_user_sources` 默认关闭：说明文案必须点出「这是把 SSRF 面变成自助入口」，
 *    否则运维会以为它只是个便利开关。
 * 4. `allow_remote_mcp` 默认关闭：外部源默认只能提供目录数据。
 *
 * ## 键名即契约
 *
 * `remote_enabled` / `catalog_sources`（字符串数组）/ `allow_user_sources` /
 * `allow_remote_mcp` / `max_sources`。写错一个字符不会报错，只会静默不生效 ——
 * 所以读写都收敛在 `api/admin.ts` 的 `getMarketSettings` / `saveMarketSettings`，
 * 本组件只持有归一后的 `MarketSettingsConfig`。
 */
import { computed, inject, onBeforeUnmount, onMounted, ref } from 'vue'
import { Alert, Button, Input, Modal, Spin, Switch, Tag, message } from 'ant-design-vue'
import { DeleteOutlined } from '@ant-design/icons-vue'
import { onBeforeRouteLeave, routerKey } from 'vue-router'
import { useI18n } from 'vue-i18n'
import AdminPage from '@/components/admin/AdminPage.vue'
import {
  emptyMarketSettings,
  getMarketSettings,
  saveMarketSettings,
  type MarketSettingsConfig,
} from '../../api/admin'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
/** 读取失败的原文：有值时展示提示，但不是空白面板（沿用空态字段继续可编辑） */
const loadError = ref('')

/** 上次持久化的快照 —— 「未保存」标记与「重置」都以它为基准 */
const saved = ref<MarketSettingsConfig>(emptyMarketSettings())
/** 正在编辑的草稿 */
const draft = ref<MarketSettingsConfig>(emptyMarketSettings())

/** 添加框的输入与行内校验文案 */
const newSource = ref('')

/** 保存失败提示：优先 Error.message，取不到用兜底文案 */
function saveErrorText(err: unknown): string {
  return err instanceof Error && err.message ? err.message : t('errors.unknown_error')
}

/** 只认同 https：外部源要让本机发请求，明文 http 会把 URL 与内容暴露在链路上 */
function isHttps(url: string): boolean {
  return /^https:\/\/[^\s/]+/i.test(url.trim())
}

/** 已配置条数 >= 上限即满 */
const atLimit = computed(() => draft.value.catalog_sources.length >= draft.value.max_sources)

/** 非 https 的存量条数（后端可能存了旧数据 / 手工写入）—— 有值时禁止保存 */
const invalidCount = computed(
  () => draft.value.catalog_sources.filter(u => !isHttps(u)).length,
)

/**
 * 添加框当下的行内错误。空输入不报错（还没开始填，报错会像在指责用户）。
 * 返回 i18n 后的文案而不是 key：模板里直接渲染，避免漏掉插值。
 */
const newSourceError = computed(() => {
  const value = newSource.value.trim()
  if (!value) return ''
  if (!isHttps(value)) return t('admin.market.source_https_required')
  if (draft.value.catalog_sources.includes(value)) return t('admin.market.source_duplicate')
  return ''
})

/** 总开关关闭时下方整块只读 —— 顺序在这里定，模板不再重复判断 */
const fieldsDisabled = computed(() => !draft.value.remote_enabled)

/** 能否添加：总开关开着、未满、输入合法 */
const canAdd = computed(() => !fieldsDisabled.value && !atLimit.value && !newSourceError.value)

/** 「未保存」判据：只比业务字段，忽略 max_sources 这类派生值 */
function fingerprint(c: MarketSettingsConfig): string {
  return JSON.stringify({
    remote_enabled: c.remote_enabled,
    catalog_sources: c.catalog_sources,
    allow_user_sources: c.allow_user_sources,
    allow_remote_mcp: c.allow_remote_mcp,
  })
}
const dirty = computed(() => fingerprint(draft.value) !== fingerprint(saved.value))

/** 是否可保存：有改动、无非法地址、未在保存中 */
const canSave = computed(
  () => dirty.value && invalidCount.value === 0 && !saving.value && !loading.value,
)

function addSource() {
  if (!canAdd.value) return
  const value = newSource.value.trim()
  if (!value || draft.value.catalog_sources.includes(value)) return
  draft.value.catalog_sources = [...draft.value.catalog_sources, value]
  newSource.value = ''
}

function removeSource(index: number) {
  if (fieldsDisabled.value) return
  draft.value.catalog_sources = draft.value.catalog_sources.filter((_, i) => i !== index)
}

/** 重置只回滚到「上次保存」的配置，不重新拉后端 —— 否则会顺手丢掉用户的其它修改 */
function resetDraft() {
  draft.value = {
    ...saved.value,
    catalog_sources: [...saved.value.catalog_sources],
  }
  newSource.value = ''
}

async function load() {
  loading.value = true
  loadError.value = ''
  try {
    const config = await getMarketSettings()
    saved.value = config
    draft.value = { ...config, catalog_sources: [...config.catalog_sources] }
  } catch (err) {
    // 读不到时保留「全关」的空态：安全开关的未知态必须是关，不能是开
    loadError.value = saveErrorText(err)
  } finally {
    loading.value = false
  }
}

async function save() {
  if (!canSave.value) return
  saving.value = true
  try {
    await saveMarketSettings(draft.value)
    // 以提交值作为新基准（后端会把 max_sources 归一到同一值）
    saved.value = { ...draft.value, catalog_sources: [...draft.value.catalog_sources] }
    message.success(t('admin.market.saved'))
  } catch (err) {
    message.error(t('errors.save_failed_error', { error: saveErrorText(err) }))
  } finally {
    saving.value = false
  }
}

// ── 未保存时离开提醒 ───────────────────────────────────────────────
// 路由守卫只在真的有 router 时注册：本组件也会被单测直接 mount（无 router 上下文），
// 无条件调 onBeforeRouteLeave 会打一条 dev 警告，噪音掩盖真实问题。

/** 硬刷新/关窗口的兜底（路由守卫管不到 beforeunload） */
function onBeforeUnload(e: BeforeUnloadEvent) {
  if (!dirty.value) return
  e.preventDefault()
  // 现代浏览器只认 returnValue 非空，文案由浏览器决定
  e.returnValue = ''
}

const router = inject(routerKey, null)
if (router) {
  onBeforeRouteLeave(() => {
    if (!dirty.value) return true
    return new Promise<boolean>((resolve) => {
      Modal.confirm({
        title: t('admin.market.unsaved_leave_title'),
        content: t('admin.market.unsaved_leave_body'),
        okText: t('admin.market.leave_confirm'),
        cancelText: t('common.cancel'),
        onOk: () => resolve(true),
        onCancel: () => resolve(false),
      })
    })
  })
}

onMounted(() => {
  window.addEventListener('beforeunload', onBeforeUnload)
  void load()
})

onBeforeUnmount(() => {
  window.removeEventListener('beforeunload', onBeforeUnload)
})
</script>

<template>
  <AdminPage
    :title="$t('admin.market.page_title')"
    :description="$t('admin.market.page_desc')"
  >
    <Spin :spinning="loading">
      <Alert
        v-if="loadError"
        type="warning"
        show-icon
        class="mp-alert"
        data-test="market-load-failed"
        :message="t('admin.market.load_failed')"
        :description="loadError"
      >
        <template #action>
          <Button
            size="small"
            :loading="loading"
            @click="load"
          >
            {{ $t('common.retry') }}
          </Button>
        </template>
      </Alert>

      <!-- 总开关：默认关闭，且明确写出开启后的后果 -->
      <section class="mp-section">
        <div class="mp-master">
          <div class="mp-master-text">
            <div class="mp-master-label">
              {{ $t('admin.market.remote_enabled') }}
            </div>
            <p class="mp-master-hint">
              {{ $t('admin.market.remote_enabled_hint') }}
            </p>
          </div>
          <div class="mp-master-ctrl">
            <Tag
              v-if="dirty"
              color="orange"
              class="mp-unsaved"
            >
              {{ $t('common.unsaved') }}
            </Tag>
            <Switch
              v-model:checked="draft.remote_enabled"
              data-test="market-master"
              :aria-label="t('admin.market.remote_enabled')"
            />
          </div>
        </div>
        <Alert
          v-if="!draft.remote_enabled"
          type="info"
          show-icon
          class="mp-alert"
          :message="$t('admin.market.disabled_note')"
        />
      </section>

      <!-- 源列表：关闭时可见但不可编辑 -->
      <section
        class="mp-section"
        data-test="market-sources-section"
        :class="{ 'mp-section--disabled': fieldsDisabled }"
        :data-disabled="String(fieldsDisabled)"
      >
        <div class="mp-section-head">
          <h3 class="mp-section-title">
            {{ $t('admin.market.sources_list') }}
          </h3>
          <span
            class="mp-count"
            data-test="market-count"
          >
            {{ $t('admin.market.source_count', { n: draft.catalog_sources.length, max: draft.max_sources }) }}
          </span>
        </div>

        <ul class="mp-list">
          <li
            v-for="(url, index) in draft.catalog_sources"
            :key="`${index}-${url}`"
            class="mp-row"
            :class="{ 'mp-row--invalid': !isHttps(url) }"
          >
            <span class="mp-row-url">{{ url }}</span>
            <span
              v-if="!isHttps(url)"
              class="mp-error"
              data-test="market-row-error"
            >
              {{ $t('admin.market.source_https_required') }}
            </span>
            <Button
              class="mp-row-remove"
              size="small"
              type="text"
              danger
              data-test="market-remove"
              :disabled="fieldsDisabled"
              :aria-label="t('admin.market.source_remove')"
              :title="t('admin.market.source_remove')"
              @click="removeSource(index)"
            >
              <template #icon>
                <DeleteOutlined />
              </template>
            </Button>
          </li>
        </ul>
        <p
          v-if="!draft.catalog_sources.length"
          class="mp-empty"
          data-test="market-empty"
        >
          {{ $t('admin.market.sources_empty') }}
        </p>

        <div class="mp-add">
          <Input
            v-model:value="newSource"
            class="mp-add-input"
            data-test="market-add-input"
            :disabled="fieldsDisabled || atLimit"
            :status="newSourceError ? 'error' : undefined"
            :placeholder="t('admin.market.source_placeholder')"
            :aria-label="t('admin.market.source_placeholder')"
            @press-enter="addSource"
          />
          <Button
            type="primary"
            data-test="market-add-btn"
            :disabled="!canAdd"
            @click="addSource"
          >
            {{ $t('admin.market.source_add') }}
          </Button>
        </div>
        <!-- 行内校验优先于上限提示：先告诉用户"这条不对"，再说"加不进去了" -->
        <p
          v-if="newSourceError"
          class="mp-error"
          data-test="market-add-error"
        >
          {{ newSourceError }}
        </p>
        <p
          v-else-if="atLimit"
          class="mp-hint"
          data-test="market-limit"
        >
          {{ $t('admin.market.sources_limit_reached', { max: draft.max_sources }) }}
        </p>
      </section>

      <!-- 两个子开关：默认关闭，各自说明为什么 -->
      <section class="mp-section">
        <div class="mp-switch-row">
          <div class="mp-switch-text">
            <div class="mp-switch-label">
              {{ $t('admin.market.allow_user_sources') }}
            </div>
            <p class="mp-switch-hint">
              {{ $t('admin.market.allow_user_sources_hint') }}
            </p>
          </div>
          <Switch
            v-model:checked="draft.allow_user_sources"
            data-test="market-allow-user"
            :disabled="fieldsDisabled"
            :aria-label="t('admin.market.allow_user_sources')"
          />
        </div>
        <div class="mp-switch-row">
          <div class="mp-switch-text">
            <div class="mp-switch-label">
              {{ $t('admin.market.allow_remote_mcp') }}
            </div>
            <p class="mp-switch-hint">
              {{ $t('admin.market.allow_remote_mcp_hint') }}
            </p>
          </div>
          <Switch
            v-model:checked="draft.allow_remote_mcp"
            data-test="market-allow-mcp"
            :disabled="fieldsDisabled"
            :aria-label="t('admin.market.allow_remote_mcp')"
          />
        </div>
      </section>

      <div class="mp-actions">
        <Button
          type="primary"
          data-test="market-save"
          :loading="saving"
          :disabled="!canSave"
          @click="save"
        >
          {{ $t('common.save') }}
        </Button>
        <Button
          data-test="market-reset"
          :disabled="!dirty || saving"
          @click="resetDraft"
        >
          {{ $t('admin.market.reset') }}
        </Button>
        <span
          v-if="invalidCount"
          class="mp-error"
          data-test="market-invalid-count"
        >
          {{ $t('admin.market.source_invalid_count', { n: invalidCount }) }}
        </span>
      </div>
    </Spin>
  </AdminPage>
</template>

<style scoped>
.mp-alert { margin-bottom: 12px; }

.mp-section {
  padding: 12px 14px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
  background: var(--bg-card);
  margin-bottom: 12px;
  transition: opacity var(--dur-fast) ease;
}
/* 关闭态：整体压暗表示只读，但仍可读（藏起来会让人以为配置丢了） */
.mp-section--disabled { opacity: 0.7; }

.mp-master { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.mp-master-text { min-width: 0; }
.mp-master-label { font-weight: var(--fw-semibold); color: var(--text-primary); }
.mp-master-hint { margin: 4px 0 0; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }
.mp-master-ctrl { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.mp-unsaved { margin: 0; }

.mp-section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 8px; }
.mp-section-title { margin: 0; font-size: 13px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.mp-count { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }

.mp-list { list-style: none; margin: 0 0 8px; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.mp-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-md);
  background: var(--bg-page);
}
.mp-row--invalid { border-color: var(--error); }
.mp-row-url {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 12px;
  word-break: break-all;
  color: var(--text-primary);
  text-align: start;
}
.mp-row-remove { flex: 0 0 auto; }

.mp-empty { margin: 0 0 8px; font-size: 12px; color: var(--text-tertiary); }

.mp-add { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mp-add-input { flex: 1 1 240px; min-width: 0; }

.mp-error { margin: 6px 0 0; font-size: 12px; color: var(--error); line-height: 1.5; }
.mp-hint { margin: 6px 0 0; font-size: 12px; color: var(--text-secondary); line-height: 1.5; }

.mp-switch-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-block: 8px;
  border-block-start: var(--hairline-width) solid var(--border);
}
.mp-switch-row:first-child { padding-block-start: 0; border-block-start: none; }
.mp-switch-text { min-width: 0; }
.mp-switch-label { font-weight: var(--fw-medium); color: var(--text-primary); }
.mp-switch-hint { margin: 4px 0 0; font-size: 12px; line-height: 1.6; color: var(--text-secondary); }

.mp-actions { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }

@media (prefers-reduced-motion: reduce) {
  .mp-section { transition: none; }
}
</style>
