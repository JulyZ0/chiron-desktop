<script setup lang="ts">
/**
 * 引擎功能开关 —— 设置页「引擎功能开关」标签页（仅桌面端）。
 *
 * ## 这个面板解决的问题
 *
 * 引擎有一批开关只认**进程环境变量**（`python-engine/app/config.py` 是
 * pydantic-settings，字段名大写即变量名）。桌面端把引擎当子进程拉起，而用户从
 * 开始菜单启动时**没有 shell** 可设 —— 于是这些开关在安装包里恒为默认值。
 *
 * 最直接的后果是 `superagent_takeover_enabled`（默认 `false`）：用户在界面上配了
 * 定时接管，引擎从不巡检，任务一条也不会被执行，而链路上没有任何报错。
 * 以前唯一的出路是"改服务端环境变量再重启"—— 可桌面端根本没有服务端。
 *
 * ## 为什么这里只画开关、不给任意 KEY=VALUE 输入框
 *
 * 白名单由主进程下发（`ENGINE_TOGGLES`），其中包括 `APP_SECRET`、`SANDBOX_BACKEND`
 * 在内的其它 env **不可由界面改写**：那些是进程的安全边界，改掉等于关掉鉴权或隔离。
 * 前端只是照着规格渲染，不自行决定"哪些能改"。
 *
 * ## 保存与生效是两步
 *
 * 保存只写盘（`engine-env.json`），重启本地后端才会生效。**不自动重启**：
 * 重启会打断正在进行的对话 —— 那必须由用户显式点。保存成功后把「立即重启」摆出来，
 * 避免用户以为已经生效。
 */
