import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { Select, message } from 'ant-design-vue'
import ToolPicker from '../ToolPicker.vue'

const api = vi.hoisted(() => ({ listTools: vi.fn() }))
vi.mock('../../../api', () => api)

const SCHEMA = { type: 'object', properties: { path: { type: 'string' } } }

function mockTools() {
  api.listTools.mockResolvedValue([
    { name: 'read_file', description: '读文件', parameters: SCHEMA },
    { name: 'mcp__fs__read', description: '来自插件的工具', parameters: SCHEMA },
    { name: 'no_schema_tool', description: '没有参数定义' },
  ])
}

/** 触发多选的 update:value（antd 的上行事件） */
async function pick(wrapper: ReturnType<typeof mount>, names: string[]) {
  wrapper.findComponent(Select).vm.$emit('update:value', names)
  await flushPromises()
}

function lastEmitted(wrapper: ReturnType<typeof mount>): any[] {
  const events = wrapper.emitted('update:modelValue')
  expect(events).toBeTruthy()
  return JSON.parse(String(events![events!.length - 1]![0]))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ToolPicker（从引擎可用工具中挑选）', () => {
  it('勾选工具写入完整定义（含 parameters schema）', async () => {
    mockTools()
    const wrapper = mount(ToolPicker, { props: { modelValue: '[]' } })
    await flushPromises()

    await pick(wrapper, ['read_file'])

    expect(lastEmitted(wrapper)).toEqual([
      { name: 'read_file', description: '读文件', parameters: SCHEMA },
    ])
  })

  it('插件（MCP）工具与内置工具一视同仁', async () => {
    mockTools()
    const wrapper = mount(ToolPicker, { props: { modelValue: '[]' } })
    await flushPromises()

    await pick(wrapper, ['mcp__fs__read'])

    expect(lastEmitted(wrapper)[0].name).toBe('mcp__fs__read')
  })

  it('缺少 parameters 的工具不写出空 schema（避免生成无效配置）', async () => {
    mockTools()
    const wrapper = mount(ToolPicker, { props: { modelValue: '[]' } })
    await flushPromises()

    await pick(wrapper, ['no_schema_tool'])

    expect(lastEmitted(wrapper)).toEqual([{ name: 'no_schema_tool', description: '没有参数定义' }])
  })

  it('保留手写但不在引擎列表里的条目（选择器只是补充）', async () => {
    mockTools()
    const handwritten = [{ name: 'my_custom_tool', description: '自己写的' }]
    const wrapper = mount(ToolPicker, { props: { modelValue: JSON.stringify(handwritten) } })
    await flushPromises()

    await pick(wrapper, ['read_file'])

    const result = lastEmitted(wrapper)
    expect(result.map((t: any) => t.name)).toEqual(['my_custom_tool', 'read_file'])
  })

  it('引擎里已有的条目按最新 schema 重写（而不是与手写旧值重复）', async () => {
    mockTools()
    const stale = [{ name: 'read_file', description: '旧描述', parameters: { type: 'object' } }]
    const wrapper = mount(ToolPicker, { props: { modelValue: JSON.stringify(stale) } })
    await flushPromises()

    await pick(wrapper, ['read_file'])

    const result = lastEmitted(wrapper)
    expect(result).toHaveLength(1)
    expect(result[0]).toEqual({ name: 'read_file', description: '读文件', parameters: SCHEMA })
  })

  it('现有配置不是合法 JSON 时给出提示并按本次选择重写', async () => {
    mockTools()
    const warn = vi.spyOn(message, 'warning').mockImplementation(() => undefined as any)
    const wrapper = mount(ToolPicker, { props: { modelValue: '{ 坏掉的 json' } })
    await flushPromises()

    await pick(wrapper, ['read_file'])

    expect(warn).toHaveBeenCalled()
    expect(lastEmitted(wrapper)).toHaveLength(1)
    warn.mockRestore()
  })

  it('已配置的工具回填成勾选态（打开时能看到当前配置）', async () => {
    mockTools()
    const wrapper = mount(ToolPicker, { props: { modelValue: JSON.stringify([{ name: 'read_file' }]) } })
    await flushPromises()

    expect(wrapper.findComponent(Select).props('value')).toEqual(['read_file'])
  })

  it('工具列表接口失败时降级为空列表且不抛错', async () => {
    api.listTools.mockRejectedValue(new Error('engine down'))
    const wrapper = mount(ToolPicker, { props: { modelValue: '[]' } })
    await flushPromises()

    expect(wrapper.findComponent(Select).props('options')).toEqual([])
    expect(wrapper.text()).toContain('安装插件后')
  })
})
