<template>
  <div class="deps-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.deps_title') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.deps_desc') }}
        </p>
      </div>
      <Button
        :loading="loading"
        @click="reload"
      >
        <template #icon>
          <ReloadOutlined />
        </template>
        {{ $t('common.refresh') }}
      </Button>
    </div>

    <Alert
      v-if="listError"
      class="block"
      type="error"
      show-icon
      :message="listError"
    />

    <div
      v-if="list"
      class="engine-meta"
    >
      <span>{{ $t('admin.deps_python_version', { version: list.python }) }}</span>
      <code class="engine-path">{{ list.executable }}</code>
    </div>

    <!-- 安装任务进度：pip 安装在引擎里后台跑，这里只轮询它的状态 -->
    <Card
      v-if="job"
      class="block"
      size="small"
      :title="$t('admin.deps_job_title')"
    >
      <div class="job-head">
        <Tag :color="jobColor">
          {{ $t(`admin.deps_job_${job.status}`) }}
        </Tag>
        <span class="job-target">{{ jobCommand }}</span>
      </div>
      <Progress
        :percent="job.progress"
        :status="jobProgressStatus"
        :show-info="false"
      />
      <p
        v-if="job.message"
        class="job-message"
      >
        {{ job.message }}
      </p>
      <pre
        v-if="job.output"
        class="job-log"
      >{{ job.output }}</pre>
    </Card>

    <!-- 快照与回滚：装/卸包之前引擎会自动落一份，这里也能手动拍 -->
    <Card
      class="block"
      size="small"
      :title="$t('admin.deps_snapshot_title')"
    >
      <template #extra>
        <Button
          size="small"
          type="primary"
          :disabled="busy || restoring || snapshotCreating"
          @click="openSnapshotModal"
        >
          {{ $t('admin.deps_snapshot_create') }}
        </Button>
      </template>
      <p class="card-desc">
        {{ $t('admin.deps_snapshot_desc', { retention: snapshotRetention }) }}
      </p>
      <Alert
        v-if="snapshotError"
        class="block-tight"
        type="error"
        show-icon
        :message="snapshotError"
      />
      <Spin :spinning="snapshotLoading">
        <Table
          :columns="snapshotColumns"
          :data-source="snapshots"
          :pagination="{ pageSize: 5, showSizeChanger: false, hideOnSinglePage: true }"
          :locale="{ emptyText: $t('admin.deps_snapshot_empty') }"
          row-key="id"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'created_at'">
              <span class="pkg-version">{{ formatSnapshotTime((record as DepSnapshot).created_at) }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'trigger'">
              <span class="snapshot-trigger">{{ (record as DepSnapshot).trigger }}</span>
              <div
                v-if="(record as DepSnapshot).label"
                class="snapshot-label"
              >
                {{ (record as DepSnapshot).label }}
              </div>
            </template>
            <template v-else-if="column.dataIndex === 'package_count'">
              <span class="pkg-version">{{ (record as DepSnapshot).package_count }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'actions'">
              <Button
                size="small"
                :loading="restoreTarget?.id === (record as DepSnapshot).id && (restorePreviewing || restoring)"
                :disabled="busy || restoring || snapshotCreating"
                @click="openRestore(record as DepSnapshot)"
              >
                {{ $t('admin.deps_restore') }}
              </Button>
            </template>
          </template>
        </Table>
      </Spin>
    </Card>

    <!-- 已安装包 -->
    <Card
      class="block"
      size="small"
      :title="$t('admin.deps_installed_title')"
    >
      <template #extra>
        <Input
          v-model:value="filter"
          class="filter-input"
          allow-clear
          :placeholder="$t('admin.deps_filter_placeholder')"
        />
      </template>
      <Spin :spinning="loading">
        <Table
          :columns="columns"
          :data-source="filteredPackages"
          :pagination="{ pageSize: 15, showSizeChanger: false, hideOnSinglePage: true }"
          :locale="{ emptyText: $t('admin.deps_installed_empty') }"
          row-key="name"
          size="small"
        >
          <template #bodyCell="{ column, record }">
            <template v-if="column.dataIndex === 'name'">
              <span class="pkg-name">{{ (record as DepPackage).name }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'version'">
              <span class="pkg-version">{{ (record as DepPackage).version }}</span>
            </template>
            <template v-else-if="column.dataIndex === 'status'">
              <!-- 可升级：只有在本次会话里搜过这个包、且 PyPI 版本更新时才提示 -->
              <Tag
                v-if="latestFor((record as DepPackage).name)"
                color="warning"
              >
                {{ $t('admin.deps_status_outdated', { version: latestFor((record as DepPackage).name) }) }}
              </Tag>
              <Tag
                v-else
                color="success"
              >
                {{ $t('admin.deps_status_installed') }}
              </Tag>
            </template>
            <template v-else-if="column.dataIndex === 'actions'">
              <Popconfirm
                :title="$t('admin.deps_uninstall_confirm', { name: (record as DepPackage).name })"
                :ok-text="$t('common.confirm')"
                :cancel-text="$t('common.cancel')"
                @confirm="uninstall((record as DepPackage).name)"
              >
                <Button
                  size="small"
                  danger
                  :loading="uninstalling === (record as DepPackage).name"
                  :disabled="busy || uninstalling !== ''"
                >
                  {{ $t('admin.deps_uninstall') }}
                </Button>
              </Popconfirm>
            </template>
          </template>
        </Table>
      </Spin>
    </Card>

    <!-- PyPI 查询与一键安装 -->
    <Card
      class="block"
      size="small"
      :title="$t('admin.deps_search_title')"
    >
      <p class="card-desc">
        {{ $t('admin.deps_search_desc') }}
      </p>
      <div class="search-row">
        <Input
          v-model:value="query"
          :placeholder="$t('admin.deps_search_placeholder')"
          allow-clear
          @press-enter="doSearch"
        />
        <Button
          type="primary"
          :loading="searching"
          :disabled="!query.trim()"
          @click="doSearch"
        >
          {{ $t('common.search') }}
        </Button>
      </div>

      <Alert
        v-if="searchError"
        class="block-tight"
        type="error"
        show-icon
        :message="searchError"
      />
      <Empty
        v-else-if="searchResult && !searchResult.results.length"
        :description="$t('admin.deps_search_not_found')"
      />
      <div
        v-for="item in searchResult?.results ?? []"
        :key="item.name"
        class="pypi-row"
      >
        <div class="pypi-main">
          <div class="pypi-head">
            <span class="pkg-name">{{ item.name }}</span>
            <span class="pkg-version">{{ item.version }}</span>
            <Tag
              v-if="installedVersion(item.name)"
              :color="isNewerVersion(item.version, installedVersion(item.name) ?? '') ? 'warning' : 'success'"
            >
              {{ isNewerVersion(item.version, installedVersion(item.name) ?? '')
                ? $t('admin.deps_status_upgradable')
                : $t('admin.deps_status_installed') }}
            </Tag>
          </div>
          <div class="pypi-summary">
            {{ item.summary || $t('admin.deps_no_summary') }}
          </div>
        </div>
        <Button
          type="primary"
          size="small"
          :disabled="busy || !isNewerVersion(item.version, installedVersion(item.name) ?? item.version)"
          @click="installFromSearch(item)"
        >
          {{ $t('admin.deps_install') }}
        </Button>
      </div>
    </Card>

    <!-- 命令行式安装：与搜索同一条校验规则 -->
    <Card
      class="block"
      size="small"
      :title="$t('admin.deps_cmd_title')"
    >
      <p class="card-desc">
        {{ $t('admin.deps_cmd_desc') }}
      </p>
      <div class="cmd-row">
        <span class="cmd-prefix">pip install</span>
        <Input
          v-model:value="manualSpec"
          class="cmd-input"
          :placeholder="$t('admin.deps_cmd_placeholder')"
          :status="specError ? 'error' : ''"
          @press-enter="submitManual"
        />
        <Button
          type="primary"
          :disabled="!!specError || busy"
          @click="submitManual"
        >
          {{ $t('admin.deps_install') }}
        </Button>
      </div>
      <p
        v-if="specError"
        class="cmd-error"
      >
        {{ $t(`admin.deps_error_${specError}`) }}
      </p>
      <p
        v-else
        class="cmd-hint"
      >
        {{ $t('admin.deps_cmd_hint') }}
      </p>
    </Card>

    <!-- 安装二次确认：装包会改动整台机器共享的运行环境，点一下就走太轻率 -->
    <Modal
      v-model:open="installModalOpen"
      :title="$t('admin.deps_install_confirm_title')"
      :ok-text="$t('admin.deps_install_confirm_ok')"
      :cancel-text="$t('common.cancel')"
      :confirm-loading="installSubmitting"
      class="install-confirm"
      @ok="confirmInstall"
    >
      <p class="modal-line">
        {{ $t('admin.deps_install_confirm_spec') }}
      </p>
      <pre class="modal-spec">{{ pendingSpec }}</pre>
      <Alert
        class="block-tight"
        type="warning"
        show-icon
        :message="$t('admin.deps_install_confirm_warning')"
      />
      <p class="modal-hint">
        {{ $t('admin.deps_install_confirm_snapshot') }}
      </p>
    </Modal>

    <!-- 手动创建快照 -->
    <Modal
      v-model:open="snapshotModalOpen"
      :title="$t('admin.deps_snapshot_create_title')"
      :ok-text="$t('admin.deps_snapshot_create')"
      :cancel-text="$t('common.cancel')"
      :confirm-loading="snapshotCreating"
      class="snapshot-create"
      @ok="submitSnapshot"
    >
      <p class="modal-line">
        {{ $t('admin.deps_snapshot_label_desc') }}
      </p>
      <Input
        v-model:value="snapshotLabel"
        :placeholder="$t('admin.deps_snapshot_label_placeholder')"
        :maxlength="120"
        @press-enter="submitSnapshot"
      />
    </Modal>

    <!-- 恢复前的差异确认：必须让用户看到"会卸载什么" -->
    <Modal
      v-model:open="restoreModalOpen"
      :title="$t('admin.deps_restore_title', { id: restoreTarget?.id ?? '' })"
      :ok-text="$t('admin.deps_restore_confirm')"
      :cancel-text="$t('common.cancel')"
      :ok-button-props="{ danger: true, disabled: !restoreDiff || restoreDiff.exceeds_limit }"
      :confirm-loading="restoring"
      class="restore-confirm"
      @ok="confirmRestore"
    >
      <Spin :spinning="restorePreviewing">
        <Alert
          v-if="restoreError"
          class="block-tight"
          type="error"
          show-icon
          :message="restoreError"
        />
        <template v-if="restoreDiff">
          <p class="modal-line">
            {{ $t('admin.deps_restore_summary', {
              install: restoreDiff.install_count,
              uninstall: restoreDiff.uninstall_count,
              changed: restoreDiff.changed.length,
            }) }}
          </p>
          <Alert
            v-if="restoreDiff.exceeds_limit"
            class="block-tight"
            type="error"
            show-icon
            :message="$t('admin.deps_restore_too_many', { limit: restoreDiff.limit, count: restoreDiff.install_count })"
          />
          <Alert
            v-else-if="restoreDiff.total_changes === 0"
            class="block-tight"
            type="info"
            show-icon
            :message="$t('admin.deps_restore_noop')"
          />
          <Alert
            v-else
            class="block-tight"
            type="warning"
            show-icon
            :message="$t('admin.deps_restore_warning')"
          />

          <div
            v-if="restoreDiff.extra.length"
            class="diff-block"
          >
            <div class="diff-title">
              {{ $t('admin.deps_restore_extra', { count: restoreDiff.extra.length }) }}
            </div>
            <div class="diff-items">
              <Tag
                v-for="pkg in restoreDiff.extra.slice(0, 12)"
                :key="`extra-${pkg.name}`"
                color="error"
              >
                {{ pkg.name }} {{ pkg.version }}
              </Tag>
              <span
                v-if="restoreDiff.extra.length > 12"
                class="diff-more"
              >{{ $t('admin.deps_snapshot_more', { count: restoreDiff.extra.length - 12 }) }}</span>
            </div>
          </div>
          <div
            v-if="restoreDiff.missing.length"
            class="diff-block"
          >
            <div class="diff-title">
              {{ $t('admin.deps_restore_missing', { count: restoreDiff.missing.length }) }}
            </div>
            <div class="diff-items">
              <Tag
                v-for="pkg in restoreDiff.missing.slice(0, 12)"
                :key="`missing-${pkg.name}`"
                color="processing"
              >
                {{ pkg.name }}=={{ pkg.version }}
              </Tag>
              <span
                v-if="restoreDiff.missing.length > 12"
                class="diff-more"
              >{{ $t('admin.deps_snapshot_more', { count: restoreDiff.missing.length - 12 }) }}</span>
            </div>
          </div>
          <div
            v-if="restoreDiff.changed.length"
            class="diff-block"
          >
            <div class="diff-title">
              {{ $t('admin.deps_restore_changed', { count: restoreDiff.changed.length }) }}
            </div>
            <div class="diff-items">
              <Tag
                v-for="item in restoreDiff.changed.slice(0, 12)"
                :key="`changed-${item.name}`"
                color="warning"
              >
                {{ item.name }} {{ item.from }} → {{ item.to }}
              </Tag>
              <span
                v-if="restoreDiff.changed.length > 12"
                class="diff-more"
              >{{ $t('admin.deps_snapshot_more', { count: restoreDiff.changed.length - 12 }) }}</span>
            </div>
          </div>
        </template>
      </Spin>
    </Modal>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Progress,
  Spin,
  Table,
  Tag,
  message,
} from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  createDepSnapshot,
  getDepJob,
  installDep,
  isNewerVersion,
  isTerminalJob,
  listDepSnapshots,
  listDeps,
  previewDepRestore,
  restoreDepSnapshot,
  searchPypi,
  uninstallDep,
  validatePipSpec,
  type DepJob,
  type DepListResult,
  type DepPackage,
  type DepRestoreDiff,
  type DepSearchResult,
  type DepSnapshot,
  type PypiResult,
} from '../../api/deps'
import { serverErrorDetail } from '../../utils/apiError'

const { t } = useI18n()

const loading = ref(false)
const listError = ref('')
const list = ref<DepListResult | null>(null)
const filter = ref('')

const query = ref('')
const searching = ref(false)
const searchError = ref('')
const searchResult = ref<DepSearchResult | null>(null)

const manualSpec = ref('')
const uninstalling = ref('')

const job = ref<DepJob | null>(null)
let pollTimer: ReturnType<typeof setTimeout> | null = null
let pollAttempts = 0

// ── 快照 ──
const snapshots = ref<DepSnapshot[]>([])
const snapshotRetention = ref(20)
const snapshotLoading = ref(false)
const snapshotError = ref('')
const snapshotCreating = ref(false)
const snapshotLabel = ref('')
const snapshotModalOpen = ref(false)

// ── 恢复（两步：先 dry_run 看差异，再确认执行）──
const restoreTarget = ref<DepSnapshot | null>(null)
const restoreDiff = ref<DepRestoreDiff | null>(null)
const restoreError = ref('')
const restoreModalOpen = ref(false)
const restorePreviewing = ref(false)
const restoring = ref(false)

// ── 安装二次确认 ──
const pendingSpec = ref('')
const installModalOpen = ref(false)
const installSubmitting = ref(false)

/** 一次安装任务的轮询上限：5 分钟（引擎侧安装超时也是 300s）。 */
const MAX_POLL_ATTEMPTS = 250
const POLL_INTERVAL_MS = 1200

const columns = computed(() => [
  { title: t('admin.deps_col_name'), dataIndex: 'name', key: 'name' },
  { title: t('admin.deps_col_version'), dataIndex: 'version', key: 'version', width: 160 },
  { title: t('admin.deps_col_status'), dataIndex: 'status', key: 'status', width: 180 },
  { title: t('admin.deps_col_actions'), dataIndex: 'actions', key: 'actions', width: 120 },
])

const snapshotColumns = computed(() => [
  { title: t('admin.deps_snapshot_col_time'), dataIndex: 'created_at', key: 'created_at', width: 170 },
  { title: t('admin.deps_snapshot_col_trigger'), dataIndex: 'trigger', key: 'trigger' },
  { title: t('admin.deps_snapshot_col_count'), dataIndex: 'package_count', key: 'package_count', width: 100 },
  { title: t('admin.deps_col_actions'), dataIndex: 'actions', key: 'actions', width: 110 },
])

const busy = computed(() => !!job.value && !isTerminalJob(job.value.status))

/** 任务卡片的标题行：install/uninstall 显示命令，restore 显示回滚到哪份快照。 */
const jobCommand = computed(() => {
  const current = job.value
  if (!current) return ''
  if (current.kind === 'install') return `pip install ${current.target}`
  if (current.kind === 'uninstall') return `pip uninstall ${current.target}`
  return `${t('admin.deps_restore')} ${current.target}`
})

const specError = computed(() => validatePipSpec(manualSpec.value))

/** 本地过滤：包列表可能有几百条，但过滤不该每按一个键就打一次引擎。 */
const filteredPackages = computed<DepPackage[]>(() => {
  const all = list.value?.packages ?? []
  const needle = filter.value.trim().toLowerCase()
  if (!needle) return all
  return all.filter(p => p.name.toLowerCase().includes(needle))
})

/** 本次会话里查到过的"最新版本"（只有搜过的包才有，见模板里的"可升级"徽标）。 */
const latestVersions = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const item of searchResult.value?.results ?? []) {
    if (item.name && item.version) map[item.name.toLowerCase()] = item.version
  }
  return map
})

