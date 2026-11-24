<script setup lang="ts">
/**
 * 后台「知识库」页：知识库索引服务用**本地模式**还是**外部 pageindex 服务**。
 *
 * 这一页最容易骗人的两处，本页必须说清楚：
 *
 *   1. **保存成功 ≠ 引擎已采纳**。配置写进 DB 只是第一步，引擎靠 Redis 广播
 *      （运行期保存）或下次启动的 `hydrate()` 才会真正换地址。所以页面同时显示
 *      「已保存」与「引擎生效中」两组值，并标出两者分叉 —— 只显示自己保存的值，
 *      用户会对着一个引擎从未采纳的地址排查半天。
 *   2. **本地模式不接受自定义地址**。它永远还原到进程启动时注入的那个地址
 *      （见 `runtime.bootstrap_url()`）：那是引擎唯一能"回到本机内置服务"的依据。
 *      因此选了本地模式就把地址输入框置灰，而不是允许填一个被静默忽略的值。
 *
 * 连通性一律交给**引擎**探（后端转发到 `/v1/internal/knowledge-runtime`）：
 * 网关和引擎的网络位置不同，网关自己探出来的结论对用户没有意义。
 */
import { computed, onMounted, ref } from 'vue'
import { Alert, Button, Card, Input, Radio, Spin, Tag, message } from 'ant-design-vue'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import {
  getKnowledgeInfo,
  saveKnowledgeInfo,
  testKnowledgeService,
  type AdminKnowledgeInfo,
} from '@/api/admin'
import AdminPage from '@/components/admin/AdminPage.vue'
import { serverErrorMessage } from '../../utils/apiError'

const { t } = useI18n()

type Mode = 'local' | 'remote'

const loading = ref(false)
const saving = ref(false)
const testing = ref(false)
const info = ref<AdminKnowledgeInfo | null>(null)

/** 表单草稿：与「已保存值」分开，才能显示"改了但还没保存"。 */
const draft = ref<{ mode: Mode; service_url: string }>({ mode: 'local', service_url: '' })

/** 测试结果：`null` = 还没测过（与"测了但失败"必须可区分）。 */
const testResult = ref<{ ok: boolean; detail: string; engine_available: boolean } | null>(null)

const savedMode = computed<Mode>(() =>
  info.value?.saved?.mode === 'remote' ? 'remote' : 'local',
)
const savedURL = computed(() => String(info.value?.saved?.service_url || ''))

/** 引擎生效值：`null` 表示读不到（引擎没起 / 未就绪），不代表配置错。 */
const runtime = computed(() => info.value?.runtime ?? null)

const dirty = computed(() => {
  if (!info.value) return false
  // 本地模式下地址不参与比较：那个值本来就会被忽略（见文件头第 2 条）。
  const urlChanged = draft.value.mode === 'remote'
    && draft.value.service_url.trim() !== savedURL.value
  return draft.value.mode !== savedMode.value || urlChanged
})

/** 已保存 ≠ 引擎在用：后端算好分叉，这里只负责呈现。 */
const diverged = computed(() => info.value?.pending_restart === true)

function adopt(next: AdminKnowledgeInfo) {
  info.value = next
  const mode: Mode = next.saved?.mode === 'remote' ? 'remote' : 'local'
  draft.value = { mode, service_url: String(next.saved?.service_url || '') }
  // 换过一次数据后，旧的探测结论就不再对应当前输入 —— 留着会误导。
  testResult.value = null
}

