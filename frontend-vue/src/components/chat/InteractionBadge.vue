<script setup lang="ts">
/**
 * 待办角标（M21）：跨会话回答"我还欠哪些答复"。
 *
 * 与 `InteractionInbox.vue` 的分工：收件箱在**打开那个会话**时列出该会话的票（可答复），
 * 角标在**任何页面**给出未读数 —— 点一下跳到欠答复的那条会话。M7 只做到"打开就看得见"，
 * 而"用户不知道哪个会话欠答复"正是它没覆盖的那一半。
 *
 * 数据源两条，缺一不可：
 *   - **用户级 SSE**（`createUserEventsConnection`）：落票瞬间刷新（用户可能正看着别的会话）；
 *   - **低频轮询**：SSE 断开（浏览器休眠、网关重启、代理掐长连接）时的自愈路径 ——
 *     提醒不该依赖某一条连接活着。
 *
 * 两条都失败时**保持上一次读数**、不弹错：角标是增强，不是页面能否使用的条件。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Badge } from 'ant-design-vue'
import {
  createUserEventsConnection,
  listMyPendingInteractions,
  type PendingInteraction,
} from '../../api/interactions'
import type { SSEHandle } from '../../api'
import { INTERACTIONS_CHANGED_EVENT, pendingUnionCount } from './pendingReconcile'

const props = defineProps<{
  pollMs?: number
  /**
   * 本轮实时票据的 id（H61 对账）。
   *
   * 角标的数据源是**服务端的跨会话列表**，而实时票据是"这一轮真的在等"的现场 ——
   * 两者都缺一半：列表可能还没落库完，实时那份只覆盖当前会话。读数取**并集**（按票去重），
   * 于是"角标数着 2、界面上只有 1 张能点"这类不一致不再出现。
   */
  liveIds?: string[]
}>()
const emit = defineEmits<{ (e: 'select', sessionId: string): void }>()

const items = ref<PendingInteraction[]>([])
const source = ref<SSEHandle | null>(null)
let timer: ReturnType<typeof setInterval> | null = null

/** 角标读数：历史 ∪ 实时，同一张票只算一次 */
const count = computed(() => pendingUnionCount(
  items.value,
  (props.liveIds || []).map((id) => ({ id, kind: 'approval' as const })),
))

async function refresh() {
  try {
    items.value = await listMyPendingInteractions()
  } catch {
    // 保持上一次读数：拉不到不等于"没有待办"。
  }
}

function openFirst() {
  const sessionId = items.value[0]?.session_id
  if (sessionId) emit('select', sessionId)
}

function onInteractionsChanged() {
  void refresh()
}

onMounted(() => {
  void refresh()
  timer = setInterval(() => void refresh(), props.pollMs ?? 30_000)
  // 任何一处把票答完（收件箱 / 实时卡片 / 子 Agent 面板）都会广播一次：
  // 不接这一条，角标就只能等下一轮 30s 轮询才变 —— 期间它一直数着已经答完的那张。
  window.addEventListener(INTERACTIONS_CHANGED_EVENT, onInteractionsChanged)
  try {
    source.value = createUserEventsConnection(() => void refresh())
  } catch {
    // 连接建不起来：轮询仍然兜住（角标是增强，不是页面能否使用的条件）。
    source.value = null
  }
})

onBeforeUnmount(() => {
  if (timer) clearInterval(timer)
  timer = null
  window.removeEventListener(INTERACTIONS_CHANGED_EVENT, onInteractionsChanged)
  source.value?.close()
  source.value = null
})
</script>

<template>
  <button
    v-if="count > 0"
    type="button"
    class="interaction-badge"
    :title="$t('agent.pending_interactions_title')"
    @click="openFirst"
  >
    <Badge :count="count" :overflow-count="99" />
  </button>
</template>

<style scoped>
.interaction-badge {
  display: inline-flex;
  align-items: center;
  border: none;
  background: transparent;
  cursor: pointer;
  padding: 0 4px;
  transition: color var(--dur-fast) ease, background-color var(--dur-fast) ease, border-color var(--dur-fast) ease;
}
.interaction-badge:hover { background: var(--bg-hover); }
</style>
