import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import LoadingView from '../../src/views/LoadingView.vue'

// Mock vue-router
const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mockPush }),
  createRouter: vi.fn(() => ({ beforeEach: vi.fn(), beforeResolve: vi.fn(), afterEach: vi.fn() })),
  createWebHistory: vi.fn(),
}))

// Mock useAuth
vi.mock('../../src/composables/useAuth', () => ({
  useAuth: () => ({
    user: { value: { display_name: 'Admin', username: 'admin' } },
    token: { value: 'test-token' }
  })
}))

// Mock fetch — 按 URL 返回不同响应
global.fetch = vi.fn((url) => {
  if (url === '/api/setup/status') {
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ data: { setup_complete: true } }),
    })
  }
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve({ data: {} }),
  })
})

describe('LoadingView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders loading page', () => {
    const wrapper = mount(LoadingView)
    expect(wrapper.find('.loading-page').exists()).toBe(true)
  })

  it('shows greeting with user name', async () => {
    const wrapper = mount(LoadingView)
    await vi.advanceTimersByTimeAsync(1000) // Wait for typing animation
    expect(wrapper.find('.greeting').exists()).toBe(true)
  })

  it('shows status indicator', () => {
    const wrapper = mount(LoadingView)
    expect(wrapper.find('.status').exists()).toBe(true)
    expect(wrapper.find('.status-dot').exists()).toBe(true)
  })

  it('shows loading bar', () => {
    const wrapper = mount(LoadingView)
    expect(wrapper.find('.loader').exists()).toBe(true)
    expect(wrapper.find('.loader-bar').exists()).toBe(true)
  })

  it('checks services on mount', async () => {
    const wrapper = mount(LoadingView)
    await vi.advanceTimersByTimeAsync(2000) // Wait for typing + service check
    // Component should render without errors
    expect(wrapper.find('.loading-page').exists()).toBe(true)
  })

  it('redirects to /chat after services ready', async () => {
    mount(LoadingView)
    await vi.advanceTimersByTimeAsync(5000) // Wait for all checks
    expect(mockPush).toHaveBeenCalledWith('/chat')
  })
})
