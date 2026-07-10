import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock useAuth
const mockIsAuthenticated = { value: false }
vi.mock('../src/composables/useAuth', () => ({
  useAuth: () => ({
    isAuthenticated: mockIsAuthenticated
  })
}))

// Mock vue-router
const mockNext = vi.fn()
const mockTo = { name: 'Chat', fullPath: '/chat' }
const mockFrom = { name: null }

vi.mock('vue-router', () => ({
  createRouter: vi.fn(() => ({
    beforeEach: vi.fn((cb) => {
      // Store the callback for testing
      global.__routerBeforeEach = cb
    }),
    beforeResolve: vi.fn(),
    afterEach: vi.fn()
  })),
  createWebHistory: vi.fn()
}))

describe('Router Guards', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockIsAuthenticated.value = false
  })

  it('redirects to login when not authenticated', async () => {
    // Import router to trigger beforeEach registration
    await import('../src/router/index.js')
    
    const beforeEach = global.__routerBeforeEach
    expect(beforeEach).toBeDefined()
    
    beforeEach(mockTo, mockFrom, mockNext)
    
    expect(mockNext).toHaveBeenCalledWith({ name: 'Login', query: { redirect: '/chat' } })
  })

  it('allows access when authenticated', async () => {
    mockIsAuthenticated.value = true

    await import('../src/router/index.js')
    const { markBackendReady } = await import('../src/router/index.js')
    markBackendReady()
    const beforeEach = global.__routerBeforeEach

    beforeEach(mockTo, mockFrom, mockNext)

    expect(mockNext).toHaveBeenCalledWith()
  })

  it('allows access to login page when not authenticated', async () => {
    const loginTo = { name: 'Login', fullPath: '/login' }
    
    await import('../src/router/index.js')
    const beforeEach = global.__routerBeforeEach
    
    beforeEach(loginTo, mockFrom, mockNext)
    
    expect(mockNext).toHaveBeenCalledWith()
  })

  it('redirects to landing when authenticated user visits login', async () => {
    mockIsAuthenticated.value = true
    const loginTo = { name: 'Login', fullPath: '/login' }
    
    await import('../src/router/index.js')
    const beforeEach = global.__routerBeforeEach
    
    beforeEach(loginTo, mockFrom, mockNext)
    
    expect(mockNext).toHaveBeenCalledWith('/landing')
  })
})