const installedVersions = computed<Record<string, string>>(() => {
  const map: Record<string, string> = {}
  for (const p of list.value?.packages ?? []) {
    map[p.name.toLowerCase()] = p.version
  }
  return map
})

const jobColor = computed(() => {
  switch (job.value?.status) {
    case 'succeeded': return 'success'
    case 'failed': return 'error'
    case 'running': return 'processing'
    default: return 'default'
  }
})

const jobProgressStatus = computed<'success' | 'exception' | 'active' | 'normal'>(() => {
  switch (job.value?.status) {
    case 'succeeded': return 'success'
    case 'failed': return 'exception'
    case 'running': return 'active'
    default: return 'normal'
  }
})

function installedVersion(name: string): string | null {
  return installedVersions.value[name.toLowerCase()] ?? null
}

/** 有更新时返回 PyPI 版本，否则 null（模板据此决定徽标）。 */
function latestFor(name: string): string | null {
  const latest = latestVersions.value[name.toLowerCase()]
  const installed = installedVersion(name)
  if (!latest || !installed) return null
  return isNewerVersion(latest, installed) ? latest : null
}

async function reload() {
  loading.value = true
  listError.value = ''
  try {
    list.value = await listDeps()
  } catch (err) {
    // 失败时不清空 list：上一次的清单比一片空白更有用（常用于"引擎重启中"）。
    listError.value = serverErrorDetail(err, t('admin.deps_load_failed'))
  } finally {
    loading.value = false
  }
}

