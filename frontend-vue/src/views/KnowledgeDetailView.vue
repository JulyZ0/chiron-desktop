<script setup lang="ts">
import { ref, onMounted, computed, markRaw } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  Card, Button, Upload, Table, Spin, Empty, Tag, Space, Switch,
  Progress, message, Modal, Input, Checkbox, Popconfirm,
} from 'ant-design-vue'
import {
  ArrowLeftOutlined, CloudUploadOutlined, DeleteOutlined, FolderOutlined,
  PlayCircleOutlined, PictureOutlined, SearchOutlined,
  MessageOutlined, EyeOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import { api, resolveMediaUrl } from '../api'
import {
  buildKnowledgeBase as requestBuildIndex,
  isKbBuilding,
  isKbTerminal,
} from '../api/knowledge'
import { errorDetail } from '../utils/apiError'
import { createChunkUpload } from '../utils/uploader'
import { kbAcceptAttr, isSupportedKbFile } from '../config/kbFileTypes'
import type { UploadProps } from 'ant-design-vue'
import EmptyState from '../components/common/EmptyState.vue'
import KBDocumentPreview from '../components/KBDocumentPreview.vue'
import AttachToAgentDialog from '../components/common/AttachToAgentDialog.vue'

import { useI18n } from 'vue-i18n'
const { t } = useI18n()
const API_URL = import.meta.env.VITE_API_URL || ''

/** 相对路径补 API_URL 前缀（resolveMediaUrl 返回的签名 URL 为 /media/s/... 相对路径） */
function absUrl(url: string): string {
  if (!url) return ''
  if (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('data:')) return url
  return `${API_URL}${url.startsWith('/') ? '' : '/'}${url}`
}

/**
 * 知识库链路的错误原文：`detail`（Python 引擎的 FastAPI 风格）→ `error`（网关）→ 兜底。
 * 刻意不含 `message` 回退 —— 需要它时由调用方追加 `errorDetail`。
 */
function kbErrorText(err: unknown, fallback: string): string {
  const e = (err ?? {}) as { response?: { data?: { detail?: unknown; error?: unknown } } }
  const detail = e.response?.data?.detail
  if (typeof detail === 'string' && detail) return detail
  const error = e.response?.data?.error
  if (typeof error === 'string' && error) return error
  return fallback
}

// 库状态：disabled/indexing/ready/error（0023 迁移的取值集合）
function kbStatusColor(status?: string): string {
  if (status === 'ready') return 'green'
  if (status === 'indexing') return 'processing'
  if (status === 'error') return 'red'
  return 'default'
}

function kbStatusLabel(status?: string): string {
  return t(`knowledge.status_${status || 'disabled'}`)
}

/** GET /v1/kb/{id}（网关字段 + 引擎补充的统计字段） */
interface KnowledgeBase {
  id?: string
  name?: string
  description?: string
  status?: string
  /** 用户开关（与 `status` 正交）；旧后端不返回时按启用处理。 */
  enabled?: boolean
  provider?: string
  visibility?: string
  document_count?: number
  total_size_bytes?: number
  credits_consumed?: number
  /** 索引失败原因（worker 写入；成功时为空串）。 */
  error_message?: string
}

/** GET /v1/kb/{id}/documents 的 documents[] */
interface KnowledgeDocument {
  id: string
  name?: string
  file_type?: string
  file_size_bytes?: number
  status?: string
  page_count?: number
  pageindex_doc_id?: string
  created_at?: string
}

/** GET /v1/media 的 items[]（媒体库选取弹窗用到的字段） */
interface MediaFileItem {
  id: string
  name: string
  type?: string
  size: number
  file_url: string
  mime_type?: string
}

/** 媒体库文件夹（`type === 'folder'`）。与文件同列返回，只能进入、不能被选中。 */
function isMediaFolder(item: MediaFileItem): boolean {
  return item.type === 'folder'
}

/** 弹窗一次拉取的上限 —— 与服务端 parsePagination 的 pageSize 上限一致（200）。 */
const MEDIA_PICKER_PAGE_SIZE = 200

/** Upload 的 customRequest 参数：从 antd 的 props 推导，避免手抄内部类型名 */
type UploadRequestOptions = Parameters<NonNullable<UploadProps['customRequest']>>[0]

const route = useRoute()
const router = useRouter()
const loading = ref(true)
const building = ref(false)
const kb = ref<KnowledgeBase | null>(null)
const documents = ref<KnowledgeDocument[]>([])
const buildProgress = ref(0)

// 文档列表列定义
const docColumns = [
  { title: t('media.file_name'), dataIndex: 'name', ellipsis: true },
  { title: t('common.type'), dataIndex: 'file_type', width: 80 },
  {
    title: t('common.size'), dataIndex: 'file_size_bytes', width: 100,
    customRender: ({ text }: { text: number }) => formatSize(text),
  },
  {
    title: t('common.status'), dataIndex: 'status', width: 100,
  },
  { title: t('knowledge.page_count'), dataIndex: 'page_count', width: 80 },
  {
    title: t('common.upload_time'), dataIndex: 'created_at', width: 160,
    customRender: ({ text }: { text: string }) => {
      if (!text) return ''
      const d = new Date(text)
      return isNaN(d.getTime()) ? '' : d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
    },
  },
  {
    title: t('common.action'), key: 'action', width: 90,
  },
]

// 文档搜索 + 批量删除
const docSearch = ref('')
const selectedDocIds = ref<string[]>([])
const deletingDocs = ref(false)

// 文档预览
const previewDocId = ref<string | null>(null)
const showPreview = ref(false)

// 批量重新索引
const reindexingIds = ref<string[]>([])
const showReindexBtn = ref(false)

const filteredDocs = computed(() => {
  const q = docSearch.value.trim().toLowerCase()
  if (!q) return documents.value
  return documents.value.filter(d => (d.name || '').toLowerCase().includes(q) || (d.file_type || '').toLowerCase().includes(q))
})

/** 当前库的 provider —— 决定接受哪些扩展名（自托管只吃 PDF，cloud 还有 Office） */
const kbProvider = computed(() => kb.value?.provider)

/** `<input accept>` 的属性值，随 provider 变化 */
const kbAccept = computed(() => kbAcceptAttr(kbProvider.value))

/**
 * 选择文件时先挡一次不受支持的类型。
 *
 * 这里不是安全边界（真正的判定在网关 Init），纯粹是 UX：批量选 20 个文件时，
 * 与其让它们全部传完再报 20 次错，不如在点击「打开」的那一刻就说清楚。
 * 返回 false 只拦截该文件，同批次里合法的文件继续上传。
 */
const beforeKbUpload: NonNullable<UploadProps['beforeUpload']> = (file) => {
  const name = typeof file === 'string' ? file : file.name
  if (isSupportedKbFile(kbProvider.value, name)) return true
  message.error(t('knowledge.unsupported_file_type', {
    name,
    supported: kbAccept.value.replace(/,/g, ' '),
  }))
  return false
}

async function deleteDoc(id: string) {
  try {
    await api.delete(`/v1/kb/${kbId}/documents`, { params: { doc_id: id } })
    message.success(t('common.deleted'))
    await loadDocuments()
    await loadKnowledgeBase()
  } catch (e) {
    message.error(kbErrorText(e, t('errors.delete_failed')))
  }
}

async function batchDeleteDocs() {
  const ids = [...selectedDocIds.value]
  if (!ids.length) { message.warning(t('knowledge.please_select_a_document_first')); return }
  deletingDocs.value = true
  try {
    for (const id of ids) {
      await api.delete(`/v1/kb/${kbId}/documents`, { params: { doc_id: id } })
    }
    message.success(t('knowledge.deleted_n_documents', { n: ids.length }))
    selectedDocIds.value = []
    await loadDocuments()
    await loadKnowledgeBase()
  } catch (e) {
    message.error(kbErrorText(e, t('errors.delete_failed')))
  } finally {
    deletingDocs.value = false
  }
}

/** 打开文档预览 */
function openPreview(docId: string) {
  previewDocId.value = docId
  showPreview.value = true
}

/** 批量重新索引 */
async function batchReindexDocs() {
  const ids = [...selectedDocIds.value]
  if (!ids.length) { message.warning(t('knowledge.please_select_a_document_first')); return }
  reindexingIds.value = ids
  try {
    // 与顶部「构建索引」走同一个客户端函数：重建是全库重建（后端没有部分重建的参数），
    // 提示语如实说明"全库"，免得用户以为只重跑勾选的那几篇。
    await requestBuildIndex(kbId)
    message.success(t('knowledge.index_build_started'))
  } catch (e) {
    message.error(kbErrorText(e, t('errors.re_index_failed')))
  } finally {
    reindexingIds.value = []
  }
}

// 媒体库相关
const showMediaModal = ref(false)
const mediaFiles = ref<MediaFileItem[]>([])
const selectedMediaIds = ref<string[]>([])
const loadingMedia = ref(false)
const importingMedia = ref(false)
const mediaSearchQuery = ref('')

// ── 选取弹窗的目录导航 ──
// 媒体库的文件夹与文件**同列返回**（`type === 'folder'`）。此前弹窗把它们当普通条目
// 列出来：点一下只会把文件夹 id 塞进选中集合，接着 importFromMedia 取不到 file_url、
// 静默 `continue` —— 用户看到"已导入 0 个文件"，却没有任何解释。
// 现在文件夹变成可进入的一层：面包屑与媒体库页一致，根目录 id 为空串（服务端约定）。
const mediaBreadcrumbs = ref<{ id: string; name: string }[]>([{ id: '', name: '' }])
const mediaParentId = computed(
  () => mediaBreadcrumbs.value[mediaBreadcrumbs.value.length - 1].id,
)

/**
 * 已选条目本身（id → item），而不只是 id。
 *
 * 选中可以**跨越多个文件夹**，而 `mediaFiles` 只装当前目录。若只存 id，切一次目录
 * 后再导入，`find` 必然落空，用户点的导入就变成"成功 0 个"。
 */
const pickedMedia = ref<Record<string, MediaFileItem>>({})

const kbId = route.params.id as string

onMounted(async () => {
  await loadKnowledgeBase()
  await loadDocuments()
})

async function loadKnowledgeBase() {
  try {
    const res = await api.get(`/v1/kb/${kbId}`)
    kb.value = res.data?.data || res.data
  } catch {
    message.error(t('errors.failed_to_load_knowledge_base'))
    router.push('/knowledge')
  } finally {
    loading.value = false
  }
}

async function loadDocuments() {
  try {
    const res = await api.get(`/v1/kb/${kbId}/documents`)
    documents.value = res.data?.data?.documents || []
  } catch {
    // 静默失败
  }
}

// ── 启用 / 停用（owner-only）──
// `enabled` 是用户开关，与索引状态 `status` 正交：停用不销毁索引，重新启用直接可用。
// 非属主后端回 404（不回 403，免得确认库是否存在），这里统一给一句"只能操作自己的"。
const enabledToggling = ref(false)

async function toggleEnabled(next: boolean) {
  enabledToggling.value = true
  try {
    await api.put(`/v1/kb/${kbId}/status`, { enabled: next })
    if (kb.value) kb.value.enabled = next
    message.success(next ? t('knowledge.kb_enabled') : t('knowledge.kb_disabled'))
  } catch (e) {
    message.error(kbErrorText(e, t('knowledge.can_only_operate_knowledge_bases_you_created')))
    // 失败回读真实状态：开关不能停在用户以为成功的位置
    await loadKnowledgeBase()
  } finally {
    enabledToggling.value = false
  }
}

// 过滤后的媒体文件
const filteredMediaFiles = computed(() => {
  if (!mediaSearchQuery.value.trim()) return mediaFiles.value
  const query = mediaSearchQuery.value.toLowerCase()
  return mediaFiles.value.filter((f) =>
    (f.name || '').toLowerCase().includes(query) ||
    (f.type || '').toLowerCase().includes(query)
  )
})

// 上传文档：全局分片（purpose kb_doc → complete 落 knowledge_documents）
//
// 批量支持：antd 的 customRequest 是**逐文件**调用的（即使 multiple 也一样），
// 所以"批量"必须在两个地方补：
//   1. 模板上加 multiple（否则文件选择框只能选一个）；
//   2. 这里做批次聚合（否则选 10 个文件会弹 10 条提示、触发 10 次列表重载）。
//
// 并发也要限：每个文件内部已有 3 路分片并发，一次选 50 个文件就是 150 个
// 在途请求，弱网下会把连接池占满、拖垮同一页面的其他请求。
const MAX_PARALLEL_UPLOADS = 3
let activeUploads = 0
const uploadQueue: Array<() => void> = []

function acquireUploadSlot(): Promise<void> {
  if (activeUploads < MAX_PARALLEL_UPLOADS) {
    activeUploads++
    return Promise.resolve()
  }
  return new Promise<void>((resolve) => {
    uploadQueue.push(() => {
      activeUploads++
      resolve()
    })
  })
}

function releaseUploadSlot(): void {
  activeUploads--
  uploadQueue.shift()?.()
}

/** 本批次统计：inFlight 归零时才汇总提示 + 刷新列表 */
const uploadBatch = { inFlight: 0, ok: 0, failed: 0 }

async function handleUpload(info: UploadRequestOptions) {
  // antd 的 file 允许多态（string/Blob，用于受控 fileList）；本视图只接真实上传文件
  const { file } = info
  if (!(file instanceof File)) return

  uploadBatch.inFlight++
  await acquireUploadSlot()
  try {
    const handle = await createChunkUpload(file, { purpose: 'kb_doc', parentId: kbId })
    await handle.done
    uploadBatch.ok++
    info.onSuccess?.({})
  } catch (error) {
    uploadBatch.failed++
    // 失败必须点名是哪个文件：批量时"上传失败"这四个字没法定位问题
    const reason = kbErrorText(error, '') || errorDetail(error, t('errors.upload_failed'))
    message.error(`${file.name}：${reason}`)
    info.onError?.(error instanceof Error ? error : new Error(String(error)))
  } finally {
    releaseUploadSlot()
    uploadBatch.inFlight--
    if (uploadBatch.inFlight === 0) await flushUploadBatch()
  }
}

/** 一个批次结束：汇总提示 + 刷新一次列表（而不是每个文件各刷一次） */
async function flushUploadBatch() {
  const { ok, failed } = uploadBatch
  uploadBatch.ok = 0
  uploadBatch.failed = 0
  if (ok > 0) {
    message.success(t('knowledge.uploaded_n_files_to_the_knowledge_base', { n: ok }))
    await loadKnowledgeBase()
    await loadDocuments()
  }
  if (failed > 0) {
    message.error(t('errors.n_files_failed_to_upload', { n: failed }))
  }
}

async function openMediaModal() {
  showMediaModal.value = true
  selectedMediaIds.value = []
  pickedMedia.value = {}
  mediaSearchQuery.value = ''
  mediaBreadcrumbs.value = [{ id: '', name: '' }]
  await loadMediaFiles()
}

/**
 * 拉取当前目录的内容。
 *
 * `parent_id` 必须显式传空串（不是省略）：服务端按 `COALESCE(parent_id, '')` 比较，
 * 省略同样落到空串，但显式传能让"根目录"这件事在代码里写清楚。
 */
async function loadMediaFiles() {
  loadingMedia.value = true
  try {
    const res = await api.get('/v1/media', {
      params: { parent_id: mediaParentId.value, page_size: MEDIA_PICKER_PAGE_SIZE },
    })
    mediaFiles.value = res.data?.data?.items || []
  } catch {
    mediaFiles.value = []
    message.error(t('errors.failed_to_load_media_library'))
  } finally {
    loadingMedia.value = false
  }
}

/** 进入子文件夹。搜索词保留 —— 用户往往是在搜到的东西里继续往里翻。 */
async function enterMediaFolder(item: MediaFileItem) {
  mediaBreadcrumbs.value.push({ id: item.id, name: item.name })
  await loadMediaFiles()
}

async function goMediaBreadcrumb(idx: number) {
  mediaBreadcrumbs.value = mediaBreadcrumbs.value.slice(0, idx + 1)
  await loadMediaFiles()
}

async function importFromMedia() {
  if (selectedMediaIds.value.length === 0) {
    message.warning(t('media.please_select_the_file_to_import'))
    return
  }

  importingMedia.value = true
  let successCount = 0
  let failCount = 0

  // 从 pickedMedia 取条目（而不是当前目录列表）：选择可以跨文件夹，只查 mediaFiles
  // 会把其他目录里选中的文件漏掉，导入结果少得莫名其妙。
  for (const fileId of selectedMediaIds.value) {
    const file = pickedMedia.value[fileId]
    if (!file) continue

    try {
      // 安全改造：/media/ 公开路径先解析为短时效签名 URL 再取字节（带归属校验）
      const signed = await resolveMediaUrl({ id: file.id, file_url: file.file_url })
      const response = await fetch(absUrl(signed || file.file_url))
      const blob = await response.blob()
      const mediaFile = new File([blob], file.name, { type: file.mime_type || '' })
      const handle = await createChunkUpload(mediaFile, { purpose: 'kb_doc', parentId: kbId })
      await handle.done
      successCount++
    } catch {
      failCount++
    }
  }

  importingMedia.value = false
  showMediaModal.value = false
  selectedMediaIds.value = []

  if (successCount > 0) {
    message.success(t('media.imported_n_files', { n: successCount }))
    await loadKnowledgeBase()
    await loadDocuments()
  }
  if (failCount > 0) {
    message.error(t('errors.n_files_failed_to_import', { n: failCount }))
  }
}

function toggleMediaSelection(item: MediaFileItem) {
  const index = selectedMediaIds.value.indexOf(item.id)
  if (index === -1) {
    selectedMediaIds.value.push(item.id)
    pickedMedia.value[item.id] = item
  } else {
    selectedMediaIds.value.splice(index, 1)
    delete pickedMedia.value[item.id]
  }
}

/** 全选当前目录的**文件**（文件夹不是可导入对象，不能进选中集合）。 */
function selectAllMedia() {
  const files = filteredMediaFiles.value.filter((f) => !isMediaFolder(f))
  for (const f of files) pickedMedia.value[f.id] = f
  selectedMediaIds.value = files.map((f) => f.id)
}

function deselectAllMedia() {
  selectedMediaIds.value = []
  pickedMedia.value = {}
}

/** 当前目录里还有没有可导入的文件（用来区分"目录是空的"和"只有文件夹"）。 */
const hasSelectableMedia = computed(() =>
  filteredMediaFiles.value.some((f) => !isMediaFolder(f)),
)

/**
 * 当前目录的可导入文件数。
 *
 * 计数不能直接用 `filteredMediaFiles.length`：文件夹也在这份列表里，把它们算进
 * "共 N 项"会让「全选」后的选中数永远追不上总数，看着像功能坏了。
 */
const selectableMediaCount = computed(
  () => filteredMediaFiles.value.filter((f) => !isMediaFolder(f)).length,
)

/** 库上的索引错误原因（worker 写在 `error_message`，如"文档 X: 缺少原文对象"）。 */
function kbErrorMessage(): string {
  return kb.value?.error_message || ''
}

/**
 * 轮询到索引进入终态。
 *
 * 判据来自 `api/knowledge.ts` 的 `isKbTerminal`（镜像引擎的 `status.py`）——
 * 这里**不能**再写一个字面量：上一版写 `status === 'active'`，而 worker 只会写
 * `ready`/`error`，于是索引早已跑完界面还在轮询，十轮后默默放弃。
 *
 * `baseline` 是投递前的库状态，用来区分"这次构建真的跑完了"和"库里本来就停在终态"：
 * 对已 `ready` 的库做重建时，worker 取件前 `status` 仍是 `ready`，不加这层判断就会
 * 在第一次轮询时谎报"构建完成"。所以只有**观察到 `indexing` 过**或**状态已偏离基线**
 * 时才认终态。
 *
 * 返回 `pending` 表示超时未定：任务可能仍在 worker 里排着，界面只报"仍在进行"。
 */
async function waitForBuildToSettle(
  baseline?: string | null,
  attempts = 30,
  intervalMs = 500,
): Promise<'ready' | 'error' | 'pending'> {
  let sawBuilding = false
  for (let i = 0; i < attempts; i++) {
    await new Promise(resolve => setTimeout(resolve, intervalMs))
    buildProgress.value = Math.min(90, Math.round(((i + 1) / attempts) * 90))
    await loadKnowledgeBase()
    const status = kb.value?.status
    if (isKbBuilding(status)) {
      sawBuilding = true
      continue
    }
    if (isKbTerminal(status) && (sawBuilding || status !== baseline)) {
      if (status === 'ready') buildProgress.value = 100
      return status === 'ready' ? 'ready' : 'error'
    }
  }
  return 'pending'
}

async function buildKnowledgeBase() {
  try {
    building.value = true
    buildProgress.value = 0

    // 投递前的库状态：重建一个已 `ready` 的库时，worker 取件前 status 仍是 `ready`，
    // 轮询必须靠它区分"本来就完成的旧状态"和"这次真的跑完了"。
    const baseline = kb.value?.status ?? null

    // 服务端只回 status / task_id / provider —— **没有** `estimated_cost` 这个字段
    // （计费口径未定，见 python-engine/app/api/knowledge.py 的文件头）。旧提示语把
    // `{ n: undefined }` 插进「预计消耗 {n} credits」，用户看到的是「预计消耗 undefined
    // credits」。这里改成"已投递"，不自造一个后端不存在的估算值。
    await requestBuildIndex(kbId)
    message.success(t('knowledge.index_build_started'))

    const outcome = await waitForBuildToSettle(baseline)
    if (outcome === 'ready') {
      message.success(t('common.build_complete'))
    } else if (outcome === 'error') {
      message.error(kbErrorMessage() || t('errors.build_failed'))
    } else {
      // 超时不等于失败：任务可能还在 worker 里跑，如实说"仍在进行"比谎报完成好
      message.info(t('knowledge.index_still_running'))
    }
    await loadDocuments()
  } catch (error) {
    message.error(kbErrorText(error, t('errors.build_failed')))
  } finally {
    building.value = false
    buildProgress.value = 0
  }
}

// 互联互通：携带知识库上下文跳转到对话工作台
function askInChat() {
  router.push({ path: '/chat', query: { kb: kbId } })
}

// 互联互通：装配到 Agent —— 与 askInChat 的一次性上下文不同，
// 这里写回 Agent 的 kb_id（它只有一个"默认知识库"），之后每次派发都带上。
const attachOpen = ref(false)

function openAttach() {
  attachOpen.value = true
}

function formatSize(bytes: number): string {
  if (bytes === 0) return '0 B'
  const k = 1024
  const sizes = ['B', 'KB', 'MB', 'GB']
  const i = Math.floor(Math.log(bytes) / Math.log(k))
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i]
}
</script>

