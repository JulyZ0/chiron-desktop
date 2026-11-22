<script setup lang="ts">
/**
 * 会话框底部的「可用工具」入口。
 *
 * 原先是右侧栏顶部的一整块折叠列表 —— 它把"本轮能用什么能力"放在离输入框很远的
 * 地方，而且与上下文芯片、轨迹抢纵向空间。挪进 composer 后：默认收起，点开即见，
 * 与模型 / 人格 / 语言同处于"这一轮怎么跑"的功能带里。
 */
import { computed, ref } from 'vue'
import { Button, Popover } from 'ant-design-vue'
import { ThunderboltOutlined } from '@ant-design/icons-vue'
import { useI18n } from 'vue-i18n'
import { listTools } from '../../api'
import type { ToolInfo } from '../../utils/toolList'

const { t } = useI18n()

const open = ref(false)
const loading = ref(false)
const error = ref(false)
const tools = ref<ToolInfo[]>([])
let loaded = false

const mcpCount = computed(() => tools.value.filter(tool => tool.source === 'mcp').length)
const summary = computed(() => {
  if (error.value) return t('errors.failed_to_load')
  if (loading.value) return '…'
  if (!tools.value.length) return t('common.none')
  return t('common.n', { n: tools.value.length })
})

async function load() {
  if (loading.value || loaded) return
  loading.value = true
  error.value = false
  try {
    tools.value = await listTools()
    loaded = true
  } catch {
    error.value = true
  } finally {
    loading.value = false
  }
}

function onOpenChange(next: boolean) {
  open.value = next
  if (next) void load()
}
</script>

<template>
  <Popover
    :open="open"
    trigger="click"
    placement="topLeft"
    @open-change="onOpenChange"
  >
    <Button
      type="text"
      class="context-btn tools-btn"
      :title="t('agent.available_tools')"
    >
      <template #icon>
        <ThunderboltOutlined />
      </template>
      <span class="context-label">{{ t('agent.available_tools') }}</span>
      <span class="tools-count">{{ summary }}</span>
    </Button>
    <template #content>
      <div class="tools-pop">
        <div class="tools-pop-head">
          <span>{{ t('agent.available_tools') }}</span>
          <span v-if="mcpCount" class="tools-pop-mcp">{{ mcpCount }} MCP</span>
        </div>
        <div v-if="loading" class="tools-pop-empty">{{ t('common.loading') }}</div>
        <div v-else-if="error" class="tools-pop-empty">{{ t('errors.failed_to_load_tools') }}</div>
        <div v-else-if="!tools.length" class="tools-pop-empty">{{ t('agent.no_available_tools') }}</div>
        <div v-else class="tools-pop-list">
          <div
            v-for="tool in tools"
            :key="tool.name"
            class="tools-pop-row"
            :title="tool.description || tool.name"
          >
            <span class="tools-pop-name">{{ tool.name }}</span>
            <span v-if="tool.source === 'mcp'" class="tools-pop-badge">MCP</span>
          </div>
        </div>
      </div>
    </template>
  </Popover>
</template>

<style scoped>
.tools-btn { min-width: 0; }
.tools-count { font-size: var(--fs-xs); color: var(--text-tertiary); margin-inline-start: 2px; }
.tools-pop { width: 280px; max-height: 320px; display: flex; flex-direction: column; gap: 6px; }
.tools-pop-head {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 12px; font-weight: var(--fw-semibold); color: var(--text-secondary);
}
.tools-pop-mcp { color: var(--primary); }
.tools-pop-empty { padding: 10px 0; text-align: center; font-size: 12px; color: var(--text-tertiary); }
.tools-pop-list { overflow-y: auto; display: flex; flex-direction: column; gap: 2px; }
.tools-pop-row {
  display: flex; align-items: center; gap: 6px;
  padding: 5px 6px; border-radius: 6px; font-size: 12px; color: var(--text-secondary);
}
.tools-pop-row:hover { background: var(--bg-hover); }
.tools-pop-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tools-pop-badge {
  flex: none; padding: 0 6px; border-radius: 999px;
  background: var(--primary-bg); color: var(--primary); font-size: 10px;
}
</style>