async function loadSnapshots() {
  snapshotLoading.value = true
  snapshotError.value = ''
  try {
    const result = await listDepSnapshots()
    snapshots.value = result.snapshots
    snapshotRetention.value = result.retention
  } catch (err) {
    snapshotError.value = serverErrorDetail(err, t('admin.deps_snapshot_load_failed'))
  } finally {
    snapshotLoading.value = false
  }
}

/** 时间戳按本地时间展示：快照 id 是 UTC，用户看的是本地。 */
function formatSnapshotTime(seconds: number): string {
  if (!seconds) return ''
  const date = new Date(seconds * 1000)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} `
    + `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function openSnapshotModal() {
  snapshotLabel.value = ''
  snapshotModalOpen.value = true
}

async function submitSnapshot() {
  snapshotCreating.value = true
  try {
    const created = await createDepSnapshot(snapshotLabel.value.trim())
    snapshotModalOpen.value = false
    message.success(t('admin.deps_snapshot_created', { count: created.package_count }))
    await loadSnapshots()
  } catch (err) {
    message.error(serverErrorDetail(err, t('admin.deps_action_failed')))
  } finally {
    snapshotCreating.value = false
  }
}

/** 第一步：只算差异（dry_run），把"会卸载什么"摆在用户面前。 */
async function openRestore(snapshot: DepSnapshot) {
  restoreTarget.value = snapshot
  restoreDiff.value = null
  restoreError.value = ''
  restoreModalOpen.value = true
  restorePreviewing.value = true
  try {
    const preview = await previewDepRestore(snapshot.id)
    restoreDiff.value = preview.diff
  } catch (err) {
    restoreError.value = serverErrorDetail(err, t('admin.deps_restore_preview_failed'))
  } finally {
    restorePreviewing.value = false
  }
}

