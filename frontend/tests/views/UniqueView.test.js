import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import UniqueView from '../../src/views/UniqueView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: {
        persona: '你是 Aether，一个本地家庭智能助手',
        capabilities: '你能控制 HA 设备',
        guidelines: '控制设备必须调用工具',
        persona_custom: false,
        capabilities_custom: false,
        guidelines_custom: false
      }
    })
  })
)

describe('UniqueView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders unique page', () => {
    const wrapper = mount(UniqueView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(UniqueView)
    expect(wrapper.find('.page-header h1').text()).toBe('助手人格')
  })

  it('loads config on mount', async () => {
    mount(UniqueView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/unique', { credentials: 'include' })
  })

  it('renders save button after loading', async () => {
    const wrapper = mount(UniqueView)
    await vi.dynamicImportSettled()
    expect(wrapper.text()).toContain('保存')
  })

  it('renders textarea fields after loading', async () => {
    const wrapper = mount(UniqueView)
    await vi.dynamicImportSettled()
    expect(wrapper.findAll('textarea').length).toBe(1)
  })
})
