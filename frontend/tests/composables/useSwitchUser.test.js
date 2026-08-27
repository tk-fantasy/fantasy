import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'
import { useSwitchUser } from '../../src/composables/useSwitchUser'

global.fetch = vi.fn()

function makeCtx({ authed = true, currentUsername = 'alice' } = {}) {
  const user = ref({ username: currentUsername, display_name: 'Alice' })
  const isAuthenticated = ref(authed)
  const router = { push: vi.fn() }
  const api = useSwitchUser(user, isAuthenticated, router)
  return { ...api, user, isAuthenticated, router }
}

describe('useSwitchUser', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    // jsdom 的 location.reload 只读不可赋值，整体换成可检查的桩
    // （composable 里调 window.location.reload()；jsdom 下 window === globalThis）
    vi.stubGlobal('location', {
      href: 'http://localhost/',
      assign: vi.fn(),
      replace: vi.fn(),
      reload: vi.fn(),
    })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('loadUsers 成功时填充列表；非 2xx 时清空', async () => {
    const ctx = makeCtx()
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: [{ username: 'bob', display_name: 'Bob' }] }),
    })
    await ctx.loadUsers()
    expect(ctx.users.value).toEqual([{ username: 'bob', display_name: 'Bob' }])

    fetch.mockResolvedValueOnce({ ok: false })
    await ctx.loadUsers()
    expect(ctx.users.value).toEqual([])
  })

  it('loadUsers 网络异常兜底为空数组且不抛出', async () => {
    const ctx = makeCtx()
    fetch.mockRejectedValueOnce(new Error('offline'))
    await ctx.loadUsers()
    expect(ctx.users.value).toEqual([])
  })

  it('点自己不弹确认；未登录改跳登录页；已登录记录待切换目标', () => {
    const ctx = makeCtx({ currentUsername: 'alice' })

    ctx.promptSwitchUser({ username: 'alice' }) // 自己
    expect(ctx.pendingSwitch.value).toBeNull()

    ctx.promptSwitchUser({ username: 'bob' }) // 已登录
    expect(ctx.pendingSwitch.value).toEqual({ username: 'bob', displayName: 'bob' })

    const anon = makeCtx({ authed: false })
    anon.router.push.mockClear()
    anon.promptSwitchUser({ username: 'bob' })
    expect(anon.router.push).toHaveBeenCalledWith('/login')
    expect(anon.pendingSwitch.value).toBeNull()
  })

  it('promptSwitchUser 用 display_name 展示', () => {
    const ctx = makeCtx()
    ctx.promptSwitchUser({ username: 'bob', display_name: '老王' })
    expect(ctx.pendingSwitch.value.displayName).toBe('老王')
  })

  it('cancelSwitch 清空待确认状态', () => {
    const ctx = makeCtx()
    ctx.promptSwitchUser({ username: 'bob' })
    ctx.cancelSwitch()
    expect(ctx.pendingSwitch.value).toBeNull()
    expect(ctx.switchPassword.value).toBe('')
  })

  it('confirmSwitchUser：无目标或无密码时不发请求', async () => {
    const ctx = makeCtx()
    await ctx.confirmSwitchUser() // 无 pending
    ctx.pendingSwitch.value = { username: 'bob' }
    ctx.switchPassword.value = ''
    await ctx.confirmSwitchUser() // 无密码
    expect(fetch).not.toHaveBeenCalled()
  })

  it('密码正确：写入 localStorage 并整页刷新', async () => {
    const bobUser = { id: '2', username: 'bob' }
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: { user: bobUser } }),
    })
    const ctx = makeCtx()
    ctx.promptSwitchUser({ username: 'bob' })
    ctx.switchPassword.value = 'pw'
    await ctx.confirmSwitchUser()

    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/api/users/switch')
    expect(JSON.parse(opts.body)).toEqual({ username: 'bob', password: 'pw' })
    // LS_USER = aether_user：切换后新用户信息落 localStorage
    expect(JSON.parse(localStorage.getItem('aether_user'))).toEqual(bobUser)
    expect(location.reload).toHaveBeenCalledTimes(1)
    expect(ctx.switchingUser.value).toBe(false) // finally 复位
  })

  it('密码错误：显示后端 message 不刷新', async () => {
    fetch.mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ message: '密码错误' }),
    })
    const ctx = makeCtx()
    ctx.promptSwitchUser({ username: 'bob' })
    ctx.switchPassword.value = 'wrong'
    await ctx.confirmSwitchUser()

    expect(ctx.switchError.value).toBe('密码错误')
    expect(window.location.reload).not.toHaveBeenCalled()
  })

  it('网络异常：兜底错误文案，switchingUser 复位', async () => {
    fetch.mockRejectedValueOnce(new Error('offline'))
    const ctx = makeCtx()
    ctx.promptSwitchUser({ username: 'bob' })
    ctx.switchPassword.value = 'pw'
    await ctx.confirmSwitchUser()

    expect(ctx.switchError.value).toBe('切换用户失败')
    expect(ctx.switchingUser.value).toBe(false)
  })
})