/** 第二步：确认后真的执行（后台任务 + 轮询，与安装同一套）。 */
async function confirmRestore() {
  const target = restoreTarget.value
  if (!target || restorePreviewing.value) return
  if (!restoreDiff.value || restoreDiff.value.exceeds_limit) return
  if (restoreDiff.value.total_changes === 0) {
    restoreModalOpen.value = false
    message.info(t('admin.deps_restore_noop'))
    return
  }
  restoring.value = true
  try {
    const result = await restoreDepSnapshot(target.id)
    restoreModalOpen.value = false
    if (result.job) {
      stopPolling()
      job.value = result.job
      pollAttempts = 0
      schedulePoll()
    } else {
      message.info(t('admin.deps_restore_noop'))
      await reload()
      await loadSnapshots()
    }
  } catch (err) {
    message.error(serverErrorDetail(err, t('admin.deps_action_failed')))
  } finally {
    restoring.value = false
  }
}

async function doSearch() {
  const q = query.value.trim()
  if (!q) return
  searching.value = true
  searchError.value = ''
  searchResult.value = null
  try {
    searchResult.value = await searchPypi(q)
  } catch (err) {
    searchError.value = serverErrorDetail(err, t('admin.deps_search_failed'))
  } finally {
    searching.value = false
  }
}

