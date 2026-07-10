import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import SessionsView from '../../src/views/SessionsView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: [
        { id: 'session-1', title: '测试会话', message_count: 5, created_at: Date.now(), updated_at: Date.now() }
      ]
    })
  })
)

// Mock vue-router
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() })
}))

describe('SessionsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders sessions page', () => {
    const wrapper = mount(SessionsView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(SessionsView)
    expect(wrapper.find('.page-header h1').text()).toBe('会话管理')
  })

  it('loads sessions on mount', async () => {
    mount(SessionsView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/sessions', { credentials: 'include' })
  })

  it('renders empty state when no sessions', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: [] })
      })
    )
    const wrapper = mount(SessionsView)
    await vi.dynamicImportSettled()
    expect(wrapper.find('.empty-state').exists()).toBe(true)
  })
})