async function load() {
  loading.value = true
  try {
    adopt(await getKnowledgeInfo())
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.knowledge.load_failed')))
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const payload: { mode?: Mode; service_url?: string } = {}
    if (draft.value.mode !== savedMode.value) payload.mode = draft.value.mode
    // 地址只在远程模式下有意义；本地模式下提交它等于让后端存一个永远被忽略的值。
    if (draft.value.mode === 'remote' && draft.value.service_url.trim() !== savedURL.value) {
      payload.service_url = draft.value.service_url.trim()
    }
    if (Object.keys(payload).length === 0) return

    const next = await saveKnowledgeInfo(payload)
    adopt(next)
    // 说什么取决于"引擎采纳了没"：广播正常时几秒内就会一致，此时说"需重启"是错的。
    if (next.pending_restart) message.warning(t('admin.knowledge.saved_pending'))
    else message.success(t('admin.knowledge.saved'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.knowledge.save_failed')))
  } finally {
    saving.value = false
  }
}

async function test() {
  testing.value = true
  try {
    // 远程模式测草稿里的地址（保存前就能验）；本地模式测当前生效的地址。
    const target = draft.value.mode === 'remote' ? draft.value.service_url.trim() : ''
    const res = await testKnowledgeService(target)
    testResult.value = {
      ok: res.ok,
      detail: res.detail || '',
      engine_available: res.engine_available,
    }
    if (res.ok) message.success(t('admin.knowledge.test_ok'))
    else if (!res.engine_available) message.error(t('admin.knowledge.engine_unavailable'))
    else message.warning(t('admin.knowledge.test_failed'))
  } catch (err) {
    message.error(serverErrorMessage(err, t('admin.knowledge.test_failed')))
  } finally {
    testing.value = false
  }
}

onMounted(load)
</script>

<template>
  <AdminPage
    :title="$t('admin.knowledge.page_title')"
    :description="$t('admin.knowledge.page_desc')"
  >
    <template #actions>
      <div class="header-actions">
        <Button
          :loading="loading"
          @click="load"
        >
          <template #icon>
            <ReloadOutlined />
          </template>
          {{ $t('admin.knowledge.refresh') }}
        </Button>
        <Button
          data-test="knowledge-test"
          :loading="testing"
          @click="test"
        >
          <template #icon>
            <ThunderboltOutlined />
          </template>
          {{ $t('admin.knowledge.test_connection') }}
        </Button>
        <Button
          type="primary"
          data-test="knowledge-save"
          :loading="saving"
          :disabled="!dirty"
          @click="save"
        >
          {{ $t('common.save') }}
        </Button>
      </div>
    </template>

    <Spin :spinning="loading">
      <!-- 分叉提示必须在主位：保存了但引擎还在用旧地址，是这一页最需要被看见的状态。 -->
      <Alert
        v-if="diverged"
        class="knowledge-alert"
        data-test="knowledge-diverged"
        type="warning"
        show-icon
        :message="$t('admin.knowledge.diverged')"
      />

      <Alert
        v-if="info && !info.engine_available"
        class="knowledge-alert"
        data-test="knowledge-engine-unavailable"
        type="info"
        show-icon
        :message="$t('admin.knowledge.engine_unavailable_hint')"
      />

      <Alert
        v-if="testResult"
        class="knowledge-alert"
        data-test="knowledge-test-result"
        :type="testResult.ok ? 'success' : 'error'"
        show-icon
        :message="testResult.ok
          ? $t('admin.knowledge.probe_ok')
          : $t('admin.knowledge.probe_failed')"
        :description="testResult.detail || undefined"
      />

      <Card
        :bordered="false"
        class="knowledge-card"
      >
        <section class="knowledge-section">
          <h3 class="section-title">
            {{ $t('admin.knowledge.mode_title') }}
          </h3>

          <Radio.Group
            v-model:value="draft.mode"
            class="mode-group"
          >
            <Radio
              value="local"
              class="mode-option"
              :class="{ 'mode-option--active': draft.mode === 'local' }"
              data-test="mode-local"
            >
              <span class="mode-text">
                <span class="mode-label">{{ $t('admin.knowledge.mode_local') }}</span>
                <span class="mode-desc">{{ $t('admin.knowledge.mode_local_desc') }}</span>
              </span>
            </Radio>
            <Radio
              value="remote"
              class="mode-option"
              :class="{ 'mode-option--active': draft.mode === 'remote' }"
              data-test="mode-remote"
            >
              <span class="mode-text">
                <span class="mode-label">{{ $t('admin.knowledge.mode_remote') }}</span>
                <span class="mode-desc">{{ $t('admin.knowledge.mode_remote_desc') }}</span>
              </span>
            </Radio>
          </Radio.Group>

          <div class="field">
            <label class="field-label">{{ $t('admin.knowledge.service_url') }}</label>
            <Input
              v-model:value="draft.service_url"
              data-test="knowledge-service-url"
              class="field-input"
              :disabled="draft.mode === 'local'"
              placeholder="http://127.0.0.1:8100"
            />
            <p class="field-hint">
              {{ draft.mode === 'local'
                ? $t('admin.knowledge.service_url_local_hint')
                : $t('admin.knowledge.service_url_hint') }}
            </p>
          </div>
        </section>

        <section class="knowledge-section">
          <h3 class="section-title">
            {{ $t('admin.knowledge.runtime_title') }}
          </h3>

          <template v-if="runtime">
            <div class="field">
              <label class="field-label">{{ $t('admin.knowledge.runtime_mode') }}</label>
              <Tag data-test="runtime-mode">
                {{ runtime.mode === 'remote'
                  ? $t('admin.knowledge.mode_remote')
                  : $t('admin.knowledge.mode_local') }}
              </Tag>
            </div>
            <div class="field">
              <label class="field-label">{{ $t('admin.knowledge.runtime_url') }}</label>
              <code
                class="mono"
                data-test="runtime-url"
              >{{ runtime.service_url || '—' }}</code>
            </div>
            <div class="field">
              <label class="field-label">{{ $t('admin.knowledge.bootstrap_url') }}</label>
              <code class="mono">{{ runtime.bootstrap_url || '—' }}</code>
              <p class="field-hint">
                {{ $t('admin.knowledge.bootstrap_url_hint') }}
              </p>
            </div>
          </template>
          <p
            v-else
            class="field-hint"
          >
            {{ $t('admin.knowledge.runtime_unavailable') }}
          </p>
        </section>

        <section class="knowledge-section">
          <h3 class="section-title">
            {{ $t('admin.knowledge.saved_title') }}
          </h3>
          <div class="field">
            <label class="field-label">{{ $t('admin.knowledge.saved_mode') }}</label>
            <Tag>
              {{ savedMode === 'remote'
                ? $t('admin.knowledge.mode_remote')
                : $t('admin.knowledge.mode_local') }}
            </Tag>
          </div>
          <div
            v-if="savedMode === 'remote'"
            class="field"
          >
            <label class="field-label">{{ $t('admin.knowledge.saved_url') }}</label>
            <code class="mono">{{ savedURL || '—' }}</code>
          </div>
        </section>
      </Card>
    </Spin>
  </AdminPage>
</template>

<style scoped>
.knowledge-config-view { width: 100%; }
.header-actions { display: flex; gap: 8px; }
.knowledge-alert { margin-bottom: 12px; }
.knowledge-card { border-radius: var(--radius-lg); }
.knowledge-section + .knowledge-section {
  margin-top: 24px;
  padding-top: 20px;
  border-top: var(--hairline-width) solid var(--border-card);
}
.section-title {
  margin: 0 0 12px;
  font-size: var(--fs-md);
  font-weight: var(--fw-semibold);
  color: var(--text-primary);
}
.mode-group { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
.mode-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 12px 14px;
  border: var(--hairline-width) solid var(--border-card);
  border-radius: var(--radius-md);
  cursor: pointer;
}
.mode-option--active { border-color: var(--primary); }
.mode-text { display: flex; flex-direction: column; gap: 2px; }
.mode-label { color: var(--text-primary); font-weight: var(--fw-medium, 500); }
.mode-desc { color: var(--text-secondary); font-size: var(--fs-sm); }
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
.mono { font-family: var(--font-mono, monospace); color: var(--text-primary); }
</style>