/**
 * 安装前的二次确认。
 *
 * 装包会改动**整台机器共享**的运行环境（引擎自己的 venv），点一下就执行太轻率：
 * 用户至少该看到"装的是什么规格"，并知道"装完要重启才生效"。
 */
function askInstall(spec: string) {
  const invalid = validatePipSpec(spec)
  if (invalid) {
    message.error(t(`admin.deps_error_${invalid}`))
    return
  }
  pendingSpec.value = spec.trim()
  installModalOpen.value = true
}

function confirmInstall() {
  const spec = pendingSpec.value
  installModalOpen.value = false
  if (!spec) return
  void startInstall(spec)
}

/** 创建安装任务并开始轮询；`spec` 必须已通过校验（两处入口都先校验）。 */
async function startInstall(spec: string) {
  const invalid = validatePipSpec(spec)
  if (invalid) {
    message.error(t(`admin.deps_error_${invalid}`))
    return
  }
  stopPolling()
  installSubmitting.value = true
  try {
    job.value = await installDep(spec.trim())
    pollAttempts = 0
    schedulePoll()
  } catch (err) {
    message.error(serverErrorDetail(err, t('admin.deps_action_failed')))
  } finally {
    installSubmitting.value = false
  }
}

function installFromSearch(item: PypiResult) {
  // 用 `==` 钉住搜索结果里的版本：用户点的就是"这个版本"，不该被上游重新解析成别的。
  askInstall(`${item.name}==${item.version}`)
}