<template>
  <div class="kb-detail-container">
    <div class="kb-detail-header">
      <Button
        type="text"
        @click="router.push('/knowledge')"
      >
        <template #icon>
          <ArrowLeftOutlined />
        </template>
        {{ $t('common.back') }}
      </Button>
      <h1>{{ kb?.name || $t('knowledge.knowledge_base') }}</h1>
      <Space>
        <Button
          :title="$t('knowledge.ask_based_on_this_knowledge_base_in_the_conversation')"
          @click="askInChat"
        >
          <template #icon>
            <MessageOutlined />
          </template>
          {{ $t('common.ask_about_this') }}
        </Button>
        <Button
          :title="$t('agent.assemble_this_knowledge_base_as_an_agent_s_default_knowledge_base_persistent_binding')"
          @click="openAttach"
        >
          {{ $t('agent.assemble_to_agent') }}
        </Button>
        <Button
          v-if="!isKbBuilding(kb?.status)"
          type="primary"
          :loading="building"
          :disabled="building || kb?.enabled === false"
          :title="kb?.enabled === false ? $t('knowledge.kb_disabled_hint') : undefined"
          @click="buildKnowledgeBase"
        >
          <template #icon>
            <PlayCircleOutlined />
          </template>
          {{ $t('common.build_index') }}
        </Button>
        <Button
          v-else
          disabled
        >
          {{ $t('common.building') }}
        </Button>
      </Space>
    </div>

    <Spin :spinning="loading">
      <div
        v-if="kb"
        class="kb-info"
      >
        <Card>
          <div class="info-grid">
            <div class="info-item">
              <span class="label">{{ $t('knowledge.provider') }}</span>
              <Tag :color="kb.provider === 'cloud' ? 'geekblue' : 'cyan'">
                {{ kb.provider === 'cloud' ? 'PageIndex Cloud' : $t('knowledge.provider_selfhosted_short') }}
              </Tag>
            </div>
            <div class="info-item">
              <span class="label">{{ $t('common.visibility') }}</span>
              <Tag :color="kb.visibility === 'public' ? 'warning' : 'default'">
                {{ kb.visibility === 'public' ? $t('common.public') : $t('common.private') }}
              </Tag>
            </div>
            <div class="info-item">
              <span class="label">{{ $t('common.status') }}</span>
              <Tag :color="kbStatusColor(kb.status)">
                {{ kbStatusLabel(kb.status) }}
              </Tag>
            </div>
            <!-- 用户开关：与上面那个索引状态正交（停用不销毁索引，重新启用即可用） -->
            <div class="info-item">
              <span class="label">{{ $t('knowledge.enabled') }}</span>
              <span
                class="enabled-toggle"
                :title="kb.enabled === false ? $t('knowledge.kb_disabled_hint') : $t('knowledge.kb_enabled_hint')"
              >
                <Switch
                  :checked="kb.enabled !== false"
                  :loading="enabledToggling"
                  size="small"
                  @change="(v: any) => toggleEnabled(Boolean(v))"
                />
              </span>
            </div>
            <div class="info-item">
              <span class="label">{{ $t('knowledge.documents') }}</span>
              <span>{{ kb.document_count }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ $t('common.total_size') }}</span>
              <span>{{ formatSize(kb.total_size_bytes ?? 0) }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ $t('common.consumed') }}</span>
              <span>{{ kb.credits_consumed }} credits</span>
            </div>
          </div>
          <p
            v-if="kb.description"
            class="kb-description"
          >
            {{ kb.description }}
          </p>
        </Card>

        <!-- 构建进度 -->
        <Card
          v-if="building"
          :title="$t('common.build_progress')"
          style="margin-top: 16px"
        >
          <Progress
            :percent="buildProgress"
            status="active"
          />
        </Card>

        <!-- 文档列表 -->
        <Card
          :title="$t('knowledge.document_management')"
          style="margin-top: 16px"
        >
          <template #extra>
            <Space>
              <Input
                v-model:value="docSearch"
                :placeholder="$t('knowledge.search_documents')"
                allow-clear
                size="small"
                style="width: 180px"
              >
                <template #prefix>
                  <SearchOutlined />
                </template>
              </Input>
              <Button
                v-if="selectedDocIds.length > 0"
                size="small"
                danger
                :loading="deletingDocs"
                @click="batchDeleteDocs"
              >
                <template #icon>
                  <DeleteOutlined />
                </template>
                {{ $t('common.batch_delete_n', { n: selectedDocIds.length }) }}
              </Button>
              <Button
                v-if="selectedDocIds.length > 0"
                size="small"
                :loading="reindexingIds.length > 0"
                @click="batchReindexDocs"
              >
                <template #icon>
                  <ReloadOutlined />
                </template>
                {{ $t('common.batch_reindex_n', { n: selectedDocIds.length }) }}
              </Button>
              <Button
                size="small"
                @click="openMediaModal"
              >
                <template #icon>
                  <PictureOutlined />
                </template>
                {{ $t('common.pick_from_media_library') }}
              </Button>
              <Upload
                multiple
                :show-upload-list="false"
                :custom-request="handleUpload"
                :before-upload="beforeKbUpload"
                :accept="kbAccept"
              >
                <Button
                  type="primary"
                  size="small"
                >
                  <template #icon>
                    <CloudUploadOutlined />
                  </template>
                  {{ $t('knowledge.upload_document') }}
                </Button>
              </Upload>
            </Space>
          </template>

          <EmptyState
            v-if="filteredDocs.length === 0"
            size="list"
            :icon="markRaw(CloudUploadOutlined)"
            :description="$t('knowledge.no_documents_yet')"
            :hint="$t('knowledge.upload_documents_or_pick_from_the_media_library_they_become_searchable_after_indexing')"
          />
          <Table
            v-else
            :columns="docColumns"
            :data-source="filteredDocs"
            :pagination="false"
            :row-selection="{ selectedRowKeys: selectedDocIds, onChange: (keys: any[]) => (selectedDocIds = keys) }"
            row-key="id"
          >
            <template #bodyCell="{ column, text, record }">
              <template v-if="column.dataIndex === 'name'">
                <a
                  class="doc-name-link"
                  role="button"
                  tabindex="0"
                  @click="openPreview(record.id)"
                  @keydown.enter.prevent="openPreview(record.id)"
                  @keydown.space.prevent="openPreview(record.id)"
                >{{ text }}</a>
              </template>
              <template v-else-if="column.dataIndex === 'file_size_bytes'">
                {{ formatSize(text) }}
              </template>
              <template v-else-if="column.dataIndex === 'status'">
                <Tag :color="text === 'completed' ? 'success' : text === 'processing' ? 'processing' : text === 'error' ? 'error' : 'default'">
                  {{ text === 'pending' ? $t('common.pending') : text === 'processing' ? $t('common.processing') : text === 'completed' ? $t('common.completed') : $t('errors.failed') }}
                </Tag>
              </template>
              <template v-else-if="column.dataIndex === 'created_at'">
                {{ text ? new Date(text).toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '' }}
              </template>
              <template v-else-if="column.key === 'action'">
                <Space>
                  <Button
                    type="text"
                    size="small"
                    :title="$t('knowledge.preview_document')"
                    @click="openPreview(record.id)"
                  >
                    <template #icon>
                      <EyeOutlined />
                    </template>
                  </Button>
                  <Popconfirm
                    :title="$t('knowledge.confirm_deleting_this_document')"
                    @confirm="deleteDoc(record.id)"
                  >
                    <Button
                      type="text"
                      danger
                      size="small"
                      :title="$t('knowledge.delete_document')"
                    >
                      <template #icon>
                        <DeleteOutlined />
                      </template>
                    </Button>
                  </Popconfirm>
                </Space>
              </template>
            </template>
          </Table>
        </Card>
      </div>
    </Spin>

    <!-- 从媒体库选取弹窗 -->
    <Modal
      v-model:visible="showMediaModal"
      :title="$t('common.pick_from_media_library')"
      :footer="null"
      :style="{ maxWidth: '700px' }"
    >
      <!-- 搜索栏 -->
      <div class="media-search-bar">
        <Input
          v-model:value="mediaSearchQuery"
          :placeholder="$t('media.search_files_2')"
          allow-clear
        >
          <template #prefix>
            <SearchOutlined />
          </template>
        </Input>
        <div class="media-actions">
          <span class="selected-count">{{ $t('common.selected_sel_total_items', { sel: selectedMediaIds.length, total: selectableMediaCount }) }}</span>
          <Button
            size="small"
            :disabled="!hasSelectableMedia"
            @click="selectAllMedia"
          >
            {{ $t('common.select_all') }}
          </Button>
          <Button
            size="small"
            @click="deselectAllMedia"
          >
            {{ $t('common.clear_selection') }}
          </Button>
        </div>
      </div>

      <!-- 目录面包屑：与媒体库页同构（末项是当前位置，不做按钮）。
           根目录回退用 all_files，与媒体库页的文案一致。 -->
      <div class="media-breadcrumb">
        <template
          v-for="(cr, i) in mediaBreadcrumbs"
          :key="cr.id || 'root'"
        >
          <span
            class="crumb"
            :class="{ active: i === mediaBreadcrumbs.length - 1 }"
            :role="i === mediaBreadcrumbs.length - 1 ? undefined : 'button'"
            :tabindex="i === mediaBreadcrumbs.length - 1 ? undefined : 0"
            :aria-current="i === mediaBreadcrumbs.length - 1 ? 'page' : undefined"
            @click="i < mediaBreadcrumbs.length - 1 && goMediaBreadcrumb(i)"
            @keydown.enter.prevent="i < mediaBreadcrumbs.length - 1 && goMediaBreadcrumb(i)"
          >{{ cr.name || $t('media.all_files') }}</span>
          <span
            v-if="i < mediaBreadcrumbs.length - 1"
            class="crumb-sep"
            aria-hidden="true"
          >/</span>
        </template>
      </div>

      <Spin :spinning="loadingMedia">
        <div
          v-if="filteredMediaFiles.length === 0 && !loadingMedia"
          class="media-empty"
        >
          <Empty :description="mediaSearchQuery ? $t('media.no_matching_files') : $t('media.no_files_yet')" />
        </div>
        <div
          v-else
          class="media-list"
        >
          <!-- 文件夹与文件混在一列返回：文件夹渲染成"进入"按钮，不参与勾选 -->
          <div
            v-for="item in filteredMediaFiles"
            :key="item.id"
            :class="['media-item', { selected: !isMediaFolder(item) && selectedMediaIds.includes(item.id) }]"
            :role="isMediaFolder(item) ? 'button' : 'checkbox'"
            :aria-checked="isMediaFolder(item) ? undefined : selectedMediaIds.includes(item.id)"
            tabindex="0"
            @click="isMediaFolder(item) ? enterMediaFolder(item) : toggleMediaSelection(item)"
            @keydown.enter.prevent="isMediaFolder(item) ? enterMediaFolder(item) : toggleMediaSelection(item)"
            @keydown.space.prevent="isMediaFolder(item) ? enterMediaFolder(item) : toggleMediaSelection(item)"
          >
            <!-- 文件夹没有勾选框：放进一个不会被点到的 Checkbox 只会让人以为能选 -->
            <FolderOutlined
              v-if="isMediaFolder(item)"
              class="media-folder-icon"
              aria-hidden="true"
            />
            <!-- 内层 Checkbox 只作视觉：语义与键盘操作已由外层的 role="checkbox" + aria-checked
                 承担。若不排除它，这一行会出现两个可聚焦点表达同一件事（Tab 要经过两次、
                 读屏要读两遍）；aria-hidden 配合 tabindex="-1" 才是合法组合
                 （aria-hidden 的元素不允许仍可聚焦）。 -->
            <Checkbox
              v-else
              :checked="selectedMediaIds.includes(item.id)"
              :tabindex="-1"
              aria-hidden="true"
            />
            <div class="media-info">
              <span class="media-name">{{ item.name }}</span>
              <span class="media-meta">
                <span
                  v-if="!isMediaFolder(item)"
                  class="media-type"
                >{{ item.type }}</span>
                <span
                  v-if="!isMediaFolder(item)"
                  class="media-size"
                >{{ formatSize(item.size) }}</span>
              </span>
            </div>
          </div>
        </div>
      </Spin>

      <div class="modal-footer">
        <Button @click="showMediaModal = false">
          {{ $t('common.cancel') }}
        </Button>
        <Button
          type="primary"
          :loading="importingMedia"
          :disabled="selectedMediaIds.length === 0"
          @click="importFromMedia"
        >
          {{ $t('media.import_selected_files_n', { n: selectedMediaIds.length }) }}
        </Button>
      </div>
    </Modal>

    <!-- 文档预览弹窗 -->
    <KBDocumentPreview
      v-model:visible="showPreview"
      :document-id="previewDocId"
      :kb-id="kbId"
    />

    <!-- 装配到 Agent：写回 Agent 的 kb_id（只有一个"默认知识库"，故为覆盖语义） -->
    <AttachToAgentDialog
      v-model:open="attachOpen"
      kind="kb"
      :value="kbId"
      :label="kb?.name || $t('knowledge.knowledge_base')"
    />
  </div>
