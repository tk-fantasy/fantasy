import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import LoginView from '../../src/views/LoginView.vue'

// Mock vue-router
const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  useRoute: () => ({ query: {} })
}))

// Mock useAuth
const mockLogin = vi.fn()
const mockRegister = vi.fn()
vi.mock('../../src/composables/useAuth', () => ({
  useAuth: () => ({
    login: mockLogin,
    register: mockRegister
  })
}))

describe('LoginView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders login form', () => {
    const wrapper = mount(LoginView)
    expect(wrapper.find('.login-page').exists()).toBe(true)
    expect(wrapper.find('.login-card').exists()).toBe(true)
  })

  it('shows login title by default', () => {
    const wrapper = mount(LoginView)
    expect(wrapper.find('.title').text()).toBe('欢迎回来')
    expect(wrapper.find('.subtitle').text()).toBe('登录以继续')
  })

  it('switches to register mode', async () => {
    const wrapper = mount(LoginView)
    await wrapper.find('.switch-btn').trigger('click')
    expect(wrapper.find('.title').text()).toBe('创建账户')
    expect(wrapper.find('.subtitle').text()).toBe('注册你的 Aether 账户')
  })

  it('has username and password inputs', () => {
    const wrapper = mount(LoginView)
    expect(wrapper.find('input[type="text"]').exists()).toBe(true)
    expect(wrapper.find('input[type="password"]').exists()).toBe(true)
  })

  it('shows display name input in register mode', async () => {
    const wrapper = mount(LoginView)
    await wrapper.find('.switch-btn').trigger('click')
    const inputs = wrapper.findAll('input[type="text"]')
    expect(inputs.length).toBe(2) // username + display name
  })

  it('has submit button', () => {
    const wrapper = mount(LoginView)
    expect(wrapper.find('.submit-btn').exists()).toBe(true)
    expect(wrapper.find('.submit-btn').text()).toBe('登录')
  })

  it('calls login on form submit', async () => {
    mockLogin.mockResolvedValueOnce({})
    const wrapper = mount(LoginView)
    
    await wrapper.find('input[type="text"]').setValue('admin')
    await wrapper.find('input[type="password"]').setValue('password')
    await wrapper.find('form').trigger('submit.prevent')
    
    expect(mockLogin).toHaveBeenCalledWith('admin', 'password')
  })

  it('shows error message on login failure', async () => {
    mockLogin.mockRejectedValueOnce(new Error('登录失败'))
    const wrapper = mount(LoginView)
    
    await wrapper.find('input[type="text"]').setValue('admin')
    await wrapper.find('input[type="password"]').setValue('wrong')
    await wrapper.find('form').trigger('submit.prevent')
    
    await vi.dynamicImportSettled()
    expect(wrapper.find('.form-error').exists()).toBe(true)
    expect(wrapper.find('.form-error').text()).toBe('登录失败')
  })

  it('redirects to /loading after successful login', async () => {
    mockLogin.mockResolvedValueOnce({})
    const wrapper = mount(LoginView)
    
    await wrapper.find('input[type="text"]').setValue('admin')
    await wrapper.find('input[type="password"]').setValue('password')
    await wrapper.find('form').trigger('submit.prevent')
    
    await vi.dynamicImportSettled()
    expect(mockPush).toHaveBeenCalledWith('/loading')
  })
})
