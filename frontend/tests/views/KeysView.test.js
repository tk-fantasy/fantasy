import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import KeysView from '../../src/views/KeysView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: [
        { id: 'key1', base_url: 'https://api.example.com', model: 'gpt-4', type: 'chat', api_key_set: true },
        { id: 'key2', base_url: 'https://api.example.com', model: 'gpt-3.5', type: 'vision', api_key_set: false }
      ]
    })
  })
)

describe('KeysView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders keys page', () => {
    const wrapper = mount(KeysView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(KeysView)
    expect(wrapper.find('.page-header h1').text()).toBe('API Keys')
  })

  it('renders add button', () => {
    const wrapper = mount(KeysView)
    expect(wrapper.find('.btn-add').exists()).toBe(true)
    expect(wrapper.find('.btn-add').text()).toBe('+ 添加 Key')
  })

  it('loads keys on mount', async () => {
    mount(KeysView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/llm_keys', { credentials: 'include' })
  })

  it('shows form when add button clicked', async () => {
    const wrapper = mount(KeysView)
    await wrapper.find('.btn-add').trigger('click')
    expect(wrapper.find('.form-card').exists()).toBe(true)
  })

  it('type dropdown includes stt option', async () => {
    const wrapper = mount(KeysView)
    await wrapper.find('.btn-add').trigger('click')
    // FlowSelect 自定义下拉：点击 trigger 展开，断言选项含 stt
    const flowSelect = wrapper.findComponent({ name: 'FlowSelect' })
    await flowSelect.find('.trigger').trigger('click')
    const optionTexts = flowSelect.findAll('.option').map(o => o.text())
    expect(optionTexts).toContain('stt')
  })

  it('has no standalone STT config card after unification', async () => {
    const wrapper = mount(KeysView)
    await wrapper.find('.btn-add').trigger('click')
    // 改造后 STT 已纳入 llm_keys 体系，不应再有独立 STT 配置卡片
    expect(wrapper.find('.stt-card').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('语音识别')
  })
})
