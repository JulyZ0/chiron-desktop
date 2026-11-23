<script setup lang="ts">
/**
 * 设置 →「版本」。
 *
 * 只做三件事：显示当前版本、检查官方发布清单、下载并拉起安装包。
 * 在线升级由桌面壳完成（`desktop:check-update` / `desktop:install-update`），
 * 安装包会先按发布清单校验 SHA-512 再执行；Web 端没有这条通道，直接说明。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { Button, Progress, Tag, message } from 'ant-design-vue'
import { CloudDownloadOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  useDeviceStore,
  type AppInfo,
  type UpdateCheckResult,
  type UpdateJobStatus,
} from '../../stores/device'

const { t } = useI18n()
const device = useDeviceStore()

const info = ref<AppInfo | null>(null)
const checking = ref(false)
const installing = ref(false)
const result = ref<UpdateCheckResult | null>(null)
const checkedOnce = ref(false)
// 后台升级任务由主进程持有；这里只订阅进度，关掉/重开设置页不会中断下载。
const job = ref<UpdateJobStatus | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null

const supported = computed(() => Boolean(info.value) && typeof device.checkUpdate === 'function')
const jobActive = computed(() =>
  job.value ? ['downloading', 'verifying', 'launching'].includes(job.value.state) : false,
)
const jobPaused = computed(() => job.value?.state === 'paused')
const percent = computed(() => {
  const total = job.value?.total || 0
  const done = job.value?.downloaded || 0
  if (total <= 0) return undefined
  return Math.min(100, Math.round((done / total) * 100))
})
const progressText = computed(() => {
  const total = job.value?.total || 0
  const done = job.value?.downloaded || 0
  if (total <= 0) return ''
  return t('settings.version_update_progress_in', {
    downloaded: formatMB(done),
    total: formatMB(total),
  })
})

function formatMB(bytes: number) {
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    const next = await device.getUpdateStatus?.()
    if (!next) {
      stopPolling()
      return
    }
    const prev = job.value
    job.value = next
    if (['downloading', 'verifying', 'launching'].includes(next.state)) return
    stopPolling()
    if (next.state === 'ready' && prev?.state !== 'ready') {
      message.success(t('settings.version_install_started'))
    } else if (next.state === 'error' && prev?.state !== 'error') {
      message.error(next.message || t('settings.version_install_failed'))
    }
  }, 1500)
}

/** 打开面板/重开设置页时恢复视图：下载中则继续轮询，暂停则展示"继续下载"。 */
async function syncJob() {
  const status = await device.getUpdateStatus?.()
  if (!status) return
  job.value = status
  if (['downloading', 'verifying', 'launching'].includes(status.state)) startPolling()
}

onMounted(async () => {
  if (typeof device.getAppInfo !== 'function') return
  info.value = await device.getAppInfo()
  await syncJob()
})

onUnmounted(stopPolling)

async function check() {
  if (checking.value || typeof device.checkUpdate !== 'function') return
  checking.value = true
  try {
    result.value = await device.checkUpdate()
    checkedOnce.value = true
    if (!result.value.ok) {
      message.error(result.value.message || t('settings.version_check_failed'))
    }
  } finally {
    checking.value = false
  }
}

async function install() {
  if (installing.value || jobActive.value || typeof device.installUpdate !== 'function') return
  installing.value = true
  try {
    const res = await device.installUpdate()
    if (res.ok) {
      if (res.status) job.value = res.status
      message.info(t('settings.version_install_started'))
      startPolling()
    } else {
      message.error(res.message || t('settings.version_install_failed'))
    }
  } finally {
    installing.value = false
  }
}
</script>

<template>
  <div class="version-panel">
    <div class="version-block">
      <div class="version-title">
        {{ $t('settings.version_current') }}
      </div>
      <div class="version-line">
        <span class="version-badge">Chiron</span>
        <strong class="version-number">{{ info?.version || '—' }}</strong>
        <Tag v-if="info && !info.packaged" color="orange">{{ $t('settings.version_development') }}</Tag>
      </div>
      <div v-if="info" class="version-meta">
        Electron {{ info.electron }} · Chromium {{ info.chrome }}
      </div>
      <p v-if="!supported" class="version-hint">
        {{ $t('settings.version_desktop_only') }}
      </p>
    </div>

    <div class="version-block">
      <div class="version-title">
        {{ $t('settings.version_update') }}
      </div>
      <p class="version-hint">
        {{ $t('settings.version_update_hint') }}
      </p>
      <div class="version-actions">
        <Button
          type="primary"
          :loading="checking"
          :disabled="!supported"
          @click="check"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('settings.version_check') }}
        </Button>
        <Button
          v-if="(result?.ok && result.hasUpdate) || (job && job.state !== 'idle')"
          type="default"
          :loading="installing || jobActive"
          :disabled="jobActive"
          @click="install"
        >
          <template #icon>
            <CloudDownloadOutlined />
          </template>
          {{ jobPaused ? $t('settings.version_update_resume') : $t('settings.version_install') }}
        </Button>
      </div>

      <div v-if="job && job.state !== 'idle'" class="version-progress">
        <Progress
          :percent="percent ?? 0"
          :status="job.state === 'error' ? 'exception' : jobActive ? 'active' : 'normal'"
          :show-info="percent !== undefined"
        />
        <div class="version-result">
          <Tag v-if="job.state === 'paused'" color="orange">
            {{ $t('settings.version_update_paused') }}
          </Tag>
          <Tag v-else-if="job.state === 'error'" color="red">
            {{ $t('settings.version_install_failed') }}
          </Tag>
          <span v-if="progressText">{{ progressText }}</span>
          <span v-if="job.message">{{ job.message }}</span>
        </div>
      </div>

      <div v-if="checkedOnce && result?.ok" class="version-result">
        <template v-if="result.hasUpdate">
          <Tag color="blue">{{ $t('settings.version_available') }}</Tag>
          <span>{{ $t('settings.version_latest', { version: result.latest || '' }) }}</span>
          <a
            v-if="result.releaseUrl"
            :href="result.releaseUrl"
            target="_blank"
            rel="noopener noreferrer"
          >{{ $t('settings.version_release_notes') }}</a>
        </template>
        <template v-else>
          <Tag color="green">{{ $t('settings.version_up_to_date') }}</Tag>
          <span>{{ $t('settings.version_current_is_latest') }}</span>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.version-block { padding-block: 4px 20px; }
.version-block + .version-block {
  border-top: var(--hairline-width) solid var(--border-card, var(--border));
  padding-top: 20px;
}
.version-title { font-weight: var(--fw-semibold); color: var(--text-primary); margin-bottom: 12px; }
.version-line { display: flex; align-items: center; gap: 10px; }
.version-badge {
  font-size: 12px; color: var(--text-secondary);
  border: 1px solid var(--border-card, var(--border));
  border-radius: 999px; padding: 2px 8px;
}
.version-number { font-size: 22px; font-variant-numeric: tabular-nums; }
.version-meta { margin-top: 6px; font-size: 12px; color: var(--text-tertiary); }
.version-hint { margin: 0 0 12px; color: var(--text-secondary); font-size: 13px; line-height: 1.7; }
.version-actions { display: flex; gap: 10px; flex-wrap: wrap; }
.version-result { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 12px; font-size: 13px; color: var(--text-secondary); }
.version-result a { color: var(--primary); }
.version-progress { margin-top: 14px; }
.version-progress :deep(.ant-progress) { margin-bottom: 6px; }
</style>
