import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import SetupWizardView from '../../src/views/SetupWizardView.vue'

// Mock vue-router
const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush })
}))

// Mock useAuth
vi.mock('../../src/composables/useAuth', () => ({
  useAuth: () => ({
    user: { display_name: 'Admin', username: 'admin' },
    token: { value: 'test-token' }
  })
}))

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: {
        setup_complete: false,
        has_llm_key: false,
        ha_connected: false,
        has_home_info: false,
      }
    })
  })
)

describe('SetupWizardView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders setup page', () => {
    const wrapper = mount(SetupWizardView)
    expect(wrapper.find('.setup-page').exists()).toBe(true)
  })

  it('shows welcome message', () => {
    const wrapper = mount(SetupWizardView)
    expect(wrapper.find('.setup-title').text()).toBe('初始配置')
    expect(wrapper.find('.setup-subtitle').text()).toContain('欢迎使用 Aether')
  })

  it('shows progress bar', () => {
    const wrapper = mount(SetupWizardView)
    expect(wrapper.find('.progress-bar').exists()).toBe(true)
    expect(wrapper.find('.progress-fill').exists()).toBe(true)
  })

  it('starts at step 1 - home info', () => {
    const wrapper = mount(SetupWizardView)
    expect(wrapper.find('.step-title').text()).toBe('家庭信息')
  })

  it('shows home info form at step 1', () => {
    const wrapper = mount(SetupWizardView)
    expect(wrapper.find('input[placeholder="我的家"]').exists()).toBe(true)
    expect(wrapper.find('input[placeholder="小童"]').exists()).toBe(true)
  })

  it('shows next button', () => {
    const wrapper = mount(SetupWizardView)
    const btn = wrapper.find('.btn-primary')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toBe('下一步')
  })

  it('disables next button when form is incomplete', () => {
    const wrapper = mount(SetupWizardView)
    const btn = wrapper.find('.btn-primary')
    expect(btn.attributes('disabled')).toBeDefined()
  })

  it('shows 3 steps total', () => {
    const wrapper = mount(SetupWizardView)
    expect(wrapper.find('.step-indicator').text()).toContain('步骤 1 / 3')
  })

  it('HA 配置保存返回 422 校验错误时显示可读中文而非 [object Object]', async () => {
    // setup/status：前两步已完成 → 直接落到步骤 3（HA）
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: { has_home_info: true, has_llm_key: true } }),
    })
    const wrapper = mount(SetupWizardView)
    await flushPromises()
    expect(wrapper.find('.step-title').text()).toBe('Home Assistant 连接')

    // url 已有默认值，填令牌解锁「完成配置」按钮
    await wrapper.find('input[type="password"]').setValue('short')
    fetch.mockResolvedValueOnce({
      ok: false,
      status: 422,
      json: () => Promise.resolve({
        detail: [{
          type: 'string_too_short',
          loc: ['body', 'token'],
          msg: 'String should have at least 8 characters',
          ctx: { min_length: 8 },
        }],
      }),
    })

    await wrapper.find('.btn-primary').trigger('click')
    await flushPromises()

    const errText = wrapper.find('.error-message').text()
    expect(errText).not.toBe('[object Object]')
    expect(errText).toContain('令牌长度不足（至少 8 位）')
  })

  it('LLM key 测试连接返回 422 校验错误时错误可读而非 [object Object]', async () => {
    const wrapper = mount(SetupWizardView)
    await flushPromises()

    // 步骤 1：填家庭信息 → 下一步
    await wrapper.find('input[placeholder="我的家"]').setValue('我的家')
    await wrapper.find('input[placeholder="小童"]').setValue('小童')
    fetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: {} }) })
    await wrapper.find('.btn-primary').trigger('click')
    await flushPromises()
    expect(wrapper.find('.step-title').text()).toBe('LLM 模型配置')

    // 步骤 2：填 chat 角色表单 → 下一步触发 key 测试连接
    await wrapper.find('input[placeholder="https://api.openai.com/v1"]').setValue('https://api.example.com/v1')
    await wrapper.find('input[placeholder="sk-..."]').setValue('sk-short')
    await wrapper.find('input[placeholder="gpt-4o-mini"]').setValue('gpt-test')
    fetch.mockResolvedValueOnce({
      ok: false,
      status: 422,
      json: () => Promise.resolve({
        detail: [{
          type: 'string_too_short',
          loc: ['body', 'api_key'],
          msg: 'String should have at least 8 characters',
          ctx: { min_length: 8 },
        }],
      }),
    })

    await wrapper.find('.btn-primary').trigger('click')
    await flushPromises()

    const errText = wrapper.find('.error-message').text()
    expect(errText).not.toBe('[object Object]')
    expect(errText).toContain('API Key长度不足（至少 8 位）')
  })
})
