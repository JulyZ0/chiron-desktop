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
        <span class="job-target">{{ job.kind === 'install' ? 'pip install' : 'pip uninstall' }} {{ job.target }}</span>
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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Alert, Button, Card, Empty, Input, Popconfirm, Progress, Spin, Table, Tag, message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  getDepJob,
  installDep,
  isNewerVersion,
  isTerminalJob,
  listDeps,
  searchPypi,
  uninstallDep,
  validatePipSpec,
  type DepJob,
  type DepListResult,
  type DepPackage,
  type DepSearchResult,
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

/** 一次安装任务的轮询上限：5 分钟（引擎侧安装超时也是 300s）。 */
const MAX_POLL_ATTEMPTS = 250
const POLL_INTERVAL_MS = 1200

const columns = computed(() => [
  { title: t('admin.deps_col_name'), dataIndex: 'name', key: 'name' },
  { title: t('admin.deps_col_version'), dataIndex: 'version', key: 'version', width: 160 },
  { title: t('admin.deps_col_status'), dataIndex: 'status', key: 'status', width: 180 },
  { title: t('admin.deps_col_actions'), dataIndex: 'actions', key: 'actions', width: 120 },
])

const busy = computed(() => !!job.value && !isTerminalJob(job.value.status))

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

/** 创建安装任务并开始轮询；`spec` 必须已通过校验（两处入口都先校验）。 */
async function startInstall(spec: string) {
  const invalid = validatePipSpec(spec)
  if (invalid) {
    message.error(t(`admin.deps_error_${invalid}`))
    return
  }
  stopPolling()
  try {
    job.value = await installDep(spec.trim())
    pollAttempts = 0
    schedulePoll()
  } catch (err) {
    message.error(serverErrorDetail(err, t('admin.deps_action_failed')))
  }
}

function installFromSearch(item: PypiResult) {
  // 用 `==` 钉住搜索结果里的版本：用户点的就是"这个版本"，不该被上游重新解析成别的。
  void startInstall(`${item.name}==${item.version}`)
}

function submitManual() {
  if (specError.value) return
  void startInstall(manualSpec.value)
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
      // 装/卸成功后包清单必然变了；失败也可能装了一半，刷新一次更接近事实。
      await reload()
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
  } catch (err) {
    message.error(serverErrorDetail(err, t('admin.deps_action_failed')))
  } finally {
    uninstalling.value = ''
  }
}

onMounted(reload)
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
