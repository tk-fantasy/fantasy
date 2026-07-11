import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
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
})
