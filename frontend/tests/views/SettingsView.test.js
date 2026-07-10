import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import SettingsView from '../../src/views/SettingsView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: {
        home_name: '测试家',
        owner_name: '测试用户',
        province: '上海市',
        city: '上海市',
        district: '宝山区'
      }
    })
  })
)

describe('SettingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders settings page', () => {
    const wrapper = mount(SettingsView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(SettingsView)
    expect(wrapper.find('.page-header h1').text()).toBe('设置')
  })

  it('renders home info section', () => {
    const wrapper = mount(SettingsView)
    expect(wrapper.find('.section-title').text()).toContain('家庭信息')
  })

  it('renders save button', () => {
    const wrapper = mount(SettingsView)
    expect(wrapper.find('.btn-primary').exists()).toBe(true)
    expect(wrapper.find('.btn-primary').text()).toBe('保存设置')
  })

  it('renders dark mode toggle', () => {
    const wrapper = mount(SettingsView)
    expect(wrapper.text()).toContain('深色模式')
  })

  it('loads home info on mount', async () => {
    mount(SettingsView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/home/info', { credentials: 'include' })
  })

  it('renders location selects', () => {
    const wrapper = mount(SettingsView)
    expect(wrapper.findAll('.flow-select').length).toBe(3) // province, city, district
  })
})
