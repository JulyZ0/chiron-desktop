<script setup lang="ts">
/**
 * 项目侧边栏：会话地图改造后的主入口。
 *
 * 与旧「扁平会话列表」的根本差别不是样式，而是**组织维度**：
 * 旧列表把所有会话按时间堆在一起，用户只能靠标题回想"这是哪个事的会话"；
 * 这里以**文件夹**为单位分组，会话属于哪个项目一眼可见。
 *
 * 交互约定（见 `docs/project-management-design.md` 5.1）：
 *   - 点项目行**只原地展开/折叠**，不跳页 —— 侧栏很窄，跳页会让用户失去上下文；
 *   - 点会话行才切会话（`open-session`）；
 *   - 删除项目不删会话（后端 I5），因此这里必须明确提示"会话会回到未归入"。
 *
 * 数据自取：这个组件不依赖父级传 projects —— 侧边栏挂载时机与 ChatView 的
 * 会话加载时机不同步，靠 props 传会让"新建项目后侧栏不刷新"变成必然。
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Button, Empty, Input, Modal, message } from 'ant-design-vue'
import {
  DownOutlined,
  FolderAddOutlined,
  FolderOpenOutlined,
  LoadingOutlined,
  PlusOutlined,
  RightOutlined,
} from '@ant-design/icons-vue'

import { describeApiError } from '../../utils/apiError'
import { formatRelativeTime } from '../../components/chat/chat-types'
import {
  bindConflictOwner,
  createProject,
  deleteProject,
  listProjectSessions,
  listProjects,
  listUnassignedSessions,
} from './projectsApi'
import type { ProjectSessionSummary, ProjectSummary } from './types'

const emit = defineEmits<{
  /** 切到某个会话 */
  (e: 'open-session', id: string): void
  /** 在项目内新建会话（父级带项目上下文） */
  (e: 'create-session', projectId: string): void
  /** 请求看某个项目的画布（降级为详情页次要视图） */
  (e: 'open-canvas', projectId: string): void
  /**
   * 某个会话的项目归属变了（绑定 / 移出 / 移动）。
   *
   * 不带会话 id 是有意的：消费方（ChatView）只需知道"当前会话的归属可能已经变了"
   * 并重查一次，而它本来就知道自己在看哪个会话。传 id 只会多一个与 `activeSessionId`
   * 比对的步骤，比错就静默失效。
   */
  (e: 'binding-changed'): void
}>()

const { t, locale } = useI18n()

const loading = ref(false)
const projects = ref<ProjectSummary[]>([])
const unassigned = ref<ProjectSessionSummary[]>([])
/** 已展开的项目 id → 该项目内的会话。懒加载：没展开的项目不发请求。 */
const sessionsByProject = ref<Record<string, ProjectSessionSummary[]>>({})
const expanded = ref<Set<string>>(new Set())
const loadingProject = ref<string>('')
const creating = ref(false)
const newName = ref('')
const createOpen = ref(false)

const hasAnything = computed(() => projects.value.length > 0 || unassigned.value.length > 0)

const tr = (key: string, params?: Record<string, unknown>) => t(key, params ?? {})

async function refresh() {
  loading.value = true
  try {
    const [p, u] = await Promise.all([listProjects(), listUnassignedSessions()])
    projects.value = p
    unassigned.value = u
    // 已展开的项目重新拉一次：新建会话后数量会变，不刷新会显示过期计数。
    await Promise.all([...expanded.value].map(id => loadSessions(id)))
  } catch (err) {
    message.error(describeApiError(err, tr('chat.projects_load_failed')))
  } finally {
    loading.value = false
  }
}

async function loadSessions(projectId: string) {
  loadingProject.value = projectId
  try {
    const list = await listProjectSessions(projectId)
    sessionsByProject.value = { ...sessionsByProject.value, [projectId]: list }
  } catch (err) {
    message.error(describeApiError(err, tr('chat.projects_sessions_load_failed')))
  } finally {
    loadingProject.value = ''
  }
}

/** 点项目行：只切换展开态。**不跳页** —— 跳页会让用户在窄侧栏里失去上下文。 */
async function toggle(projectId: string) {
  const next = new Set(expanded.value)
  if (next.has(projectId)) {
    next.delete(projectId)
  } else {
    next.add(projectId)
    if (!sessionsByProject.value[projectId]) await loadSessions(projectId)
  }
  expanded.value = next
}

