import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import FocusView from '../../src/views/FocusView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: [
        { id: '1', text: '检测人员活动', enabled: true },
        { id: '2', text: '监控门口', enabled: false }
      ]
    })
  })
)

describe('FocusView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders focus page', () => {
    const wrapper = mount(FocusView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(FocusView)
    expect(wrapper.find('.page-header h1').text()).toBe('视觉关注')
  })

  it('loads focuses on mount', async () => {
    mount(FocusView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/vision/focuses', { credentials: 'include' })
  })

  it('renders input for new focus', () => {
    const wrapper = mount(FocusView)
    expect(wrapper.find('input').exists()).toBe(true)
  })

  it('renders add button', () => {
    const wrapper = mount(FocusView)
    expect(wrapper.text()).toContain('添加')
  })
})
