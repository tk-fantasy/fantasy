import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import HAView from '../../src/views/HAView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: { url: 'http://localhost:8123', token_set: true, token_preview: 'abcd****efgh' }
    })
  })
)

describe('HAView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders HA page', () => {
    const wrapper = mount(HAView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(HAView)
    expect(wrapper.find('.page-header h1').text()).toBe('Home Assistant')
  })

  it('loads config on mount', async () => {
    mount(HAView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/config', { credentials: 'include' })
  })

  it('renders URL input', async () => {
    const wrapper = mount(HAView)
    await vi.dynamicImportSettled()
    expect(wrapper.find('input').exists()).toBe(true)
  })

  it('renders test button', async () => {
    const wrapper = mount(HAView)
    await vi.dynamicImportSettled()
    expect(wrapper.text()).toContain('测试连接')
  })
})
