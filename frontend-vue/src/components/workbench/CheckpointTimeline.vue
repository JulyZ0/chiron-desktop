<script setup lang="ts">
/**
 * 还原点时间线：列出「过去哪些时刻可以回滚回去」，并提供回滚动作。
 *
 * 四个必须说清楚的点：
 *
 * 1. **回滚是写用户磁盘的操作**，所以走二次确认，且确认框里写明会动多少个文件。
 * 2. **默认不删除**快照之后新增的文件（与后端 `allow_delete: false` 同口径）。
 *    不删的路径在结果里如实列出，并给一个**独立的**删除入口 —— 删是比改更重的
 *    动作，不该藏在第一次确认里顺手做掉。第二次请求只带 `paths: skipped_removed`，
 *    引擎的 `restore()` 因此只删这些路径、不重写任何文件。
 * 3. **`state !== 'ok'` 的点不可回滚**：那种快照本身就没写全，拿它回滚会得到
 *    半截工作区。按钮禁用并说明原因，而不是让用户点下去再看报错。
 * 4. **被跳过 / 受保护 / 不支持的路径必须显示**：`skipped_count` 非 0 意味着这个点
 *    还原出来本来就不完整；`protected_removed`（.env、密钥类命中保护名单而没删）
 *    与 `unsupported`（符号链接等）如果不说，用户会以为工作区已经完全回到那一刻。
 * 5. **先看差异，再决定回滚**：每个点都能预览（只读），预览里列出路径清单与计数。
 *    预览失败必须就地报错 —— 静默变成"没有差异"会让用户放心地点下回滚。
 * 6. **`untracked_omitted` 必须显示**：清单读不到时预览只覆盖 git 跟踪的文件，
 *    不说的话"预览没有差异"会被理解成"回滚不会动任何东西"。
 * 7. **GC 只对管理员可见，且默认只是预演**：真正的删除要再确认一次。可见性只是
 *    体验，权限由后端兜底（`POST /v1/admin/checkpoints/gc` 要求平台 owner）。
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Modal } from 'ant-design-vue'
import {
  listProjectCheckpoints,
  previewCheckpointDiff,
  restoreCheckpoint,
  runCheckpointGc,
  type CheckpointDiffPreview,
  type CheckpointGcResult,
  type CheckpointStats,
  type ProjectCheckpoint,
  type RestoreResult,
} from '../../api/checkpoints'
import { getProjectVcs, initProjectVcs, type ProjectVCS } from '../../api/projectVcs'
import { formatSmart } from '../../utils/datetime'
import { describeApiError } from '../../utils/apiError'
import { notify } from '../../utils/notify'

const { t: tr } = useI18n()

const props = withDefaults(
  defineProps<{
    /** 当前会话所属项目；空串 = 没有项目，不请求 */
    projectId?: string
    /**
     * 是否显示「回收未引用缓存」入口（管理员）。
     *
     * **这只是可见性**：真正的权限由后端 `POST /v1/admin/checkpoints/gc` 兜底，
     * 前端藏按钮挡不住任何请求。之所以还要藏，是为了不让普通用户看到一个
     * 点了必然 403 的按钮。
     */
    canAdmin?: boolean
  }>(),
  { projectId: '', canAdmin: false },
)

const emit = defineEmits<{
  /**
   * 回滚成功。父级据此刷新「改动」页签与文件树 —— 工作区刚被改写，
   * 不刷新的话用户看到的是回滚前的旧清单。
   */
  (e: 'restored', result: RestoreResult): void
}>()

const loading = ref(false)
const error = ref('')
const items = ref<ProjectCheckpoint[]>([])
/** 正在回滚的检查点 id，用来禁用重复点击 */
const restoring = ref('')
/** 最近一次回滚结果，就地展开显示动了什么 */
const lastResult = ref<RestoreResult | null>(null)
/** `lastResult` 属于哪个检查点：结果块里的「删除新增文件」要知道发给谁 */
const lastCheckpoint = ref<ProjectCheckpoint | null>(null)

