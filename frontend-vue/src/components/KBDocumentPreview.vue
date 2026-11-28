<template>
  <Modal
    v-model:visible="modalVisible"
    :title="$t('knowledge.document_preview')"
    :footer="null"
    :style="{ maxWidth: '800px' }"
    :destroy-on-close="true"
    @cancel="close"
  >
    <Spin :spinning="loading">
      <div class="doc-preview">
        <!-- 文档元信息 -->
        <div class="doc-meta">
          <span class="doc-name">{{ document?.name || $t('knowledge.unknown_document') }}</span>
          <span class="doc-type">{{ document?.file_type?.toUpperCase() }}</span>
          <span
            v-if="document?.file_size_bytes"
            class="doc-size"
          >{{ formatSize(document.file_size_bytes) }}</span>
          <Tag
            :color="docStatusColor"
            class="doc-status"
          >
            {{ document?.status }}
          </Tag>
        </div>

        <!-- 内容预览 -->
        <div
          v-if="content"
          class="doc-content"
        >
          <div class="content-header">
            <span class="content-label">{{ $t('knowledge.document_content_preview') }}</span>
            <span class="content-chunks">{{ $t('knowledge.n_pages', { n: pageCount }) }}</span>
          </div>
          <!-- G6：截断必须说出来。引擎侧只回前 1 万字符，用户若以为这是全文，
               就会拿"预览里没有"当"文档里没有"。 -->
          <p
            v-if="truncated"
            class="content-truncated"
          >
            {{ $t('knowledge.preview_truncated_note', { n: content.length }) }}
          </p>
          <pre class="content-body">{{ content }}</pre>
          <!-- G17：分页。内容区**不因"加载更多"而清空**（累积渲染，不闪烁）；
               某一页失败时只标记这一页，已经读到的正文原样留着。 -->
          <div
            v-if="!eof || moreError"
            class="content-more"
          >
            <p
              v-if="moreError"
              class="content-more-error"
            >
              {{ moreError }}
            </p>
            <button
              type="button"
              class="content-more-btn"
              :disabled="loadingMore"
              @click="loadMore"
            >
              {{ $t('knowledge.load_more') }}
            </button>
            <span
              v-if="loadingMore"
              class="content-more-loading"
            >…</span>
          </div>
        </div>

        <EmptyState
          v-else-if="!loading"
          size="list"
          :description="$t('common.no_content_preview_yet')"
          :hint="note || $t('errors.the_document_may_be_processing_or_its_format_is_not_previewable_yet')"
        />

        <!-- 失败信息 -->
        <Alert
          v-if="error"
          type="error"
          :message="error"
          closable
          style="margin-top: 12px"
        />
      </div>
    </Spin>
  </Modal>
</template>

<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { Modal, Spin, Tag, Alert } from 'ant-design-vue'
import EmptyState from './common/EmptyState.vue'
import { api } from '../api'
import { useI18n } from 'vue-i18n'
import { createKeyedGuard } from '../utils/staleGuard'

const { t } = useI18n()

interface Document {
  id: string
  name: string
  file_type: string
  file_size_bytes: number
  preview_truncated?: boolean
  status: string
  page_count?: number
  content?: string
  /** G17 分页游标：本页起点（字符偏移） */
  offset?: number
  /** G17：下一页起点。与 `content.length` 不完全等价（硬上限处会提前停） */
  next_offset?: number
  /** G17：没有下一页了。老后端没有这个字段 —— 按"没有下一页"处理 */
  eof?: boolean
  /** G17：全文长度（分页模式下才是真的全长） */
  total_chars?: number
  /** 后端给出的"为什么没有内容"（扫描件 / 无本机副本 / 格式不支持） */
  preview_note?: string
}

const props = withDefaults(defineProps<{
  visible: boolean
  documentId: string | null
  kbId: string
}>(), {
  visible: false,
  documentId: null,
})

const emit = defineEmits<{
  (e: 'update:visible', v: boolean): void
}>()

// v-model 不能直接绑定 prop，使用 computed 双向绑定
const modalVisible = computed({
  get: () => props.visible,
  set: (val: boolean) => emit('update:visible', val),
})

