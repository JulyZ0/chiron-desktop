import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { Input, Modal, Select, message } from 'ant-design-vue'
import SaveToKnowledgeDialog from '../SaveToKnowledgeDialog.vue'

const api = vi.hoisted(() => ({ listKnowledgeBases: vi.fn() }))
vi.mock('../../../api', () => api)

const uploader = vi.hoisted(() => ({ createChunkUpload: vi.fn() }))
vi.mock('../../../utils/uploader', () => uploader)

const BASES = [
  { id: 'kb-1', name: '产品知识库' },
  { id: 'kb-2', name: '运维手册' },
]

function mockBases() {
  api.listKnowledgeBases.mockResolvedValue(BASES)
}

/** createChunkUpload 的返回句柄：组件会挂 onProgress 并 await done */
function mockUpload() {
  const handle = { onProgress: vi.fn(), done: Promise.resolve() }
  uploader.createChunkUpload.mockResolvedValue(handle)
  return handle
}

const wrapper = () => mount(SaveToKnowledgeDialog, { props: { open: false, content: '对话正文' } })

/** 打开弹窗（组件在 open 变 true 时才拉知识库） */
async function open(w: ReturnType<typeof wrapper>, props: Record<string, unknown> = {}) {
  await w.setProps({ open: true, ...props })
  await flushPromises()
  return w
}

async function chooseKb(w: ReturnType<typeof wrapper>, id: string) {
  w.findComponent(Select).vm.$emit('update:value', id)
  await flushPromises()
}

/** antd Modal 的 ok 回调就是组件的 save() */
async function confirm(w: ReturnType<typeof wrapper>) {
  w.findComponent(Modal).vm.$emit('ok')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SaveToKnowledgeDialog（把正文沉淀为知识库文档）', () => {
  it('打开时拉取知识库并填入选项', async () => {
    mockBases()
    const w = await open(wrapper())

    expect(api.listKnowledgeBases).toHaveBeenCalledTimes(1)
    expect(w.findComponent(Select).props('options')).toEqual([
      { value: 'kb-1', label: '产品知识库' },
      { value: 'kb-2', label: '运维手册' },
    ])
  })

  it('默认标题取传入的会话标题并去掉首尾空白', async () => {
    mockBases()
    const w = await open(wrapper(), { defaultTitle: '  周会记录  ' })

    expect(w.findComponent(Input).props('value')).toBe('周会记录')
  })

  it('没选知识库时确定按钮禁用（不会向空 kb 上传）', async () => {
    mockBases()
    const w = await open(wrapper())

    expect(w.findComponent(Modal).props('okButtonProps')).toEqual({ disabled: true })
  })

  it('正文为空时同样禁用（空文档没有沉淀价值）', async () => {
    mockBases()
    const w = await open(wrapper(), { content: '   ' })
    await chooseKb(w, 'kb-1')

    expect(w.findComponent(Modal).props('okButtonProps')).toEqual({ disabled: true })
  })

  it('保存成功：文件内容为正文、目标知识库正确，并关闭弹窗、发出 saved', async () => {
    mockBases()
    mockUpload()
    const w = await open(wrapper())
    await chooseKb(w, 'kb-1')
    await confirm(w)

    expect(uploader.createChunkUpload).toHaveBeenCalledTimes(1)
    const [file, meta] = uploader.createChunkUpload.mock.calls[0]
    expect(meta).toEqual({ purpose: 'kb_doc', parentId: 'kb-1' })
    expect(file.type).toBe('text/markdown')
    expect(await file.text()).toBe('对话正文')
    expect(w.emitted('saved')?.[0]).toEqual(['kb-1'])
    expect(w.emitted('update:open')?.at(-1)).toEqual([false])
  })

  it('文件名里的非法字符被替换（否则部分后端会拒收）', async () => {
    mockBases()
    mockUpload()
    const w = await open(wrapper(), { defaultTitle: 'a/b:c*d?e"f<g>h|i' })
    await chooseKb(w, 'kb-1')
    await confirm(w)

    const [file] = uploader.createChunkUpload.mock.calls[0]
    expect(file.name).toBe('a_b_c_d_e_f_g_h_i.md')
  })

  it('标题超长时截断到 80 字符（含扩展名）', async () => {
    mockBases()
    mockUpload()
    const w = await open(wrapper(), { defaultTitle: 'x'.repeat(200) })
    await chooseKb(w, 'kb-1')
    await confirm(w)

    const [file] = uploader.createChunkUpload.mock.calls[0]
    expect(file.name).toBe(`${'x'.repeat(80)}.md`)
  })

  it('上传失败时报错且不关闭弹窗（用户可重试）', async () => {
    mockBases()
    uploader.createChunkUpload.mockRejectedValue(new Error('磁盘满了'))
    const error = vi.spyOn(message, 'error').mockImplementation(() => undefined as any)
    const w = await open(wrapper())
    await chooseKb(w, 'kb-1')
    await confirm(w)

    expect(error).toHaveBeenCalled()
    expect(w.emitted('saved')).toBeFalsy()
    expect(w.emitted('update:open')).toBeFalsy()
    error.mockRestore()
  })

  it('知识库列表拉取失败时降级为空选项且不抛错', async () => {
    api.listKnowledgeBases.mockRejectedValue(new Error('network down'))
    const w = await open(wrapper())

    expect(w.findComponent(Select).props('options')).toEqual([])
  })

  it('已加载过知识库时不重复请求（反复开关弹窗只拉一次）', async () => {
    mockBases()
    const w = await open(wrapper())
    await w.setProps({ open: false })
    await open(w)

    expect(api.listKnowledgeBases).toHaveBeenCalledTimes(1)
  })
})
