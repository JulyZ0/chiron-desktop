<template>
  <div class="workstation-nav" />
</template>

<!-- 普通 script 块：导出 executeQuickCommand 供全局停靠坞（AppLayout）复用同一套
     快速命令执行逻辑：创建 uni 会话 → /v1/quick-execute → 跳转 /chat?task= -->
<script lang="ts">
import { api } from '../api'
import router from '../router'

export interface QuickCommandRunResult {
  sessionId: string
  title: string
}

/**
 * quick-execute 是**同步**完成一次模型编排的，耗时远超普通 CRUD：
 * 全局 30s 超时会让稍长的任务在前端先断，用户只看到"请求失败"。
 */
const QUICK_EXECUTE_TIMEOUT_MS = 120_000

/** 快速命令统一执行逻辑（WorkstationNav 与停靠坞弹层共用）：
 *  创建 uni 会话 → 调用 /v1/quick-execute → 跳转聊天页展示结果，可继续追问 */
export async function executeQuickCommand(command: string): Promise<QuickCommandRunResult> {
  const sessionId = `uni_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const title = command.substring(0, 50)
  let response
  try {
    response = await api.post(
      '/v1/quick-execute',
      {
        user_input: command,
        mode: 'auto',
        session_id: sessionId,
      },
      { timeout: QUICK_EXECUTE_TIMEOUT_MS },
    )
  } catch (error) {
    await router.push({ path: '/chat', query: { task: '', error: 'request failed' } })
    throw error instanceof Error ? error : new Error('request failed')
  }
  // 网关统一包 `{success:true,data:{...}}`：外层 success 恒为 true，
  // 引擎自己的失败在 data.success 里 —— 只读外层会把失败当成功。
  const payload = response.data?.data as { success?: boolean; error?: string } | undefined
  if (payload?.success === false) {
    const reason = payload.error || 'execution failed'
    await router.push({ path: '/chat', query: { task: sessionId, error: reason } })
    throw new Error(reason)
  }
  await router.push({ path: '/chat', query: { task: sessionId } })
  return { sessionId, title }
}
</script>

<script setup lang="ts">
// 空的 setup 块，确保组件有默认导出
</script>