const loading = ref(false)
const content = ref('')
/** 后端给出的"为什么没有内容"（扫描件 / 云端文档 / 格式不支持），比通用文案可执行 */
const note = ref('')
// G6：引擎侧截断标记（见 python-engine `_PREVIEW_CHARS`）
const truncated = ref(false)
const error = ref('')
const document = ref<Document | null>(null)
const pageCount = ref(0)

/**
 * G17：预览分页。
 *
 * 修前：一次请求拿整块 content，引擎侧硬截在 1 万字符 —— 第 10000 个字符之后的正文
 * 在界面上没有任何入口。现在按 `offset`/`limit` 分页，按钮追加下一页。
 *
 * `PAGE_SIZE` 与引擎侧 `_PREVIEW_PAGE_LIMIT` 对齐（5000）：前端若报得比后端上限大，
 * 每页会被后端悄悄夹短，`next_offset` 就成了唯一可用的游标；报得小则多请求几次。
 * 两边同值最省心。
 */
const PAGE_SIZE = 5000
/** 下一页的起点（= 已加载内容的字符数；用后端给的 next_offset，不自己数） */
const nextOffset = ref(0)
/** 后端说没有下一页了。字段缺失（老后端）时按 true 处理 —— 不显示一个点了没反应的按钮 */
const eof = ref(true)
const loadingMore = ref(false)
/** "下一页"失败的原因。与首屏 `error` 分开：首屏失败是没内容可看，这里是"后面还有" */
const moreError = ref('')

const docStatusColor = computed(() => {
  const s = document.value?.status
  if (s === 'completed') return 'success'
  if (s === 'processing') return 'processing'
  if (s === 'error') return 'error'
  return 'default'
})

// G6：同时盯 `visible` 与 `documentId`，并 `immediate`。
//
// 修前只 watch `visible` 且无 immediate：① 父组件挂载时就是"打开"状态 ⇒ 永不加载，
// 面板停在"暂无内容"；② 打开状态下换文档 ⇒ 不发新请求（旧响应被代次票据挡住，
// 于是界面空白）。两个症状都表现为"预览是空的"，但原因完全不同。
// H60：预览按 (kb, document) 取。快速连续打开两个文档时，先发的那次可能**后**回来，
// 于是界面显示 A 的内容而标题是 B —— 用户还会据此复制引文。
const previewGuard = createKeyedGuard(() => `${props.kbId}\u0000${props.documentId}`)

watch(
  () => [props.visible, props.documentId] as const,
  async ([visible, documentId]) => {
    if (visible && documentId) {
      await loadPreview()
    }
  },
  { immediate: true },
)


function previewUrl(): string {
  return `/v1/kb/${props.kbId}/documents/${props.documentId}/preview`
}

/**
 * 从错误对象里抠出可读原因（后端 `detail` 优先）。首屏与"下一页"共用一份。
 *
 * 首屏失败与"下一页"失败的兜底文案不同：前者是"整篇看不到"，后者是
 * "已经读到的部分还在，只是后面这段没来" —— 同一句话会误导用户重开面板。
 */
function errorMessage(e: unknown, fallback: string = t('errors.failed_to_load_preview')): string {
  const data = (e as { response?: { data?: { detail?: unknown; error?: unknown } } }).response?.data
  const detail = typeof data?.detail === 'string' ? data.detail : ''
  const summary = typeof data?.error === 'string' ? data.error : ''
  return detail || summary || fallback
}

/**
 * 换文档 / 关闭时把分页游标归零。
 *
 * 不清会把上一篇的 offset 带过来：新文档的第 1 页请求直接落在 10000 之后，
 * 界面显示"暂无可预览内容" —— 而文档其实好好的。
 */
function resetPaging(): void {
  content.value = ''
  nextOffset.value = 0
  eof.value = true
  loadingMore.value = false
  moreError.value = ''
  truncated.value = false
}

/** 把一页响应落到状态上。`append=true` 是"加载更多"，否则是"从第一页重新开始"。 */
function applyPage(data: Document, append: boolean): void {
  const chunk = typeof data.content === 'string' ? data.content : ''
  content.value = append ? content.value + chunk : chunk
  const next = Number(data.next_offset)
  nextOffset.value = Number.isFinite(next) ? next : content.value.length
  // 老后端没有 eof 字段：按"没有下一页"处理，不给一个点了没反应的按钮
  eof.value = typeof data.eof === 'boolean' ? data.eof : true
  truncated.value = Boolean(data.preview_truncated)
}