function openCreate() {
  newName.value = ''
  createOpen.value = true
}

/**
 * 选文件夹后建项目。
 *
 * 桥接对象挂在 `chironDesktop` 上（见 `electron/preload.cjs` 的
 * `exposeInMainWorld('chironDesktop', ...)`），与 `api/index.ts` 读的是同一个全局名。
 * 这里曾误写成 `window.desktop` —— 那个名字从来不存在，于是桌面端也会被判成
 * 「浏览器环境」，每次点选文件夹都弹「需要在桌面端使用」。
 *
 * Web 构建没有这个能力：提示后返回，由用户改用其它入口，不在这里退化成一个
 * 手填路径的输入框 —— `root_path` 必须是本机真实存在的目录，网页里手打一个
 * Windows 路径既没法校验也对用户毫无帮助。
 */
async function pickAndCreate() {
  const bridge = (
    globalThis as {
      chironDesktop?: { pickDirectory?: () => Promise<{ path: string; name: string } | null> }
    }
  ).chironDesktop
  if (!bridge?.pickDirectory) {
    message.info(tr('chat.projects_desktop_only'))
    return
  }
  const picked = await bridge.pickDirectory()
  if (!picked) return
  creating.value = true
  try {
    const project = await createProject({
      root_path: picked.path,
      name: newName.value.trim() || picked.name,
    })
    createOpen.value = false
    projects.value = [...projects.value, project]
    // 新建后直接展开：用户刚建完就想归会话，让他少点一次。
    expanded.value = new Set([...expanded.value, project.id])
    sessionsByProject.value = { ...sessionsByProject.value, [project.id]: [] }
  } catch (err) {
    // 后端的 `invalid_root_path` / `forbidden_root_path` 都带具体原因，
    // 直接展示原文比"创建失败"有用得多。
    message.error(describeApiError(err, tr('chat.projects_create_failed')))
  } finally {
    creating.value = false
  }
}

/**
 * 把未归入的会话放进项目。
 *
 * 409 表示该会话已在别的项目：后端回 `owner_project_id`，这里提示后带 `force=true`
 * 重试即「移动」。直接静默 force 会让用户以为"只是加进去"，实际把原项目里删掉了。
 */
async function assign(sessionId: string, projectId: string) {
  const { bindSession } = await import('./projectsApi')
  try {
    await bindSession(projectId, sessionId, false)
    await refresh()
    await loadSessions(projectId)
    // 归属变了就要通知父级：被绑的可能正是**用户当前正在看**的那个会话，
    // 此时会话 id 不变，父级不会自己重查。
    emit('binding-changed')
    return
  } catch (err) {
    const owner = bindConflictOwner(err)
    if (!owner) {
      message.error(describeApiError(err, tr('chat.projects_bind_failed')))
      return
    }
    const ownerName = projects.value.find(p => p.id === owner)?.name || owner
    Modal.confirm({
      title: tr('chat.projects_move_title'),
      content: tr('chat.projects_move_body', { project: ownerName }),
      okText: tr('chat.projects_move_ok'),
      cancelText: tr('common.cancel'),
      onOk: async () => {
        await bindSession(projectId, sessionId, true)
        await refresh()
        await loadSessions(projectId)
        emit('binding-changed')
      },
    })
  }
}

async function removeFromProject(projectId: string, sessionId: string) {
  const { unbindSession } = await import('./projectsApi')
  await unbindSession(projectId, sessionId)
  await refresh()
  await loadSessions(projectId)
  emit('binding-changed')
  message.success(tr('chat.projects_unbind_done'))
}

function removeProject(project: ProjectSummary) {
  Modal.confirm({
    title: tr('chat.projects_delete_title', { name: project.name }),
    // 必须写清"会话不删"：I5 保证会话回到未归入分组，用户不该以为数据没了。
    content: tr('chat.projects_delete_body'),
    okText: tr('common.delete'),
    okType: 'danger',
    cancelText: tr('common.cancel'),
    onOk: async () => {
      await deleteProject(project.id)
      const next = new Set(expanded.value)
      next.delete(project.id)
      expanded.value = next
      await refresh()
      message.success(tr('chat.projects_delete_done'))
    },
  })
}

