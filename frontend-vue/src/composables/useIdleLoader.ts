/**
 * 空闲加载 Composable
 * 
 * 使用 RequestIdleCallback 在浏览器空闲时执行非关键任务
 * 避免阻塞主线程，提升页面响应性
 * 
 * 使用示例:
 * ```ts
 * const { ready, executeWhenIdle } = useIdleLoader()
 * 
 * // 在空闲时预加载数据
 * executeWhenIdle(async () => {
 *   await fetchNonCriticalData()
 *   ready.value = true
 * })
 * ```
 */

import { ref } from 'vue'

interface UseIdleLoaderOptions {
  timeout?: number  // 最大等待时间(ms)
  priority?: 'high' | 'low'  // 任务优先级
}

export function useIdleLoader(options: UseIdleLoaderOptions = {}) {
  const {
    timeout = 2000,
    priority = 'high',
  } = options

  const ready = ref(false)
  const loading = ref(false)
  const error = ref<Error | null>(null)

  /**
   * 在浏览器空闲时执行任务
   */
  const executeWhenIdle = (task: () => Promise<void>) => {
    if (loading.value) return
    
    loading.value = true
    error.value = null

    const runTask = async (deadline?: IdleDeadline) => {
      try {
        // 检查是否还有空闲时间
        if (deadline && !deadline.didTimeout) {
          // 在空闲时间片内执行
          await task()
        } else {
          // 超时后执行
          await task()
        }
        ready.value = true
      } catch (e) {
        error.value = e as Error
        console.error('[useIdleLoader] Task failed:', e)
      } finally {
        loading.value = false
      }
    }

    // 使用 requestIdleCallback (如果支持)
    if ('requestIdleCallback' in window) {
      requestIdleCallback(deadline => runTask(deadline), { 
        timeout: priority === 'high' ? timeout : timeout * 2 
      })
    } else {
      // 降级方案：使用 setTimeout
      setTimeout(() => runTask(), priority === 'high' ? 100 : 500)
    }
  }

  /**
   * 批量执行任务（按优先级排序）
   */
  const executeMultiple = (tasks: Array<() => Promise<void>>) => {
    tasks.forEach((task, index) => {
      setTimeout(() => executeWhenIdle(task), index * 100)
    })
  }

  /**
   * 重置状态
   */
  const reset = () => {
    ready.value = false
    loading.value = false
    error.value = null
  }

  return {
    ready,
    loading,
    error,
    executeWhenIdle,
    executeMultiple,
    reset,
  }
}