function submitManual() {
  if (specError.value) return
  askInstall(manualSpec.value)
}

function schedulePoll() {
  if (!job.value) return
  pollTimer = setTimeout(pollJob, POLL_INTERVAL_MS)
}

async function pollJob() {
  const current = job.value
  if (!current) return
  if (pollAttempts >= MAX_POLL_ATTEMPTS) {
    // 停止轮询但保留最后状态：任务可能仍在引擎里跑，前端不该假装失败。
    message.warning(t('admin.deps_job_poll_stopped'))
    return
  }
  pollAttempts += 1
  try {
    const updated = await getDepJob(current.id)
    job.value = updated
    if (isTerminalJob(updated.status)) {
      if (updated.status === 'succeeded') {
        message.success(t('admin.deps_job_succeeded_msg', { target: updated.target }))
      } else {
        message.error(t('admin.deps_job_failed_msg', { target: updated.target }))
      }
      // 装/卸/恢复之后包清单必然变了；失败也可能装了一半，刷新一次更接近事实。
      await reload()
      // 每次变更前引擎都会自动落一份快照，所以快照列表也变了。
      await loadSnapshots()
      return
    }
  } catch {
    // 轮询失败（引擎重启 / 任务被清理）时停止：继续打只会刷日志。
    message.warning(t('admin.deps_job_poll_stopped'))
    return
  }
  schedulePoll()
}

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer)
    pollTimer = null
  }
}