/** 规模统计（时间线头部）。`null` = 还没读到 / 老网关没有这个字段 */
const stats = ref<CheckpointStats | null>(null)
/** 正在预览的检查点 id，用来禁用重复点击 */
const previewing = ref('')
/** 当前展示的预览：属于哪个点 + 引擎算出的计划 */
const preview = ref<{ checkpoint: ProjectCheckpoint; result: CheckpointDiffPreview } | null>(null)
/** 预览失败的原因（就地显示，不弹角落提示 —— 用户正看着这块） */
const previewError = ref('')
/** 正在跑 GC（预演或执行） */
const gcLoading = ref(false)
/** 最近一次 GC 结果（dry-run 或已执行） */
const gcResult = ref<CheckpointGcResult | null>(null)

const hasProject = computed(() => !!props.projectId)

/**
 * 项目根的版本控制状态。
 *
 * `null` = 还没读到（或读失败）。读不到时**不**显示"启用"入口 ——
 * 拿不到状态就问用户"要不要在这个目录里建 .git"，是在让用户为一个
 * 我们没看清的事实做决定。
 */
const vcs = ref<ProjectVCS | null>(null)
/** 正在执行 git init（写用户磁盘），用来禁用重复点击 */
const enabling = ref(false)

/**
 * 需要提示"启用版本跟踪" = 确认不在工作树内，且机器上有 git。
 *
 * `git_missing` 排除掉：那种情况下按钮点不动，用户该去装 git —— 显示一个
 * 永远失败的按钮比不显示更糟。
 */
const needsVcs = computed(() => !!vcs.value && !vcs.value.repo && !vcs.value.git_missing)

/**
 * 请求代次。
 *
 * 与 `WorkbenchPanel` 的 `treeGeneration` 同一个理由：切项目时上一个项目的
 * 时间线请求可能还在飞，它的响应回来时 `props.projectId` 已经是新值 ——
 * 直接写进 `items` 就是"新项目里显示着旧项目的还原点"，而界面上没有任何迹象
 * 说明这是错的（用户会照着它回滚到别的项目那一刻）。
 */
let generation = 0

async function load() {
  if (!props.projectId || loading.value) return
  const gen = generation
  loading.value = true
  error.value = ''
  // 状态与时间线并行：两者互不依赖，串行会让"打开还原点页"多等一个往返。
  // vcs 的失败**不**进 error —— 读不出状态只影响"要不要显示启用入口"，
  // 不该把整个时间线变成一条红字。
  const vcsPromise = getProjectVcs(props.projectId).catch(() => null)
  try {
    const list = await listProjectCheckpoints(props.projectId)
    if (gen !== generation) return
    items.value = list.checkpoints
    // 统计与列表一起回来；老网关没有 `stats` 时保持 null（头部不显示）。
    stats.value = list.stats
  } catch (e) {
    if (gen !== generation) return
    // 错误留在原地：用户正看着这块，弹到角落等于没说。
    error.value = describeApiError(e)
  } finally {
    // 旧代次的 finally 不能把新一代的 loading 关掉，否则新请求还在飞时界面
    // 会闪一下"还没有还原点"。
    if (gen === generation) loading.value = false
  }
  const state = await vcsPromise
  if (gen === generation) vcs.value = state
}

watch(
  () => props.projectId,
  () => {
    // 代次自增 + 清空，让所有在飞的旧请求失效。
    generation += 1
    items.value = []
    lastResult.value = null
    lastCheckpoint.value = null
    // 预览与统计跟着项目走：留着上一个项目的预览，用户会以为那是当前项目的差异。
    preview.value = null
    previewError.value = ''
    previewing.value = ''
    stats.value = null
    gcResult.value = null
    // vcs 必须一起清空：否则切到新项目的那一瞬间，界面上还挂着上一个项目的
    // 状态，用户会对着"A 目录没有版本控制"的提示去给 B 目录启用。
    vcs.value = null
    enabling.value = false
    error.value = ''
    loading.value = false
    void load()
  },
  { immediate: true },
)

/** 网关回的是 RFC3339；按界面语言本地化，解析失败就原样返回。 */
function when(iso: string): string {
  return iso ? formatSmart(iso, iso) : ''
}

function kindLabel(kind: string): string {
  if (kind === 'pre_turn') return tr('chat.checkpoint_kind_pre')
  if (kind === 'post_turn') return tr('chat.checkpoint_kind_post')
  return kind
}

