<script setup lang="ts">
/**
 * 后台「服务」页：这台机器对谁可见。
 *
 * 两件事在本页合流，但**生效时机相同，理由不同**，页面必须讲清楚：
 *
 *   1. 监听地址（`bind_host`）—— 只绑 `127.0.0.1` 时局域网里的手机连 TCP 都连不上。
 *      它绑在套接字上，进程内换不掉。
 *   2. 对外地址（`public_base_url`）—— 决定 Cookie 的 Secure 推导（https ⇒ Secure），
 *      也就是"局域网里能不能保持登录"。它在启动时读入，改完同样要重启才一致。
 *
 * 两者都不是保存即生效，所以保存后必须明确说「需要重启」，而不是让页面看起来
 * 生效了、实际仍是旧值。
 *
 * 端口只读：它和反向代理、桌面壳绑在一起，改错一次整站打不开，而这一页并不掌握
 * 那些信息。
 */
import { computed, onMounted, ref } from 'vue'
import { Alert, Button, Card, Empty, Input, Radio, Spin, Tag, message } from 'ant-design-vue'
import { CopyOutlined, ReloadOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { getServiceInfo, saveServiceInfo, type AdminServiceInfo } from '@/api/admin'
import { serverErrorMessage } from '../../utils/apiError'
import { copyTextOrThrow } from '../../utils/clipboard'

const { t } = useI18n()

const loading = ref(false)
const saving = ref(false)
const info = ref<AdminServiceInfo | null>(null)

/** 表单草稿：与「当前生效值」分开，才能显示「改了但还没保存」。 */
const draft = ref({ bind_host: '', public_base_url: '' })

/** 后端归一后的全网卡取值（见 internal/api/admin_service.go）。 */
const BIND_ALL = '0.0.0.0'
const BIND_LOOPBACK = '127.0.0.1'

/**
 * 访问范围的两个语义档位。
 *
 * 不用"自定义"第三个档位：表单下方的监听地址输入框本身就能填任意 IP，
 * 而多一个档位只会让人问"自定义和局域网有什么区别"。填了别的 IP 时
 * 两个 radio 都不选中 —— 这比硬归到某一档诚实。
 */
const scope = computed<'loopback' | 'lan' | null>(() => {
  const h = draft.value.bind_host.trim()
  if (h === BIND_LOOPBACK) return 'loopback'
  if (h === '' || h === BIND_ALL) return 'lan'
  return null
})

const dirty = computed(() => {
  if (!info.value) return false
  return draft.value.bind_host.trim() !== info.value.saved_bind_host
    || draft.value.public_base_url.trim() !== info.value.saved_public_base_url
})

/** 保存后仍未生效：已保存值与进程实际持有值不一致。 */
const needsRestart = computed(() => info.value?.restart_required === true)

function pickScope(next: 'loopback' | 'lan') {
  draft.value.bind_host = next === 'loopback' ? BIND_LOOPBACK : BIND_ALL
}

function adopt(next: AdminServiceInfo) {
  info.value = next
  draft.value = {
    bind_host: next.saved_bind_host,
    public_base_url: next.saved_public_base_url,
  }
}

async function load() {
  loading.value = true
  try {
    adopt(await getServiceInfo())
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.service.load_failed')))
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    // 只提交用户真正改动过的字段：后端把"字段缺席"与"显式清空"区别对待，
    // 全量提交会把另一侧未改的值也当成一次写入。
    const payload: { bind_host?: string; public_base_url?: string } = {}
    if (!info.value || draft.value.bind_host.trim() !== info.value.saved_bind_host) {
      payload.bind_host = draft.value.bind_host.trim()
    }
    if (!info.value || draft.value.public_base_url.trim() !== info.value.saved_public_base_url) {
      payload.public_base_url = draft.value.public_base_url.trim()
    }
    // 没有改动就不发请求：按钮此时是禁用的，这里只是兜住回车/程序化调用。
    if (Object.keys(payload).length === 0) return
    const next = await saveServiceInfo(payload)
    adopt(next)
    // 说什么取决于"能不能立刻用"：监听地址要重启才算数，含糊其辞正是这类页面
    // 最坑人的地方。
    if (next.restart_required) message.warning(t('admin.service.saved_need_restart'))
    else message.success(t('admin.service.saved'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.service.save_failed')))
  } finally {
    saving.value = false
  }
}

async function copyAddress(addr: string) {
  try {
    await copyTextOrThrow(addr)
    message.success(t('admin.service.address_copied'))
  } catch {
    message.warning(t('errors.copy_failed_please_copy_manually'))
  }
}

onMounted(load)
</script>

<template>
  <div class="service-view">
    <div class="page-header">
      <div>
        <h2 class="page-title page-title-sm">
          {{ $t('admin.service.page_title') }}
        </h2>
        <p class="page-desc">
          {{ $t('admin.service.page_desc') }}
        </p>
      </div>
      <div class="header-actions">
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('admin.service.refresh') }}
        </Button>
        <Button
          type="primary"
          data-test="service-save"
          :loading="saving"
          :disabled="!dirty"
          @click="save"
        >
          {{ $t('common.save') }}
        </Button>
      </div>
    </div>

    <Spin :spinning="loading">
      <Alert
        v-if="info && info.warning"
        class="service-alert"
        type="warning"
        show-icon
        :message="info.warning"
      />

      <!-- 已保存 ≠ 已生效。这条提示必须在页面主位，而不是藏在某个 tooltip 里。 -->
      <Alert
        v-if="needsRestart"
        class="service-alert"
        data-test="service-restart"
        type="info"
        show-icon
        :message="$t('admin.service.restart_required')"
      />

      <Card
        :bordered="false"
        class="service-card"
      >
        <section class="service-section">
          <h3 class="section-title">
            {{ $t('admin.service.scope_title') }}
          </h3>

          <Radio.Group
            :value="scope ?? undefined"
            class="scope-group"
            @change="(e: any) => pickScope(e.target.value)"
          >
            <Radio
              value="loopback"
              class="scope-option"
              :class="{ 'scope-option--active': scope === 'loopback' }"
              data-test="scope-loopback"
            >
              <span class="scope-text">
                <span class="scope-label">{{ $t('admin.service.scope_loopback') }}</span>
                <span class="scope-desc">{{ $t('admin.service.scope_loopback_desc') }}</span>
              </span>
            </Radio>
            <Radio
              value="lan"
              class="scope-option"
              :class="{ 'scope-option--active': scope === 'lan' }"
              data-test="scope-lan"
            >
              <span class="scope-text">
                <span class="scope-label">{{ $t('admin.service.scope_lan') }}</span>
                <span class="scope-desc">{{ $t('admin.service.scope_lan_desc') }}</span>
              </span>
            </Radio>
          </Radio.Group>

          <div class="field">
            <label class="field-label">{{ $t('admin.service.bind_host') }}</label>
            <Input
              v-model:value="draft.bind_host"
              data-test="service-bind-host"
              class="field-input"
              :placeholder="BIND_ALL"
            />
            <p class="field-hint">
              {{ $t('admin.service.bind_host_hint') }}
            </p>
          </div>

          <div class="field">
            <label class="field-label">{{ $t('admin.service.port') }}</label>
            <div class="field-static">
              <Tag>{{ info?.port || '—' }}</Tag>
              <span class="field-hint field-hint--inline">{{ $t('admin.service.port_hint') }}</span>
            </div>
          </div>

          <div class="field">
            <label class="field-label">{{ $t('admin.service.listen_addr') }}</label>
            <code class="mono">{{ info?.listen_addr || '—' }}</code>
          </div>
        </section>

        <section class="service-section">
          <h3 class="section-title">
            {{ $t('admin.service.public_url_title') }}
          </h3>

          <div class="field">
            <label class="field-label">{{ $t('admin.service.public_base_url') }}</label>
            <Input
              v-model:value="draft.public_base_url"
              data-test="service-public-url"
              class="field-input"
              placeholder="https://chat.example.com"
            />
            <p class="field-hint">
              {{ $t('admin.service.public_base_url_hint') }}
            </p>
          </div>

          <p class="field-hint">
            {{
              $t('admin.service.cookies_secure', {
                state: info?.cookie_secure
                  ? $t('admin.service.cookie_secure_on')
                  : $t('admin.service.cookie_secure_off'),
              })
            }}
          </p>
        </section>

        <section class="service-section">
          <h3 class="section-title">
            {{ $t('admin.service.local_addresses') }}
          </h3>
          <div
            v-if="info?.local_addresses?.length"
            class="addr-list"
          >
            <div
              v-for="addr in info.local_addresses"
              :key="addr"
              class="addr-row"
            >
              <code class="mono">{{ addr }}:{{ info.port }}</code>
              <Button
                type="link"
                size="small"
                @click="copyAddress(`${addr}:${info.port}`)"
              >
                <template #icon>
                  <CopyOutlined />
                </template>
                {{ $t('admin.service.copy_address') }}
              </Button>
            </div>
          </div>
          <Empty
            v-else
            :image="Empty.PRESENTED_IMAGE_SIMPLE"
            :description="$t('admin.service.local_addresses_empty')"
          />
        </section>
      </Card>
    </Spin>
  </div>
</template>

<style scoped>
.service-view { width: 100%; }
.header-actions { display: flex; gap: 8px; }
.service-alert { margin-bottom: 12px; }
.service-card { border-radius: var(--radius-lg); }
.service-section + .service-section {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--border-color, rgba(0, 0, 0, 0.06));
}
.section-title {
  margin: 0 0 12px;
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.scope-group { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
.scope-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--border-color, rgba(0, 0, 0, 0.06));
  border-radius: var(--radius-md, 8px);
  cursor: pointer;
}
.scope-option--active { border-color: var(--color-primary, #1677ff); }
.scope-text { display: flex; flex-direction: column; gap: 2px; }
.scope-label { color: var(--text-primary); font-weight: var(--fw-medium, 500); }
.scope-desc { color: var(--text-secondary); font-size: var(--fs-sm); }
.field { margin-bottom: 16px; }
.field:last-child { margin-bottom: 0; }
.field-label {
  display: block;
  margin-bottom: 6px;
  color: var(--text-secondary);
  font-size: var(--fs-sm);
}
.field-input { max-width: 420px; }
.field-hint { margin: 6px 0 0; color: var(--text-tertiary); font-size: var(--fs-xs); }
.field-hint--inline { margin: 0; }
.field-static { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.mono { font-family: var(--font-mono, monospace); color: var(--text-primary); }
.addr-list { display: flex; flex-direction: column; gap: 4px; }
.addr-row { display: flex; align-items: center; gap: 8px; }
</style>