async function uninstall(name: string) {
  uninstalling.value = name
  try {
    await uninstallDep(name)
    message.success(t('admin.deps_uninstall_done', { name }))
    await reload()
    // 卸载前也会自动拍快照
    await loadSnapshots()
  } catch (err) {
    message.error(serverErrorDetail(err, t('admin.deps_action_failed')))
  } finally {
    uninstalling.value = ''
  }
}

onMounted(() => {
  void reload()
  void loadSnapshots()
})
onUnmounted(stopPolling)
</script>

<style scoped>
.deps-view { padding: 4px; }
.page-desc { margin: 4px 0 0; font-size: 12px; color: var(--text-secondary); }
.block { margin-bottom: 16px; }
.block-tight { margin: 8px 0; }
.card-desc { margin: 0 0 10px; font-size: 12px; color: var(--text-secondary); }

.engine-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 12px;
  color: var(--text-secondary);
}
.engine-path {
  padding: 2px 6px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle, transparent);
  border: var(--hairline-width) solid var(--border);
  font-size: 11px;
  word-break: break-all;
}

.job-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.job-target { font-size: 12px; color: var(--text-secondary); word-break: break-all; }
.job-message { margin: 6px 0 0; font-size: 12px; color: var(--text-secondary); }
.job-log {
  margin: 8px 0 0;
  padding: 8px 10px;
  max-height: 220px;
  overflow: auto;
  border-radius: var(--radius-lg);
  border: var(--hairline-width) solid var(--border);
  background: var(--bg-subtle, transparent);
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.filter-input { width: 220px; }
.pkg-name { font-weight: var(--fw-medium); word-break: break-all; }
.pkg-version { font-size: 12px; color: var(--text-secondary); font-variant-numeric: tabular-nums; }

.snapshot-trigger {
  font-size: 12px;
  color: var(--text-secondary);
  word-break: break-all;
  font-variant-numeric: tabular-nums;
}
.snapshot-label { margin-top: 2px; font-size: 12px; color: var(--text-primary); word-break: break-word; }

/* Modal 内容（差异摘要）：Modal 走 Teleport 到 body，所以这些类不能依赖父级作用域变量 */
.modal-line { margin: 0 0 6px; font-size: 12px; color: var(--text-secondary); }
.modal-spec {
  margin: 0 0 10px;
  padding: 6px 10px;
  border-radius: var(--radius-md);
  border: var(--hairline-width) solid var(--border);
  background: var(--bg-subtle, transparent);
  font-size: 12px;
  word-break: break-all;
  white-space: pre-wrap;
}
.modal-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-tertiary); }
.diff-block { margin-top: 10px; }
.diff-title { margin-bottom: 4px; font-size: 12px; font-weight: var(--fw-medium); }
.diff-items { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.diff-more { font-size: 12px; color: var(--text-tertiary); }

.search-row, .cmd-row { display: flex; gap: 8px; align-items: center; }
.search-row { margin-bottom: 10px; }
.cmd-prefix {
  flex: 0 0 auto;
  padding: 4px 8px;
  border-radius: var(--radius-md);
  background: var(--bg-subtle, transparent);
  border: var(--hairline-width) solid var(--border);
  font-size: 12px;
  color: var(--text-secondary);
}
.cmd-input { flex: 1 1 auto; }
.cmd-hint { margin: 8px 0 0; font-size: 12px; color: var(--text-tertiary); }
.cmd-error { margin: 8px 0 0; font-size: 12px; color: var(--error); }

.pypi-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 10px;
  border: var(--hairline-width) solid var(--border);
  border-radius: var(--radius-lg);
  margin-bottom: 8px;
  background: var(--bg-card);
}
.pypi-main { flex: 1 1 auto; min-width: 0; }
.pypi-head { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.pypi-summary {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-secondary);
  word-break: break-word;
}
</style>