import { computed, onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { useDeviceStore } from '../../stores/device'

const { t } = useI18n()
const device = useDeviceStore()

const loading = ref(false)
const saving = ref(false)
const restarting = ref(false)
/** 主进程下发的开关规格（白名单）；空数组 + unsupported 表示外壳不支持 */
const specs = ref<Array<{ env: string; type: 'boolean'; default: boolean }>>([])
/** 编辑中的值：缺省项表示"没配过，用引擎默认" */
const draft = ref<Record<string, boolean>>({})
/** 已落盘的值：用来判断是否有未保存改动 */
const saved = ref<Record<string, boolean>>({})
/** 外壳不支持（老 preload 没有这两个 IPC） */
const unsupported = ref(false)
/** 有改动但还没重启：提示条只在真正需要时出现 */
const needsRestart = ref(false)

const dirty = computed(() => JSON.stringify(draft.value) !== JSON.stringify(saved.value))

/** 开关当前值：没配过时按引擎默认值显示（否则界面会显示成"关"而误导用户） */
function valueOf(spec: { env: string; default: boolean }): boolean {
  const explicit = draft.value[spec.env]
  return explicit === undefined ? spec.default : explicit
}

/** 该项是否被用户显式设置过 —— 与"跟着默认值走"必须能区分 */
function isSet(spec: { env: string }): boolean {
  return draft.value[spec.env] !== undefined
}

/**
 * 文案键：env 名转小写。引擎与白名单用大写是为了对齐 pydantic 字段名，
 * 而 i18n 键用小写是约定俗成；两者不一致会静默显示成键名本身。
 */
function labelKey(env: string): string {
  return `settings.engine_env_${env.toLowerCase()}`
}

function toggle(spec: { env: string; default: boolean }, next: boolean): void {
  draft.value = { ...draft.value, [spec.env]: next }
}

/** 恢复为引擎默认值（把显式设置删掉，而不是写一个 false） */
function resetToDefault(spec: { env: string }): void {
  const next = { ...draft.value }
  delete next[spec.env]
  draft.value = next
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const result = await device.loadEngineEnvSettings()
    if (!result) {
      unsupported.value = true
      return
    }
    if (!result.ok) {
      message.error(result.message || t('settings.engine_env_load_failed'))
      return
    }
    specs.value = result.toggles ?? []
    draft.value = { ...(result.values ?? {}) }
    saved.value = { ...(result.values ?? {}) }
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  saving.value = true
  try {
    const result = await device.saveEngineEnvSettings(draft.value)
    if (!result.ok) {
      message.error(
        t('settings.engine_env_save_failed', {
          detail: result.message || result.reason || 'unknown',
        }),
      )
      return
    }
    // 用主进程写盘后的真相回填：界面不该显示"我以为是这个值"
    const written = { ...(result.values ?? {}) }
    draft.value = { ...written }
    saved.value = { ...written }
    needsRestart.value = true
    message.success(t('settings.engine_env_saved'))
  } finally {
    saving.value = false
  }
}

async function restartBackend(): Promise<void> {
  restarting.value = true
  try {
    const result = await device.restartLocalBackend()
    if (!result.ok) {
      message.error(
        t('settings.engine_env_restart_failed', {
          detail: result.message || result.reason || 'unknown',
        }),
      )
      return
    }
    needsRestart.value = false
    message.success(t('settings.engine_env_restarted'))
  } finally {
    restarting.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <div class="engine-env-panel">
    <a-alert
      type="info"
      show-icon
      class="engine-env-intro"
      :message="$t('settings.engine_env_title')"
      :description="$t('settings.engine_env_intro')"
    />

    <a-alert
      v-if="unsupported"
      type="warning"
      show-icon
      :message="$t('settings.engine_env_unsupported')"
    />

    <a-spin v-else :spinning="loading">
      <div v-if="needsRestart" class="engine-env-restart">
        <span class="engine-env-restart-text">{{ $t('settings.engine_env_restart_hint') }}</span>
        <a-button
          type="primary"
          :loading="restarting"
          @click="restartBackend"
        >
          {{ restarting ? $t('settings.engine_env_restarting') : $t('settings.engine_env_restart_now') }}
        </a-button>
      </div>

      <div v-for="spec in specs" :key="spec.env" class="engine-env-row">
        <div class="engine-env-main">
          <div class="engine-env-label">{{ $t(labelKey(spec.env)) }}</div>
          <div class="engine-env-desc">{{ $t(`${labelKey(spec.env)}_desc`) }}</div>
          <div class="engine-env-meta">
            <a-tag :color="isSet(spec) ? 'blue' : 'default'">
              {{
                isSet(spec)
                  ? $t('settings.engine_env_explicitly_set')
                  : $t('settings.engine_env_not_set')
              }}
            </a-tag>
            <span class="engine-env-default">
              {{
                spec.default
                  ? $t('settings.engine_env_default_on')
                  : $t('settings.engine_env_default_off')
              }}
            </span>
            <a-button
              v-if="isSet(spec)"
              type="link"
              size="small"
              class="engine-env-reset"
              @click="resetToDefault(spec)"
            >
              <ReloadOutlined />
            </a-button>
          </div>
          <code class="engine-env-name">{{ spec.env }}</code>
        </div>
        <a-switch
          :checked="valueOf(spec)"
          @change="(checked: unknown) => toggle(spec, Boolean(checked))"
        />
      </div>

      <div class="engine-env-actions">
        <a-button
          type="primary"
          :disabled="!dirty || saving"
          :loading="saving"
          @click="save"
        >
          {{ $t('settings.engine_env_save') }}
        </a-button>
      </div>
    </a-spin>
  </div>
</template>

<style scoped>
.engine-env-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.engine-env-intro {
  margin-bottom: 4px;
}

.engine-env-restart {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 8px;
  background: var(--chiron-surface-muted, rgba(0, 0, 0, 0.03));
  border: 1px solid var(--chiron-border-color, rgba(0, 0, 0, 0.08));
}

.engine-env-restart-text {
  font-size: 13px;
  line-height: 1.5;
  color: var(--chiron-text-secondary, rgba(0, 0, 0, 0.65));
}

.engine-env-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  padding: 14px 0;
  border-bottom: 1px solid var(--chiron-border-color, rgba(0, 0, 0, 0.06));
}

.engine-env-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.engine-env-label {
  font-size: 14px;
  font-weight: 500;
}

.engine-env-desc {
  font-size: 13px;
  line-height: 1.55;
  color: var(--chiron-text-secondary, rgba(0, 0, 0, 0.65));
}

.engine-env-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 2px;
}

.engine-env-default {
  font-size: 12px;
  color: var(--chiron-text-tertiary, rgba(0, 0, 0, 0.45));
}

.engine-env-reset {
  padding: 0 4px;
  height: auto;
}

.engine-env-name {
  font-size: 12px;
  color: var(--chiron-text-tertiary, rgba(0, 0, 0, 0.45));
  user-select: text;
}

.engine-env-actions {
  display: flex;
  justify-content: flex-end;
  padding-top: 4px;
}
</style>