/** 时间显示走共享的相对时间函数：三个语言包的相对时间规则只有一份。 */
function relative(iso: string) {
  return iso ? formatRelativeTime(iso, (k: string, p?: Record<string, unknown>) => t(k, p ?? {})) : ''
}

onMounted(refresh)

defineExpose({ refresh })
</script>

<template>
  <div class="proj-panel">
    <div class="proj-toolbar">
      <span class="proj-heading">{{ tr('chat.projects_title') }}</span>
      <Button
        type="text"
        size="small"
        class="proj-add"
        :title="tr('chat.projects_new')"
        @click="openCreate"
      >
        <template #icon>
          <FolderAddOutlined />
        </template>
      </Button>
    </div>

    <div class="proj-scroll scroll-fade">
      <div
        v-if="loading && !hasAnything"
        class="proj-empty"
      >
        <LoadingOutlined /> {{ tr('common.loading') }}
      </div>
      <div
        v-else-if="!hasAnything"
        class="proj-empty"
      >
        <Empty
          :image="Empty.PRESENTED_IMAGE_SIMPLE"
          :description="tr('chat.projects_empty')"
        />
      </div>

      <template v-else>
        <!-- 项目分组 -->
        <div
          v-for="p in projects"
          :key="p.id"
          class="proj-group"
        >
          <div
            class="proj-row"
            role="button"
            tabindex="0"
            :aria-expanded="expanded.has(p.id)"
            @click="toggle(p.id)"
            @keydown.enter.prevent="toggle(p.id)"
            @keydown.space.prevent="toggle(p.id)"
          >
            <component
              :is="expanded.has(p.id) ? DownOutlined : RightOutlined"
              class="proj-caret"
            />
            <FolderOpenOutlined class="proj-icon" />
            <div class="proj-info">
              <span class="proj-name">{{ p.name }}</span>
              <!-- 路径是项目与画布的唯一区分标志，必须露出来 -->
              <span
                class="proj-path"
                :title="p.root_path"
              >{{ p.root_path }}</span>
            </div>
            <span class="proj-count">{{ p.session_count }}</span>
            <Button
              type="text"
              size="small"
              class="proj-act"
              :title="tr('chat.projects_open_canvas')"
              @click.stop="emit('open-canvas', p.id)"
            >{{ tr('common.map') }}</Button>
            <Button
              type="text"
              size="small"
              class="proj-act is-danger"
              :title="tr('chat.projects_delete')"
              @click.stop="removeProject(p)"
            >{{ tr('common.delete') }}</Button>
          </div>

          <div
            v-if="expanded.has(p.id)"
            class="proj-children"
          >
            <div
              v-if="loadingProject === p.id && !sessionsByProject[p.id]"
              class="proj-empty is-inline"
            >
              <LoadingOutlined /> {{ tr('common.loading') }}
            </div>
            <div
              v-else-if="!(sessionsByProject[p.id] || []).length"
              class="proj-empty is-inline"
            >
              {{ tr('chat.projects_no_sessions') }}
            </div>
            <div
              v-for="s in sessionsByProject[p.id] || []"
              :key="s.id"
              class="proj-session"
              role="button"
              tabindex="0"
              @click="emit('open-session', s.id)"
              @keydown.enter.prevent="emit('open-session', s.id)"
            >
              <span class="proj-session-title">{{ s.title || tr('chat.new_conversation') }}</span>
              <span class="proj-session-time">{{ relative(s.updated_at) }}</span>
              <Button
                type="text"
                size="small"
                class="proj-act"
                :title="tr('chat.projects_unbind')"
                @click.stop="removeFromProject(p.id, s.id)"
              >{{ tr('chat.projects_unbind_short') }}</Button>
            </div>
            <Button
              type="link"
              size="small"
              class="proj-new-session"
              @click="emit('create-session', p.id)"
            >
              <template #icon>
                <PlusOutlined />
              </template>
              {{ tr('chat.new_conversation') }}
            </Button>
          </div>
        </div>

        <!-- 未归入：改造前的存量会话全在这里，删掉这个分组等于让它们从侧栏消失 -->
        <div
          v-if="unassigned.length"
          class="proj-group is-unassigned"
        >
          <div class="proj-row is-static">
            <span class="proj-caret" />
            <div class="proj-info">
              <span class="proj-name">{{ tr('chat.projects_unassigned') }}</span>
            </div>
            <span class="proj-count">{{ unassigned.length }}</span>
          </div>
          <div class="proj-children">
            <div
              v-for="s in unassigned"
              :key="s.id"
              class="proj-session"
              role="button"
              tabindex="0"
              @click="emit('open-session', s.id)"
              @keydown.enter.prevent="emit('open-session', s.id)"
            >
              <span class="proj-session-title">{{ s.title || tr('chat.new_conversation') }}</span>
              <span class="proj-session-time">{{ relative(s.updated_at) }}</span>
              <select
                class="proj-assign"
                :aria-label="tr('chat.projects_assign_to')"
                :value="''"
                @click.stop
                @change="(e: Event) => assign(s.id, (e.target as HTMLSelectElement).value)"
              >
                <option value="">
                  {{ tr('chat.projects_assign_to') }}
                </option>
                <option
                  v-for="p in projects"
                  :key="p.id"
                  :value="p.id"
                >{{ p.name }}</option>
              </select>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- 新建项目：目录由系统对话框选，只额外允许改名 -->
    <Modal
      v-model:open="createOpen"
      :title="tr('chat.projects_new')"
      :ok-text="tr('chat.projects_pick_folder')"
      :cancel-text="tr('common.cancel')"
      :confirm-loading="creating"
      @ok="pickAndCreate"
    >
      <Input
        v-model:value="newName"
        :placeholder="tr('chat.projects_name_placeholder')"
        :aria-label="tr('chat.projects_name_placeholder')"
      />
      <p class="proj-hint">
        {{ tr('chat.projects_pick_hint') }}
      </p>
    </Modal>
  </div>
