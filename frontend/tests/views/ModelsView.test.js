import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ModelsView from '../../src/views/ModelsView.vue'

// Mock fetch
global.fetch = vi.fn((url) => {
  if (url === '/api/llm_keys') {
    return Promise.resolve({
      ok: true,
      json: () => Promise.resolve({
        data: [
          { id: 'key1', model: 'gpt-4', type: 'chat' },
          { id: 'key2', model: 'gpt-3.5', type: 'vision' }
        ]
      })
    })
  }
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: {
        current: { chat: { key_id: 'key1' }, vision: { key_id: 'key2' }, summary: { key_id: '' }, embed: { key_id: '' } },
        warnings: []
      }
    })
  })
})

describe('ModelsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders models page', () => {
    const wrapper = mount(ModelsView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(ModelsView)
    expect(wrapper.find('.page-header h1').text()).toBe('模型配置')
  })

  it('loads data on mount', async () => {
    mount(ModelsView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/llm_keys', { credentials: 'include' })
    expect(global.fetch).toHaveBeenCalledWith('/api/llm/settings', { credentials: 'include' })
  })

  it('renders role sections', async () => {
    const wrapper = mount(ModelsView)
    await vi.dynamicImportSettled()
    expect(wrapper.text()).toContain('对话')
    expect(wrapper.text()).toContain('视觉')
  })
})
