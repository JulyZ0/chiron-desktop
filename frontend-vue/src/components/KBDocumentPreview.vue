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


async function loadPreview() {
  if (!props.documentId) return
  const ticket = previewGuard.ticket(`${props.kbId}\u0000${props.documentId}`)
  loading.value = true
  content.value = ''
  note.value = ''
  error.value = ''

  try {
    const res = await api.get(`/v1/kb/${props.kbId}/documents/${props.documentId}/preview`)
    if (!previewGuard.isValid(ticket)) return
    const data = res.data?.data || res.data
    document.value = data
    content.value = data.content || ''
    note.value = data.preview_note || ''
    truncated.value = Boolean(data.preview_truncated)
    pageCount.value = data.page_count || 0
  } catch (e) {
    if (!previewGuard.isValid(ticket)) return
    const data = (e as { response?: { data?: { detail?: unknown; error?: unknown } } }).response?.data
    const detail = typeof data?.detail === 'string' ? data.detail : ''
    const summary = typeof data?.error === 'string' ? data.error : ''
    error.value = detail || summary || t('errors.failed_to_load_preview')
  } finally {
    // 只清自己那一代的 loading：否则会把新文档的加载态清掉（表现为"点了没反应"）
    if (previewGuard.isValid(ticket)) loading.value = false
  }
}

function close() {
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
  font-size: 15px;
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
  font-size: 13px;
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
  font-size: 13px;
}
.content-label {
  font-weight: var(--fw-medium);
  color: var(--text-primary);
}
.content-chunks {
  color: var(--text-tertiary);
  font-size: 12px;
}
.content-truncated {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--text-secondary, #8a8f98);
}

.content-body {
  margin: 0;
  padding: 14px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 400px;
  overflow-y: auto;
  background: var(--bg-card);
}
</style>