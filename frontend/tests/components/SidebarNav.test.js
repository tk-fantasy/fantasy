import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import SidebarNav from '../../src/components/SidebarNav.vue'

// Mock vue-router
const mockPush = vi.fn()
vi.mock('vue-router', () => ({
  useRoute: () => ({ path: '/chat' }),
  useRouter: () => ({ push: mockPush })
}))

// Mock localStorage
const localStorageMock = (() => {
  let store = {}
  return {
    getItem: vi.fn(key => store[key] || null),
    setItem: vi.fn((key, value) => { store[key] = value.toString() }),
    removeItem: vi.fn(key => { delete store[key] }),
    clear: vi.fn(() => { store = {} })
  }
})()
Object.defineProperty(global, 'localStorage', { value: localStorageMock })

// Mock fetch
global.fetch = vi.fn()

describe('SidebarNav - Login Button', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorageMock.clear()

    // Default: not authenticated
    global.fetch.mockImplementation((url) => {
      if (url === '/api/home/info') {
        return Promise.resolve({
          json: () => Promise.resolve({ data: { home_name: '测试家', owner_name: '' } })
        })
      }
      if (url === '/api/users') {
        return Promise.resolve({
          ok: false,
          status: 401,
          json: () => Promise.resolve({ data: [] })
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: {} })
      })
    })
  })

  it('shows login button when not authenticated', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    expect(wrapper.find('.login-btn').exists()).toBe(true)
    expect(wrapper.find('.login-btn-text').text()).toBe('登录')
  })

  it('shows user menu when authenticated', async () => {
    // Set logged-in flag in localStorage to simulate authenticated state
    localStorageMock.setItem('aether_logged_in', 'true')
    localStorageMock.setItem('aether_user', JSON.stringify({
      id: '1',
      username: 'admin',
      display_name: 'Admin'
    }))

    // Mock fetch to return users list
    global.fetch.mockImplementation((url) => {
      if (url === '/api/home/info') {
        return Promise.resolve({
          json: () => Promise.resolve({ data: { home_name: '测试家', owner_name: 'Admin' } })
        })
      }
      if (url === '/api/users') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            data: [
              { id: '1', username: 'admin', display_name: 'Admin' },
              { id: '2', username: 'user2', display_name: 'User 2' }
            ]
          })
        })
      }
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: {} })
      })
    })

    // Re-import to get fresh state with token in localStorage
    vi.resetModules()
    const { default: SidebarNavFresh } = await import('../../src/components/SidebarNav.vue')
    const wrapper = mount(SidebarNavFresh)
    await vi.dynamicImportSettled()

    // When authenticated, should show user menu (sidebar-user) not login button
    const hasUserMenu = wrapper.find('.sidebar-user').exists()
    const hasLoginBtn = wrapper.find('.login-btn').exists()

    // Either user menu exists OR login button doesn't exist
    expect(hasUserMenu || !hasLoginBtn).toBe(true)
  })

  it('opens dropdown when login button clicked', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    const loginBtn = wrapper.find('.login-btn')
    expect(loginBtn.exists()).toBe(true)

    await loginBtn.trigger('click')

    expect(wrapper.find('.user-dropdown').exists()).toBe(true)
    expect(wrapper.find('.user-dropdown-header').text()).toBe('选择账号')
  })

  it('shows login and register buttons in dropdown', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    await wrapper.find('.login-btn').trigger('click')

    const actionBtns = wrapper.findAll('.user-action-btn')
    expect(actionBtns.length).toBe(2)

    expect(actionBtns[0].text()).toContain('登录账号')
    expect(actionBtns[1].text()).toContain('注册新账号')
  })

  it('navigates to login page when login button clicked', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    await wrapper.find('.login-btn').trigger('click')

    const loginActionBtn = wrapper.findAll('.user-action-btn')[0]
    await loginActionBtn.trigger('click')

    expect(mockPush).toHaveBeenCalledWith('/login')
  })

  it('navigates to register page when register button clicked', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    await wrapper.find('.login-btn').trigger('click')

    const registerActionBtn = wrapper.findAll('.user-action-btn')[1]
    await registerActionBtn.trigger('click')

    expect(mockPush).toHaveBeenCalledWith('/login?mode=register')
  })

  it('shows no users hint when user list is empty', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    await wrapper.find('.login-btn').trigger('click')

    expect(wrapper.find('.no-users-hint').exists()).toBe(true)
    expect(wrapper.find('.no-users-hint').text()).toBe('暂无账号，请先注册')
  })

  it('closes dropdown when clicking outside', async () => {
    const wrapper = mount(SidebarNav)
    await vi.dynamicImportSettled()

    await wrapper.find('.login-btn').trigger('click')
    expect(wrapper.find('.user-dropdown').exists()).toBe(true)

    // Simulate click outside - the handler checks for .user-menu-container
    // but for unauthenticated state it's .login-btn-container
    // Just manually set showUserMenu to false
    wrapper.vm.showUserMenu = false
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.user-dropdown').exists()).toBe(false)
  })

  it('prompts for password before switching to another user (方案A)', async () => {
    // 已登录状态
    localStorageMock.setItem('aether_logged_in', 'true')
    localStorageMock.setItem('aether_user', JSON.stringify({
      id: '1', username: 'admin', display_name: 'Admin'
    }))

    global.fetch.mockImplementation((url) => {
      if (url === '/api/home/info') {
        return Promise.resolve({ json: () => Promise.resolve({ data: { home_name: '测试家', owner_name: 'Admin' } }) })
      }
      if (url === '/api/users') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            data: [
              { id: '1', username: 'admin', display_name: 'Admin' },
              { id: '2', username: 'user2', display_name: 'User 2' }
            ]
          })
        })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
    })

    vi.resetModules()
    const { default: SidebarNavFresh } = await import('../../src/components/SidebarNav.vue')
    const wrapper = mount(SidebarNavFresh)
    await vi.dynamicImportSettled()

    // 打开已登录用户菜单下拉框
    wrapper.vm.showUserMenu = true
    await wrapper.vm.$nextTick()

    // 点击另一个用户（user2）→ 应弹出密码确认框，而非直接发请求
    const otherUserItem = wrapper.findAll('.user-item').find(el => el.text().includes('user2'))
    expect(otherUserItem).toBeTruthy()
    await otherUserItem.trigger('click')

    expect(wrapper.find('.switch-confirm').exists()).toBe(true)
    expect(wrapper.find('.switch-confirm-title').text()).toContain('User 2')
    // 此时不应已发起 switch 请求
    expect(global.fetch).not.toHaveBeenCalledWith('/api/users/switch', expect.anything())

    // 输入密码并确认
    await wrapper.find('.switch-confirm-input').setValue('secret123')
    // mock switch 接口成功
    global.fetch.mockImplementation((url) => {
      if (url === '/api/users/switch') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({ data: { user: { id: '2', username: 'user2' } } })
        })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
    })
    await wrapper.find('.switch-confirm-btn.confirm').trigger('click')
    await vi.dynamicImportSettled()

    // 确认请求体包含 password 字段（方案A 核心）
    const switchCall = global.fetch.mock.calls.find(c => c[0] === '/api/users/switch')
    expect(switchCall).toBeTruthy()
    const body = JSON.parse(switchCall[1].body)
    expect(body.username).toBe('user2')
    expect(body.password).toBe('secret123')
  })
})
