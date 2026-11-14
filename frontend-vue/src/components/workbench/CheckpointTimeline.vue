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
 */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Modal } from 'ant-design-vue'
import {
  listProjectCheckpoints,
  restoreCheckpoint,
  type ProjectCheckpoint,
  type RestoreResult,
} from '../../api/checkpoints'
import { formatSmart } from '../../utils/datetime'
import { describeApiError } from '../../utils/apiError'
import { notify } from '../../utils/notify'

const { t: tr } = useI18n()

const props = withDefaults(
  defineProps<{
    /** 当前会话所属项目；空串 = 没有项目，不请求 */
    projectId?: string
  }>(),
  { projectId: '' },
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

const hasProject = computed(() => !!props.projectId)

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
  try {
    const list = await listProjectCheckpoints(props.projectId)
    if (gen !== generation) return
    items.value = list
  } catch (e) {
    if (gen !== generation) return
    // 错误留在原地：用户正看着这块，弹到角落等于没说。
    error.value = describeApiError(e)
  } finally {
    // 旧代次的 finally 不能把新一代的 loading 关掉，否则新请求还在飞时界面
    // 会闪一下"还没有还原点"。
    if (gen === generation) loading.value = false
  }
}

watch(
  () => props.projectId,
  () => {
    // 代次自增 + 清空，让所有在飞的旧请求失效。
    generation += 1
    items.value = []
    lastResult.value = null
    lastCheckpoint.value = null
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
      <div class="cp-actions">
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
  </template>
</template>

<style scoped>
.cp-empty { padding: 10px 12px; font-size: 12px; color: var(--text-tertiary); }
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
</style>