</template>

<style scoped>
.proj-panel { display: flex; flex-direction: column; min-height: 0; flex: 1; }
.proj-toolbar { flex: none; display: flex; align-items: center; justify-content: space-between; padding: 6px 12px 2px; }
.proj-heading { font-size: 12px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
.proj-scroll { flex: 1; overflow-y: auto; padding: 4px 6px 12px; scrollbar-width: thin; scrollbar-color: var(--text-disabled) transparent; }
.proj-empty { padding: 16px 10px; text-align: center; color: var(--text-tertiary); font-size: 12px; }
.proj-empty.is-inline { padding: 8px 10px; text-align: start; }
.proj-group { margin-bottom: 2px; }
.proj-row { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border-radius: var(--radius-sm, 6px); cursor: pointer; }
.proj-row:hover { background: var(--bg-hover); }
.proj-row.is-static { cursor: default; }
.proj-caret { width: 12px; flex: none; font-size: 10px; color: var(--text-tertiary); }
.proj-icon { flex: none; color: var(--text-secondary); }
.proj-info { display: flex; flex-direction: column; min-width: 0; flex: 1; }
.proj-name { font-size: 13px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proj-path { font-size: 11px; color: var(--text-tertiary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: ltr; }
.proj-count { flex: none; font-size: 11px; color: var(--text-tertiary); min-width: 16px; text-align: center; }
.proj-act { flex: none; font-size: 11px; padding: 0 4px; color: var(--text-secondary); }
.proj-act.is-danger { color: var(--color-danger, var(--text-secondary)); }
.proj-children { padding-inline-start: 24px; }
.proj-session { display: flex; align-items: center; gap: 6px; padding: 5px 8px; border-radius: var(--radius-sm, 6px); cursor: pointer; }
.proj-session:hover { background: var(--bg-hover); }
.proj-session-title { flex: 1; min-width: 0; font-size: 12px; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.proj-session-time { flex: none; font-size: 11px; color: var(--text-tertiary); }
.proj-new-session { padding-inline-start: 4px; font-size: 12px; }
.proj-assign { flex: none; max-width: 84px; font-size: 11px; color: var(--text-secondary); background: transparent; border: 1px solid var(--border-color); border-radius: var(--radius-sm, 6px); padding: 1px 2px; }
.proj-hint { margin-top: 8px; font-size: 12px; color: var(--text-tertiary); }
</style>
