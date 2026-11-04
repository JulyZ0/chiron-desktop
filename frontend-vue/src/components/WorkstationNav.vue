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

/** 快速命令统一执行逻辑（WorkstationNav 与停靠坞弹层共用）：
 *  创建 uni 会话 → 调用 /v1/quick-execute → 跳转聊天页展示结果，可继续追问 */
export async function executeQuickCommand(command: string): Promise<QuickCommandRunResult> {
  const sessionId = `uni_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const title = command.substring(0, 50)
  try {
    const response = await api.post('/v1/quick-execute', {
      user_input: command,
      mode: 'auto',
      session_id: sessionId,
    })
    if (response.data?.success) {
      await router.push({ path: '/chat', query: { task: sessionId } })
      return { sessionId, title }
    }
    await router.push({ path: '/chat', query: { task: sessionId, error: response.data?.error || 'execution failed' } })
    return { sessionId, title }
  } catch {
    await router.push({ path: '/chat', query: { task: '', error: 'request failed' } })
    throw new Error('request failed')
  }
}
</script>

<script setup lang="ts">
// 空的 setup 块，确保组件有默认导出
</script>


