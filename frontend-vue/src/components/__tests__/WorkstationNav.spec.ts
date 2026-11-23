import { describe, it, expect, vi, beforeEach } from 'vitest'

/**
 * 首页/停靠坞共用的 quick-execute 入口：两条**改坏了不会报错**的行为。
 *
 * ① 必须带上目录里第一个可用模型。不传模型时服务端会落到写死的兜底模型
 *    （deepseek-chat）；套餐里没有它就表现为"空响应"，用户看到的是"点了执行没结果"。
 * ② 子任务全失败时聚合层仍可能回 success=true + subtasks_failed 的 JSON。
 *    前端必须把它当失败（提示 + 带 error 跳转），不能当正常回答展示。
 */

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))
const routerMock = vi.hoisted(() => ({
  push: vi.fn(async (): Promise<void> => {}),
}))

vi.mock('../../api', () => ({ api: apiMock }))
vi.mock('../../router', () => ({ default: routerMock }))

async function importExecutor() {
  vi.resetModules() // 清掉模块级模型缓存，让每条用例从"未加载"开始
  const mod = await import('../WorkstationNav.vue')
  return mod.executeQuickCommand
}

beforeEach(() => {
  vi.clearAllMocks()
  apiMock.get.mockResolvedValue({
    data: { data: { models: [{ name: 'deepseek-v4.1-flash', provider: 'opencode-go' }] } },
  })
})

describe('executeQuickCommand', () => {
  it('带上目录里第一个可用模型，而不是让服务端落到兜底模型', async () => {
    const execute = await importExecutor()
    apiMock.post.mockResolvedValue({
      data: { success: true, data: { success: true, output: 'ok' } },
    })

    await execute('统计上季度营收')

    expect(apiMock.get).toHaveBeenCalledWith('/v1/models')
    expect(apiMock.post).toHaveBeenCalledWith(
      '/v1/quick-execute',
      expect.objectContaining({
        user_input: '统计上季度营收',
        llm_config: { model: 'deepseek-v4.1-flash', provider: 'opencode-go' },
      }),
      expect.anything(),
    )
    expect(routerMock.push).toHaveBeenCalledWith({
      path: '/chat',
      query: { task: expect.stringMatching(/^uni_/) },
    })
  })

  it('子任务全失败：必须抛错并带 error 跳转，不能当成功', async () => {
    const execute = await importExecutor()
    apiMock.post.mockResolvedValue({
      data: {
        success: true,
        data: {
          success: true,
          output: JSON.stringify({
            subtasks_completed: 0,
            subtasks_failed: 1,
            outputs: [{ subtask_id: 'sub_0', error: 'LLM capability returned empty response' }],
          }),
          metadata: { subtasks: [{ status: 'failed' }] },
        },
      },
    })

    await expect(execute('只回复两个字：正常')).rejects.toThrow('empty response')
    expect(routerMock.push).toHaveBeenCalledWith({
      path: '/chat',
      query: { task: expect.stringMatching(/^uni_/), error: expect.stringContaining('empty response') },
    })
  })

  it('部分子任务成功：保留结果，不判失败', async () => {
    const execute = await importExecutor()
    apiMock.post.mockResolvedValue({
      data: {
        success: true,
        data: {
          success: true,
          output: JSON.stringify({ subtasks_completed: 1, subtasks_failed: 1 }),
          metadata: { subtasks: [{ status: 'completed' }, { status: 'failed' }] },
        },
      },
    })

    await expect(execute('并行跑两个任务')).resolves.toMatchObject({ sessionId: expect.stringMatching(/^uni_/) })
  })

  it('模型目录不可用时不阻断执行（交给服务端默认路由）', async () => {
    const execute = await importExecutor()
    apiMock.get.mockRejectedValue(new Error('models unavailable'))
    apiMock.post.mockResolvedValue({
      data: { success: true, data: { success: true, output: 'ok' } },
    })

    await execute('hi')

    const body = apiMock.post.mock.calls[0][1] as Record<string, unknown>
    expect(body.llm_config).toBeUndefined()
  })
})
