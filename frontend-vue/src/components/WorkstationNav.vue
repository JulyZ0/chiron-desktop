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

/** 可选模型路由：调用方已知模型时直接传入；不传则取 `/v1/models` 的第一个可用模型。
 *  不传模型是曾经的默认路径，会落到服务端写死的兜底模型（套餐里不存在时表现为空响应）。 */
export interface QuickCommandModel {
  model: string
  provider?: string
}

/** 模型目录的第一项：undefined=未加载，null=加载过但没有可用模型 */
let quickModelCache: QuickCommandModel | null | undefined
let quickModelInFlight: Promise<QuickCommandModel | null> | null = null

async function resolveQuickModel(provided?: QuickCommandModel | null): Promise<QuickCommandModel | null> {
  if (provided?.model) return provided
  if (quickModelCache !== undefined) return quickModelCache
  if (!quickModelInFlight) {
    quickModelInFlight = (async () => {
      try {
        const { data } = await api.get('/v1/models')
        const list = Array.isArray(data?.data?.models) ? (data.data.models as Array<Record<string, unknown>>) : []
        const first = list.find(m => typeof m?.name === 'string' && m.name)
        return first
          ? { model: String(first.name), provider: typeof first.provider === 'string' ? first.provider : '' }
          : null
      } catch {
        return null
      } finally {
        quickModelInFlight = null
      }
    })()
  }
  quickModelCache = await quickModelInFlight
  return quickModelCache
}

/**
 * quick-execute 是**同步**完成一次模型编排的，耗时远超普通 CRUD：
 * 全局 30s 超时会让稍长的任务在前端先断，用户只看到"请求失败"。
 */
const QUICK_EXECUTE_TIMEOUT_MS = 120_000

interface QuickTaskStatus {
  status?: unknown
}

interface QuickExecutePayload {
  success?: boolean
  error?: string
  output?: unknown
  metadata?: { subtasks?: QuickTaskStatus[] }
}

/**
 * 一次 quick-execute 是否**真的**成功。
 *
 * 外层 `success` 只表示 HTTP 链路结束了：聚合层在"子任务全部失败"时仍可能返回
 * `success=true` + 一段 `subtasks_failed` 的 JSON（老服务端行为）。只看成功标志
 * 会把失败当回答展示 —— 首页指挥台表现为"跳过去只有一坨 JSON / 像没反应"。
 *
 * 判定口径：有子任务、且一个都没完成、却有失败 ⇒ 失败；有部分成功则保留结果。
 */
function jobFailure(payload: QuickExecutePayload | undefined): string {
  if (!payload) return 'invalid response'
  if (payload.success === false) return payload.error || 'execution failed'

  let output = payload.output
  if (typeof output === 'string') {
    try {
      output = JSON.parse(output)
    } catch {
      /* 非 JSON 输出按原样交给聊天页展示 */
    }
  }
  let firstError = ''
  if (output && typeof output === 'object') {
    const outputs = (output as { outputs?: unknown }).outputs
    if (Array.isArray(outputs)) {
      for (const item of outputs) {
        const err = item && typeof item === 'object' ? (item as { error?: unknown }).error : ''
        if (typeof err === 'string' && err) {
          firstError = err
          break
        }
      }
    }
  }

  const subtasks = Array.isArray(payload.metadata?.subtasks) ? payload.metadata.subtasks : []
  const failed = subtasks.filter(t => t && t.status === 'failed').length
  const completed = subtasks.filter(t => t && t.status === 'completed').length
  if (failed > 0 && completed === 0) return firstError || 'execution failed'
  return ''
}

/** 快速命令统一执行逻辑（WorkstationNav 与停靠坞弹层共用）：
 *  创建 uni 会话 → 调用 /v1/quick-execute → 跳转聊天页展示结果，可继续追问 */
export async function executeQuickCommand(
  command: string,
  model?: QuickCommandModel | null,
): Promise<QuickCommandRunResult> {
  const sessionId = `uni_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const title = command.substring(0, 50)
  const resolved = await resolveQuickModel(model)
  const llmConfig = resolved?.model
    ? { model: resolved.model, ...(resolved.provider ? { provider: resolved.provider } : {}) }
    : undefined
  let response
  try {
    response = await api.post(
      '/v1/quick-execute',
      {
        user_input: command,
        mode: 'auto',
        session_id: sessionId,
        ...(llmConfig ? { llm_config: llmConfig } : {}),
      },
      { timeout: QUICK_EXECUTE_TIMEOUT_MS },
    )
  } catch (error) {
    await router.push({ path: '/chat', query: { task: '', error: 'request failed' } })
    throw error instanceof Error ? error : new Error('request failed')
  }
  // 网关统一包 `{success:true,data:{...}}`：外层 success 恒为 true，
  // 引擎自己的失败在 data 里 —— 只读外层会把失败当成功（见 jobFailure）。
  const payload = response.data?.data as QuickExecutePayload | undefined
  const failure = jobFailure(payload)
  if (failure) {
    await router.push({ path: '/chat', query: { task: sessionId, error: failure } })
    throw new Error(failure)
  }
  await router.push({ path: '/chat', query: { task: sessionId } })
  return { sessionId, title }
}
</script>

<script setup lang="ts">
// 空的 setup 块，确保组件有默认导出
</script>