</template>

<style scoped>
.kb-detail-container { padding: 24px; max-width: 1200px; margin: 0 auto; }
.kb-detail-header { display: flex; align-items: center; gap: 16px; margin-bottom: 24px; }
.kb-detail-header h1 { flex: 1; margin: 0; font-size: 24px; font-weight: var(--fw-semibold); color: var(--text-primary); }
.info-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 16px; }
.info-item { display: flex; flex-direction: column; gap: 4px; }
.enabled-toggle { display: inline-flex; align-items: center; }
.info-item .label { font-size: 13px; color: var(--text-tertiary); }
.info-item .value { color: var(--text-primary); }
.kb-description { margin-top: 16px; padding-top: 16px; border-top: var(--hairline-width) solid var(--border-card); color: var(--text-secondary); line-height: 1.6; }
.modal-footer { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }

.query-results { margin-top: 16px; padding-top: 16px; border-top: var(--hairline-width) solid var(--border-card); }
.query-results h3 { margin: 0 0 12px; font-size: 16px; color: var(--text-primary); }
.query-result-item { padding: 12px; background: var(--bg-secondary, rgba(0,0,0,0.02)); border-radius: var(--radius-lg); margin-bottom: 8px; }
.result-source { font-size: 12px; color: var(--text-tertiary); }
.result-header { margin-bottom: 8px; }
.result-content { margin: 0; font-size: 14px; line-height: 1.6; color: var(--text-primary); }
.search-highlight { background: var(--highlight-bg); color: var(--highlight-fg); padding: 1px 2px; border-radius: var(--radius-xs); }
.doc-name-link {
 color: var(--primary-color, #1890ff); cursor: pointer; font-weight: var(--fw-medium); 
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.doc-name-link:hover { text-decoration: underline; }

.media-search-bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; }
.media-actions { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.selected-count { font-size: 13px; color: var(--text-secondary); }
.media-empty { padding: 40px 0; }
/* 目录面包屑：与媒体库页 .breadcrumb 的观感保持一致 */
.media-breadcrumb { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; min-width: 0; margin-bottom: 10px; font-size: 13px; }
.media-breadcrumb .crumb { color: var(--text-secondary); border-radius: var(--radius-sm, 4px); padding: 2px 4px; }
.media-breadcrumb .crumb[role="button"] { cursor: pointer; }
.media-breadcrumb .crumb[role="button"]:hover { color: var(--primary-color, #1677ff); background: var(--bg-hover, rgba(0,0,0,0.03)); }
.media-breadcrumb .crumb.active { color: var(--text-primary); font-weight: var(--fw-medium); }
.media-breadcrumb .crumb-sep { color: var(--text-tertiary); }
/* 首列图标位与 Checkbox 同宽，让文件夹行与文件行左边缘对齐 */
.media-folder-icon { font-size: 14px; color: var(--warning-color, #faad14); flex: none; }
.media-list { border: var(--hairline-width) solid var(--border-card); border-radius: var(--radius-lg); overflow: hidden; max-height: 400px; overflow-y: auto; -webkit-overflow-scrolling: touch; }
.media-item { display: flex; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: var(--hairline-width) solid var(--border-card); cursor: pointer; transition: background var(--dur-normal); }
.media-item:last-child { border-bottom: none; }
.media-item:hover { background: var(--bg-hover, rgba(0,0,0,0.03)); }
.media-item.selected { background: var(--primary-bg, #e6f7ff); }
.media-info { display: flex; flex-direction: column; gap: 2px; flex: 1; min-width: 0; }
.media-name { font-weight: var(--fw-medium); font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
.media-meta { display: flex; gap: 12px; font-size: 12px; color: var(--text-tertiary); }
.media-type { text-transform: uppercase; }

/* 移动端/平板：搜索栏堆叠、header 纵向、卡片头部换行、表格横向滚动 */
@media (max-width: 768px) {
  .kb-detail-container { padding: 16px 12px; }
  .kb-detail-header { flex-direction: column; align-items: flex-start; gap: 10px; }
  .kb-detail-header h1 { font-size: 20px; }
  .media-search-bar { flex-direction: column; align-items: stretch; gap: 8px; }
  .media-actions { justify-content: flex-end; }
  .info-grid { grid-template-columns: repeat(2, 1fr); }
  .media-item { padding: 10px 12px; }
  /* 卡片头部（标题 + 搜索/按钮）允许换行 */
  :deep(.ant-card-head) { flex-wrap: wrap; row-gap: 8px; }
  :deep(.ant-card-head-wrapper) { flex-wrap: wrap; row-gap: 8px; }
  :deep(.ant-card-extra) { margin-inline-start: 0; width: 100%; }
  :deep(.ant-card-extra .ant-space) { flex-wrap: wrap; row-gap: 8px; }
  /* 文档表格窄屏横向滚动 */
  :deep(.ant-table-wrapper) { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  :deep(.ant-table-wrapper .ant-table) { min-width: 640px; }
}

@media (prefers-reduced-motion: reduce) {
  .media-item { transition: none; }
}
</style>
