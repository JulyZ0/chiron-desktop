<script setup lang="ts">
/**
 * 设置 →「版本」。
 *
 * 只做三件事：显示当前版本、检查官方发布清单、下载并拉起安装包。
 * 在线升级由桌面壳完成（`desktop:check-update` / `desktop:install-update`），
 * 安装包会先按发布清单校验 SHA-512 再执行；Web 端没有这条通道，直接说明。
 */
import { computed, onMounted, ref } from 'vue'
import { Button, Tag, message } from 'ant-design-vue'
import { CloudDownloadOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { useDeviceStore, type AppInfo, type UpdateCheckResult } from '../../stores/device'

const { t } = useI18n()
const device = useDeviceStore()

const info = ref<AppInfo | null>(null)
const checking = ref(false)
const installing = ref(false)
const result = ref<UpdateCheckResult | null>(null)
const checkedOnce = ref(false)

const supported = computed(() => Boolean(info.value) && typeof device.checkUpdate === 'function')

onMounted(async () => {
  if (typeof device.getAppInfo !== 'function') return
  info.value = await device.getAppInfo()
})

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
  if (installing.value || typeof device.installUpdate !== 'function') return
  installing.value = true
  try {
    const res = await device.installUpdate()
    if (res.ok) {
      message.success(t('settings.version_install_started'))
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
          v-if="result?.ok && result.hasUpdate"
          type="default"
          :loading="installing"
          @click="install"
        >
          <template #icon>
            <CloudDownloadOutlined />
          </template>
          {{ $t('settings.version_install') }}
        </Button>
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
</style>