async function loadPreview() {
  if (!props.documentId) return
  const ticket = previewGuard.ticket(`${props.kbId}\u0000${props.documentId}`)
  loading.value = true
  resetPaging()
  note.value = ''
  error.value = ''

  try {
    const res = await api.get(previewUrl(), { params: { offset: 0, limit: PAGE_SIZE } })
    if (!previewGuard.isValid(ticket)) return
    const data = res.data?.data || res.data
    document.value = data
    applyPage(data, false)
    note.value = data.preview_note || ''
    pageCount.value = data.page_count || 0
  } catch (e) {
    if (!previewGuard.isValid(ticket)) return
    error.value = errorMessage(e)
  } finally {
    // 只清自己那一代的 loading：否则会把新文档的加载态清掉（表现为"点了没反应"）
    if (previewGuard.isValid(ticket)) loading.value = false
  }
}

/**
 * 追加下一页。
 *
 * 失败**不清 content**：用户已经读到的正文是有效信息，一段加载失败把它清掉，
 * 等于"网差一次就得从头再读"。这里只记下 `moreError`，按钮留在原地当重试用。
 */
async function loadMore() {
  if (!props.documentId || loadingMore.value) return
  const ticket = previewGuard.ticket(`${props.kbId}\u0000${props.documentId}`)
  loadingMore.value = true
  moreError.value = ''

  try {
    const res = await api.get(previewUrl(), {
      params: { offset: nextOffset.value, limit: PAGE_SIZE },
    })
    if (!previewGuard.isValid(ticket)) return
    const data = res.data?.data || res.data
    applyPage(data, true)
    // 备注（扫描件/无原文）可能只在某一页出现：别把已有的说明覆盖成空串
    if (data.preview_note) note.value = data.preview_note
  } catch (e) {
    if (!previewGuard.isValid(ticket)) return
    moreError.value = errorMessage(e, t('knowledge.load_more_failed'))
  } finally {
    if (previewGuard.isValid(ticket)) loadingMore.value = false
  }
}

function close() {
  // 关闭即重置：下次打开同一篇文档必须从第一页开始渲染，否则用户看到的是
  // 一段"没有开头的内容"（游标还停在上次的 offset 上）。
  resetPaging()
  note.value = ''
  error.value = ''
  emit('update:visible', false)
}

function formatSize(bytes: number): string {
  if (!bytes) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}
</script>

<style scoped>
.doc-preview {
  padding: 4px 0;
}
.doc-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}
.doc-name {
  font-weight: var(--fw-semibold);
  font-size: var(--fs-md);
  color: var(--text-primary);
}
.doc-type {
  padding: 2px 8px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  background: var(--bg-secondary);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
}
.doc-size {
  font-size: var(--fs-sm);
  color: var(--text-tertiary);
}
.doc-content {
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-lg);
  overflow: hidden;
}
.content-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  background: var(--bg-secondary);
  border-bottom: var(--hairline-width) solid var(--border-card);
  font-size: var(--fs-sm);
}
.content-label {
  font-weight: var(--fw-medium);
  color: var(--text-primary);
}
.content-chunks {
  color: var(--text-tertiary);
  font-size: var(--fs-sm);
}
.content-truncated {
  margin: 6px 0 0;
  font-size: var(--fs-sm);
  color: var(--text-secondary, #8a8f98);
}

.content-body {
  margin: 0;
  padding: 14px;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow-y: auto;
  background: var(--bg-card);
}
/* G17：分页控件与正文分开一条边界 —— 按钮是"继续读"，不是正文的一部分 */
.content-more {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 10px 14px;
  border-block-start: var(--hairline-width) solid var(--border-card);
  background: var(--bg-secondary);
}
.content-more-error {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--color-danger, var(--text-secondary));
}
.content-more-btn {
  padding: 4px 14px;
  font-size: var(--fs-sm);
  font-weight: var(--fw-medium);
  color: var(--text-primary);
  background: var(--bg-card);
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-sm);
  cursor: pointer;
}
.content-more-btn:hover:not(:disabled) {
  border-color: var(--color-primary, var(--border-card));
}
.content-more-btn:disabled {
  cursor: progress;
  opacity: 0.6;
}
.content-more-loading {
  font-size: var(--fs-sm);
  color: var(--text-tertiary);
}
</style>