import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { Table } from 'ant-design-vue'
import AdminPage from '../AdminPage.vue'
import AdminTable from '../AdminTable.vue'

window.matchMedia = vi.fn().mockImplementation((query: string) => ({
  matches: false,
  media: query,
  onchange: null,
  addListener: vi.fn(),
  removeListener: vi.fn(),
  addEventListener: vi.fn(),
  removeEventListener: vi.fn(),
  dispatchEvent: vi.fn(),
}))

describe('后台视觉基础组件', () => {
  it('AdminPage 统一呈现标题、描述和操作区', () => {
    const wrapper = mount(AdminPage, {
      props: { title: '服务商管理', description: '统一管理模型服务商' },
      slots: { actions: '<button type="button">新增</button>', default: '<p>内容</p>' },
    })
    expect(wrapper.classes()).toContain('admin-page')
    expect(wrapper.find('.admin-page-title').text()).toBe('服务商管理')
    expect(wrapper.find('.admin-page-description').text()).toBe('统一管理模型服务商')
    expect(wrapper.find('.admin-page-actions button').text()).toBe('新增')
    expect(wrapper.find('.admin-page-body p').text()).toBe('内容')
  })

  it('AdminTable 固定 middle 密度，空数据关闭分页并保留空态说明', () => {
    const wrapper = mount(AdminTable, {
      props: { columns: [{ title: '名称', dataIndex: 'name' }], dataSource: [], emptyText: '暂无服务商' },
    })
    expect(wrapper.classes()).toContain('admin-table')
    const table = wrapper.findComponent(Table)
    expect(table.props('size')).toBe('middle')
    expect(table.props('pagination')).toBe(false)
    expect(wrapper.find('.ant-pagination').exists()).toBe(false)
    expect(wrapper.text()).toContain('暂无服务商')
  })

  it('AdminTable 有数据时保留默认分页，并提供可聚焦的横向滚动区域', () => {
    const wrapper = mount(AdminTable, {
      props: { columns: [{ title: '名称', dataIndex: 'name' }], dataSource: [{ name: '服务商 A' }] },
    })
    expect(wrapper.findComponent(Table).props('pagination')).toEqual({ pageSize: 20, showSizeChanger: false })
    const region = wrapper.find('.admin-table-scroll')
    expect(region.attributes('tabindex')).toBe('0')
    expect(region.attributes('role')).toBe('region')
    expect(region.attributes('aria-label')).toBe('数据表格')
  })

  it('AdminTable 允许页面关闭分页并覆盖空态', () => {
    const wrapper = mount(AdminTable, {
      props: { columns: [], dataSource: [], pagination: false },
      slots: { emptyText: '<span class="custom-empty">自定义空态</span>' },
    })
    expect(wrapper.find('.ant-pagination').exists()).toBe(false)
    expect(wrapper.find('.custom-empty').text()).toBe('自定义空态')
  })
})