/** 可回滚 = 状态 ok。其它状态（写失败/半截）一律禁用。 */
function canRestore(c: ProjectCheckpoint): boolean {
  return c.state === 'ok' && !restoring.value
}

/** 人类可读的字节数。只用于"这个点大概多大"这类估计，不追求精度。 */
function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let value = n
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value >= 10 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`
}

/** 预览里单栏最多列出的路径数：再多也是给人看的，全量塞进 DOM 只会卡。 */
const PREVIEW_PATH_LIMIT = 200

interface PreviewSection {
  key: string
  label: string
  paths: string[]
  tone: 'info' | 'warn'
}

/**
 * 把预览拆成几栏。**顺序固定**：改动 → 新建 → 会被删 → 因保护不删 → 不支持。
 *
 * 空栏不显示 —— 但 "`removed` 为空" 这件事本身有意义（说明回滚不会删东西），
 * 所以当所有栏都空时显示一句"没有差异"，而不是一片空白。
 */
function previewSections(p: CheckpointDiffPreview): PreviewSection[] {
  const sections: PreviewSection[] = [
    { key: 'restored', label: tr('chat.checkpoint_preview_restored'), paths: p.restored, tone: 'info' },
    { key: 'added', label: tr('chat.checkpoint_preview_added'), paths: p.added, tone: 'info' },
    { key: 'removed', label: tr('chat.checkpoint_preview_removed'), paths: p.removed, tone: 'warn' },
    { key: 'protected', label: tr('chat.checkpoint_preview_protected'), paths: p.protected_removed, tone: 'warn' },
    { key: 'unsupported', label: tr('chat.checkpoint_preview_unsupported'), paths: p.unsupported, tone: 'info' },
  ]
  return sections.filter(s => s.paths.length > 0)
}

/**
 * 预览差异（只读）。**不改任何文件**：引擎用临时索引/临时 CAS 取快照。
 *
 * 预览失败必须就地报错。静默当成"没有差异"是最坏的形态 —— 用户会因此放心地
 * 点下回滚，而工作区里等待他的是完全不同的结果。
 */
async function doPreview(c: ProjectCheckpoint) {
  if (!canRestore(c) || previewing.value) return
  previewing.value = c.id
  previewError.value = ''
  preview.value = null
  try {
    const result = await previewCheckpointDiff(c.id)
    preview.value = { checkpoint: c, result }
    // 预览响应里带着"当前快照大小"，顺手把头部指标刷成同一次实测的值。
    stats.value = stats.value ? { ...stats.value, ...result.stats } : result.stats
  } catch (e) {
    previewError.value = describeApiError(e)
  } finally {
    previewing.value = ''
  }
}

/**
 * 回收未跟踪 CAS（管理员）。**两步**：先 dry-run 看计划，确认后才真删。
 *
 * 为什么不在一次请求里直接删：`freed_bytes` 与要删的分片数只有算完才知道，
 * 用户需要看到"会删多少"再决定。后端也把缺省的 `dry_run` 当 true，两侧同口径。
 */
function requestGc() {
  if (!props.canAdmin || gcLoading.value) return
  void previewGc()
}

async function previewGc() {
  gcLoading.value = true
  gcResult.value = null
  try {
    const plan = await runCheckpointGc({ workspaceId: props.projectId, dryRun: true })
    gcResult.value = plan
    Modal.confirm({
      title: tr('chat.checkpoint_gc_confirm_title'),
      content: tr('chat.checkpoint_gc_confirm', {
        n: plan.deleted,
        size: fmtBytes(plan.freed_bytes),
        kept: plan.referenced,
      }),
      okText: tr('chat.checkpoint_gc_ok'),
      okButtonProps: { danger: true },
      cancelText: tr('common.cancel'),
      onOk: () => executeGc(),
    })
  } catch (e) {
    notify.error(describeApiError(e))
  } finally {
    gcLoading.value = false
  }
}

async function executeGc() {
  gcLoading.value = true
  try {
    const out = await runCheckpointGc({ workspaceId: props.projectId, dryRun: false })
    gcResult.value = out
    notify.success(tr('chat.checkpoint_gc_done', { n: out.deleted, size: fmtBytes(out.freed_bytes) }))
  } catch (e) {
    notify.error(describeApiError(e))
  } finally {
    gcLoading.value = false
  }
}

/**
 * 在项目根启用版本跟踪（写用户磁盘）。
 *
 * 走二次确认，且文案要说清代价：只建 `.git`、不提交任何东西、不改动任何文件
 * 内容。用户同意的是"让还原点能工作"，不是"把我的项目提交一遍" ——
 * 这两件事混淆会让人以为自己的代码被推送到了哪里。
 *
 * **成功后重新拉时间线**：之前那些 `state='failed'` 的点是"目录不在版本控制下"
 * 留下的，现在目录已经纳入版本控制，但旧点本身无法补救（快照当时就没写出来）——
 * 界面必须刷新，让用户看到的是"从现在起会正常记录"，而不是继续盯着旧失败点。
 */
function requestEnableVcs() {
  if (!props.projectId || enabling.value) return
  Modal.confirm({
    title: tr('chat.checkpoint_enable_vcs_title'),
    content: tr('chat.checkpoint_enable_vcs_confirm'),
    okText: tr('chat.checkpoint_enable_vcs_ok'),
    cancelText: tr('common.cancel'),
    onOk: () => doEnableVcs(),
  })
}

async function doEnableVcs() {
  if (!props.projectId || enabling.value) return
  enabling.value = true
  try {
    const state = await initProjectVcs(props.projectId)
    vcs.value = state
    if (state.repo) {
      notify.success(tr('chat.checkpoint_enable_vcs_done'))
      // 刷新时间线：失败态的点不会消失（它们真的没写出来），但用户需要
      // 一个"现在状态如何"的当前视图，而不是停在启用之前的快照上。
      void load()
    } else {
      // init 返回成功但目录仍不在工作树内（权限等）：如实说没成。
      notify.error(tr('chat.checkpoint_enable_vcs_failed'))
    }
  } catch (e) {
    notify.error(describeApiError(e))
  } finally {
    enabling.value = false
  }
}

function requestRestore(c: ProjectCheckpoint) {
  if (!canRestore(c)) return
  Modal.confirm({
    title: tr('chat.checkpoint_restore_title'),
    content: tr('chat.checkpoint_restore_confirm', {
      seq: c.seq,
      kind: kindLabel(c.kind),
      files: c.file_count,
    }),
    okText: tr('chat.checkpoint_restore_ok'),
    cancelText: tr('common.cancel'),
    onOk: () => doRestore(c),
  })
}

async function doRestore(c: ProjectCheckpoint) {
  if (restoring.value) return
  restoring.value = c.id
  lastResult.value = null
  lastCheckpoint.value = null
  try {
    const result = await restoreCheckpoint(c.id)
    lastResult.value = result
    lastCheckpoint.value = c
    if (result.ok) {
      notify.success(tr('chat.checkpoint_restored', { n: result.written.length + result.added.length }))
      emit('restored', result)
      // 重新拉一次：applied_at 变了，界面上要能看出这个点被用过。
      // **不 await**：刷新只是补一个时间戳标记，把它串在回滚后面会让按钮在
      // 列表请求回来之前一直显示"正在回滚…"——请求挂住时用户连再点一次都做不到。
      void load()
    } else {
      // 部分失败：不弹成功提示，让用户读就地展开的失败清单。
      notify.error(tr('chat.checkpoint_restore_partial', { n: result.failed.length }))
      emit('restored', result)
    }
  } catch (e) {
    const msg = describeApiError(e)
    lastResult.value = {
      ok: false,
      written: [], added: [], deleted: [], pruned_dirs: [],
      failed: [{ path: '', error: msg }],
      skipped_removed: [], protected_removed: [], unsupported: [],
    }
    notify.error(msg)
  } finally {
    restoring.value = ''
  }
}

/**
 * 删除「快照之后新增、第一次没删」的文件。
 *
 * 单独一步而不是第一次确认里的一个勾选框：这里要删的具体路径只有跑完第一次
 * 回滚才知道（网关没有 diff 预演接口），所以只能把它们摆出来让用户看着删。
 * `paths` 一并传下去，引擎就只会删这些路径，不会重写任何刚恢复的文件。
 */
function requestDeleteNew() {
  const c = lastCheckpoint.value
  const paths = lastResult.value?.skipped_removed ?? []
  if (!c || !paths.length || restoring.value) return
  const preview = paths.slice(0, 10).join('\n') + (paths.length > 10 ? '\n…' : '')
  Modal.confirm({
    title: tr('chat.checkpoint_delete_new_title'),
    content: `${tr('chat.checkpoint_delete_new_confirm', { n: paths.length })}\n\n${preview}`,
    okText: tr('chat.checkpoint_delete_new_ok'),
    okButtonProps: { danger: true },
    cancelText: tr('common.cancel'),
    onOk: () => doDeleteNew(),
  })
}

async function doDeleteNew() {
  const c = lastCheckpoint.value
  const paths = lastResult.value?.skipped_removed ?? []
  if (!c || !paths.length || restoring.value) return
  restoring.value = c.id
  try {
    const out = await restoreCheckpoint(c.id, { allowDelete: true, paths })
    // 用新结果整体替换：`skipped_removed` 现在应为空，`deleted` 里是真正删掉的。
    lastResult.value = out
    if (out.ok) {
      notify.success(tr('chat.checkpoint_deleted_new', { n: out.deleted.length }))
      emit('restored', out)
      void load()
    } else {
      notify.error(tr('chat.checkpoint_restore_partial', { n: out.failed.length }))
    }
  } catch (e) {
    notify.error(describeApiError(e))
  } finally {
    restoring.value = ''
  }
}
</script>

<template>
  <div
    v-if="!hasProject"
    class="cp-empty"
  >{{ tr('chat.workbench_no_project') }}</div>
  <div
    v-else-if="error"
    class="cp-error"
  >
    {{ error }}
    <button
      type="button"
      class="cp-btn"
      @click="load"
    >{{ tr('common.retry') }}</button>
  </div>
  <div
    v-else-if="loading && !items.length"
    class="cp-empty"
  >{{ tr('common.loading') }}</div>
  <template v-else>
    <!-- 头部指标：近 30 天的单检查点平均规模（来自 PG 的清单），以及（预览过之后）
         当前工作区快照与未跟踪 CAS 的实测规模。用户点回滚前先知道"面对多大"。 -->
    <div
      v-if="stats"
      class="cp-stats"
    >
      <span v-if="stats.sample_count">
        {{ tr('chat.checkpoint_stats_avg', {
          files: Math.round(stats.avg_file_count),
          size: fmtBytes(stats.avg_byte_size),
          days: stats.window_days,
        }) }}
      </span>
      <span
        v-else
        class="cp-warn"
      >{{ tr('chat.checkpoint_stats_none') }}</span>
      <span
        v-if="stats.current_files"
      >{{ tr('chat.checkpoint_stats_current', {
        files: stats.current_files,
        size: fmtBytes(stats.current_bytes ?? 0),
      }) }}</span>
      <span
        v-else-if="stats.store_files"
      >{{ tr('chat.checkpoint_stats_store', {
        files: stats.store_files,
        size: fmtBytes(stats.store_bytes ?? 0),
      }) }}</span>
    </div>
    <!-- 版本控制提示：项目目录不在 git 工作树里时，还原点**永远**只能是
         `state='failed'`（引擎快照第一步就报 not a git repository）。所以这里
         必须在"还没有还原点"之前说清楚根因，并给出一次性的启用入口 ——
         只显示一句"还没有还原点"会让用户一直等一个永远不会出现的点。 -->
    <div
      v-if="needsVcs"
      class="cp-vcs"
    >
      <div class="cp-vcs-text">{{ tr('chat.checkpoint_need_vcs') }}</div>
      <button
        type="button"
        class="cp-btn cp-btn-primary"
        :disabled="enabling"
        @click="requestEnableVcs"
      >
        {{ enabling ? tr('chat.checkpoint_enable_vcs_running') : tr('chat.checkpoint_enable_vcs') }}
      </button>
    </div>
    <div
      v-if="!items.length"
      class="cp-empty"
    >{{ tr('chat.checkpoint_empty') }}</div>
    <div
      v-for="c in items"
      :key="c.id"
      class="cp-row"
    >
      <div class="cp-head">
        <span class="cp-seq">#{{ c.seq }}</span>
        <span class="cp-kind">{{ kindLabel(c.kind) }}</span>
        <!-- 用户/系统起过名字就显示名字，否则只有 kind -->
        <span
          v-if="c.label"
          class="cp-label"
        >{{ c.label }}</span>
        <!-- 被回滚到过：单独标出来，避免用户重复点同一个点 -->
        <span
          v-if="c.applied_at"
          class="cp-badge"
          :title="tr('chat.checkpoint_applied_at', { at: when(c.applied_at) })"
        >{{ tr('chat.checkpoint_applied') }}</span>
        <span
          v-if="c.state !== 'ok'"
          class="cp-badge cp-badge-warn"
          :title="tr('chat.checkpoint_not_ok', { state: c.state })"
        >{{ c.state }}</span>
        <span class="cp-time">{{ when(c.created_at) }}</span>
      </div>
      <div class="cp-meta">
        <span>{{ tr('chat.checkpoint_files', { n: c.file_count }) }}</span>
        <!-- 跳过数非 0 意味着这个点还原出来**不完整**，必须说出来 -->
        <span
          v-if="c.skipped_count"
          class="cp-warn"
        >{{ tr('chat.checkpoint_skipped', { n: c.skipped_count }) }}</span>
        <!-- 创建失败过：错误码是唯一能告诉用户"为什么这一刻没保护成功"的线索 -->
        <span
          v-if="c.error_code"
          class="cp-warn"
        >{{ c.error_code }}</span>
      </div>
      <!-- 失败原因：只有错误码时用户仍不知道"为什么"。单独一行，长文本可换行，
           不挤在 meta 行里把文件数等有用信息推走。 -->
      <div
        v-if="c.error_msg"
        class="cp-reason"
      >{{ tr('chat.checkpoint_error_reason', { reason: c.error_msg }) }}</div>
      <div class="cp-actions">
        <!-- 先看差异，再决定回滚。预览是只读的，所以不弹二次确认；
             `state !== 'ok'` 的点同样禁用 —— 半截快照的"差异"没有意义。 -->
        <button
          type="button"
          class="cp-btn"
          :disabled="!canRestore(c) || !!previewing"
          :aria-busy="previewing === c.id"
          @click="doPreview(c)"
        >
          {{ previewing === c.id ? tr('chat.checkpoint_preview_running') : tr('chat.checkpoint_preview') }}
        </button>
        <button
          type="button"
          class="cp-btn cp-btn-primary"
          :disabled="!canRestore(c)"
          :aria-busy="restoring === c.id"
          :title="c.state === 'ok' ? '' : tr('chat.checkpoint_cannot_restore')"
          @click="requestRestore(c)"
        >
          {{ restoring === c.id ? tr('chat.checkpoint_restoring') : tr('chat.checkpoint_restore') }}
        </button>
      </div>
    </div>

    <!-- 差异预览抽屉（就地展开）。路径清单 + 每栏计数，空栏不显示。 -->
    <div
      v-if="previewError"
      class="cp-preview is-bad"
    >
      <div class="cp-preview-head">
        {{ tr('chat.checkpoint_preview_failed') }}
      </div>
      <div class="cp-result-line">
        {{ previewError }}
      </div>
    </div>
    <div
      v-else-if="preview"
      class="cp-preview"
    >
      <div class="cp-preview-head">
        <span>{{ tr('chat.checkpoint_preview_title', {
          seq: preview.checkpoint.seq,
          kind: kindLabel(preview.checkpoint.kind),
        }) }}</span>
        <button
          type="button"
          class="cp-btn"
          :title="tr('common.close')"
          @click="preview = null"
        >
          ×
        </button>
      </div>
      <div class="cp-result-line">
        {{ tr('chat.checkpoint_preview_counts', {
          restored: preview.result.restored.length,
          added: preview.result.added.length,
          removed: preview.result.removed.length,
        }) }}
      </div>
      <!-- 清单读不到：预览只覆盖 git 跟踪的文件。必须说出来，否则"没有差异"
           会被理解成"回滚不会动任何东西"。 -->
      <div
        v-if="preview.result.untracked_omitted"
        class="cp-result-line cp-result-warn"
      >{{ tr('chat.checkpoint_preview_omitted') }}</div>
      <div
        v-if="!previewSections(preview.result).length"
        class="cp-result-line"
      >{{ tr('chat.checkpoint_preview_empty') }}</div>
      <div
        v-for="section in previewSections(preview.result)"
        :key="section.key"
        class="cp-preview-section"
      >
        <div
          class="cp-preview-label"
          :class="{ 'cp-result-warn': section.tone === 'warn' }"
        >
          {{ section.label }} ({{ section.paths.length }})
        </div>
        <ul class="cp-preview-paths">
          <li
            v-for="p in section.paths.slice(0, PREVIEW_PATH_LIMIT)"
            :key="p"
          >
            {{ p }}
          </li>
        </ul>
        <div
          v-if="section.paths.length > PREVIEW_PATH_LIMIT"
          class="cp-result-line"
        >
          …
        </div>
      </div>
    </div>

    <!-- 回滚结果就地展开：动了哪些文件、哪些没删、哪些失败 -->
    <div
      v-if="lastResult"
      class="cp-result"
      :class="{ 'is-bad': !lastResult.ok }"
    >
      <div class="cp-result-head">
        {{ lastResult.ok ? tr('chat.checkpoint_result_ok') : tr('chat.checkpoint_result_partial') }}
      </div>
      <div
        v-if="lastResult.written.length"
        class="cp-result-line"
      >{{ tr('chat.checkpoint_result_written', { n: lastResult.written.length }) }}</div>
      <div
        v-if="lastResult.added.length"
        class="cp-result-line"
      >{{ tr('chat.checkpoint_result_added', { n: lastResult.added.length }) }}</div>
      <div
        v-if="lastResult.deleted.length"
        class="cp-result-line"
      >{{ tr('chat.checkpoint_result_deleted', { n: lastResult.deleted.length }) }}</div>
      <!-- 没删的路径必须列出来：否则用户以为工作区已经完全回到那一刻 -->
      <div
        v-if="lastResult.skipped_removed.length"
        class="cp-result-line cp-result-warn"
      >
        {{ tr('chat.checkpoint_result_skipped', { n: lastResult.skipped_removed.length }) }}
      </div>
      <!-- 命中保护名单（.env / 密钥）而没删：这不是失败，但用户必须知道 -->
      <div
        v-if="lastResult.protected_removed.length"
        class="cp-result-line cp-result-warn"
      >{{ tr('chat.checkpoint_result_protected', { n: lastResult.protected_removed.length }) }}</div>
      <!-- 符号链接等引擎不做处理：不列出来会被误当成"已经全部恢复了" -->
      <div
        v-if="lastResult.unsupported.length"
        class="cp-result-line"
      >{{ tr('chat.checkpoint_result_unsupported', { n: lastResult.unsupported.length }) }}</div>
      <button
        v-if="lastResult.skipped_removed.length"
        type="button"
        class="cp-btn cp-btn-danger"
        :disabled="!!restoring"
        @click="requestDeleteNew"
      >
        {{ restoring ? tr('chat.checkpoint_restoring') : tr('chat.checkpoint_delete_new', { n: lastResult.skipped_removed.length }) }}
      </button>
      <ul
        v-if="lastResult.failed.length"
        class="cp-result-failed"
      >
        <li
          v-for="(f, i) in lastResult.failed"
          :key="i"
        >{{ f.path || '-' }}: {{ f.error }}</li>
      </ul>
    </div>

    <!-- 回收入口：仅管理员可见（真正的权限由后端兜底）。默认只是预演，
         真删要再确认一次 —— 它清的是本机所有项目共享的未跟踪缓存。 -->
    <div
      v-if="canAdmin"
      class="cp-gc"
    >
      <button
        type="button"
        class="cp-btn"
        :disabled="gcLoading"
        :aria-busy="gcLoading"
        @click="requestGc"
      >
        {{ gcLoading ? tr('chat.checkpoint_gc_running') : tr('chat.checkpoint_gc') }}
      </button>
      <div
        v-if="gcResult"
        class="cp-result-line"
      >
        {{ tr('chat.checkpoint_gc_result', {
          n: gcResult.deleted,
          size: fmtBytes(gcResult.freed_bytes),
          kept: gcResult.referenced,
        }) }}
        <span v-if="gcResult.dry_run">{{ tr('chat.checkpoint_gc_dry') }}</span>
      </div>
    </div>
  </template>
</template>

<style scoped>
.cp-empty { padding: 10px 12px; font-size: 12px; color: var(--text-tertiary); }
/* 版本控制提示块：它是"还原点为什么记不下来"的唯一解释，视觉上不能与普通空态
   混在一起。用逻辑属性写内外边距，RTL 下同样成立。 */
.cp-vcs {
  margin-block: 8px;
  margin-inline: 12px;
  padding: 8px 10px;
  border-radius: 4px;
  background: var(--bg-card);
  font-size: 12px;
}
.cp-vcs-text {
  color: var(--text-secondary);
  line-height: 1.5;
}
.cp-vcs .cp-btn-primary { margin-block-start: 6px; }
.cp-error { padding: 10px 12px; font-size: 12px; color: var(--danger); }
.cp-row {
  padding: 6px 12px;
  border-bottom: var(--hairline-width) solid var(--border-subtle);
  font-size: 12px;
}
.cp-head { display: flex; align-items: center; gap: 6px; }
.cp-seq { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.cp-kind { color: var(--text-primary); }
.cp-label { color: var(--text-secondary); }
.cp-time { margin-inline-start: auto; color: var(--text-tertiary); }
.cp-badge {
  padding: 0 4px; border-radius: 3px; font-size: 11px;
  background: var(--bg-card); color: var(--text-secondary);
}
.cp-badge-warn { color: var(--danger); }
.cp-meta {
  display: flex; gap: 8px; margin-top: 2px; color: var(--text-tertiary);
}
.cp-warn { color: var(--warn, var(--danger)); }
/* 失败原因：可换行。只给上下留白，不写物理方向的 padding —— RTL 契约要求逻辑属性。 */
.cp-reason {
  margin-block-start: 2px;
  color: var(--warn, var(--danger));
  overflow-wrap: anywhere;
}
.cp-actions { margin-top: 4px; }
.cp-btn {
  border: none; background: transparent; cursor: pointer;
  padding: 2px 0; font-size: 12px; color: var(--text-secondary);
}
.cp-btn:hover:not(:disabled) { color: var(--text-primary); }
.cp-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.cp-btn-primary { color: var(--accent); }
.cp-btn-danger { color: var(--danger); margin-top: 4px; }
.cp-btn:focus-visible {
  outline: var(--focus-ring-width) solid var(--focus-ring-color);
  outline-offset: var(--focus-ring-offset);
}
.cp-result {
  margin: 8px 12px; padding: 8px;
  border-radius: 4px; font-size: 12px;
  background: var(--bg-card); color: var(--text-secondary);
}
.cp-result.is-bad { color: var(--danger); }
.cp-result-head { font-weight: 500; margin-bottom: 4px; }
.cp-result-line { line-height: 1.6; }
.cp-result-warn { color: var(--warn, var(--danger)); }
.cp-result-failed { margin: 4px 0 0; padding-inline-start: 18px; }
/* 头部指标：一行小字，不与时间线争注意力 */
.cp-stats {
  display: flex; flex-wrap: wrap; gap: 8px;
  padding: 6px 12px;
  font-size: 11px;
  color: var(--text-tertiary);
  border-bottom: var(--hairline-width) solid var(--border-subtle);
}
/* 预览面板：单独一块，展开时不挤动上面的行 */
.cp-preview {
  margin: 8px 12px; padding: 8px;
  border-radius: 4px; font-size: 12px;
  background: var(--bg-card); color: var(--text-secondary);
}
.cp-preview.is-bad { color: var(--danger); }
.cp-preview-head {
  display: flex; align-items: center; justify-content: space-between;
  font-weight: 500; margin-bottom: 4px;
}
.cp-preview-section { margin-top: 6px; }
.cp-preview-label { color: var(--text-secondary); }
/* 路径清单可能很长：限高 + 滚动，避免把回滚按钮推出视野 */
.cp-preview-paths {
  margin: 2px 0 0; padding-inline-start: 18px;
  max-height: 180px; overflow-y: auto;
  overflow-wrap: anywhere;
}
.cp-gc {
  padding: 8px 12px;
  border-top: var(--hairline-width) solid var(--border-subtle);
  font-size: 12px;
}
</style>
