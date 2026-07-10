import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useAuth } from '../../src/composables/useAuth'

// Mock fetch
global.fetch = vi.fn()

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

describe('useAuth', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorageMock.clear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('returns auth functions', () => {
    const auth = useAuth()
    expect(auth.login).toBeDefined()
    expect(auth.register).toBeDefined()
    expect(auth.logout).toBeDefined()
    expect(auth.isAuthenticated).toBeDefined()
  })

  it('isAuthenticated is false initially', () => {
    const auth = useAuth()
    expect(auth.isAuthenticated.value).toBe(false)
  })

  it('login sets logged_in flag and user', async () => {
    const mockResponse = {
      ok: true,
      json: () => Promise.resolve({
        data: {
          user: { id: '1', username: 'admin', display_name: 'Admin' }
        }
      })
    }
    global.fetch.mockResolvedValueOnce(mockResponse)

    const auth = useAuth()
    await auth.login('admin', 'password')

    expect(auth.isAuthenticated.value).toBe(true)
    expect(auth.user.value.username).toBe('admin')
    expect(localStorageMock.setItem).toHaveBeenCalledWith('aether_logged_in', 'true')
  })

  it('login throws on failure', async () => {
    const mockResponse = {
      ok: false,
      json: () => Promise.resolve({ detail: 'Invalid credentials' })
    }
    global.fetch.mockResolvedValueOnce(mockResponse)

    const auth = useAuth()
    await expect(auth.login('admin', 'wrong')).rejects.toThrow('Invalid credentials')
  })

  it('register sets logged_in flag and user', async () => {
    const mockResponse = {
      ok: true,
      json: () => Promise.resolve({
        data: {
          user: { id: '2', username: 'newuser', display_name: 'New User' }
        }
      })
    }
    global.fetch.mockResolvedValueOnce(mockResponse)

    const auth = useAuth()
    await auth.register('newuser', 'password', 'New User')

    expect(auth.isAuthenticated.value).toBe(true)
    expect(auth.user.value.username).toBe('newuser')
  })

  it('logout calls backend and clears state', async () => {
    // First login
    const mockResponse = {
      ok: true,
      json: () => Promise.resolve({
        data: {
          user: { id: '1', username: 'admin' }
        }
      })
    }
    global.fetch.mockResolvedValueOnce(mockResponse)

    const auth = useAuth()
    await auth.login('admin', 'password')
    expect(auth.isAuthenticated.value).toBe(true)

    // Then logout
    global.fetch.mockResolvedValueOnce({ ok: true })
    await auth.logout()
    expect(auth.isAuthenticated.value).toBe(false)
    expect(auth.user.value).toBeNull()
    expect(localStorageMock.removeItem).toHaveBeenCalledWith('aether_logged_in')
  })
})
