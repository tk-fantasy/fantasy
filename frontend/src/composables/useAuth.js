/**
 * 认证状态管理 composable
 * Token 存储在 httpOnly cookie 中（JS 不可见），前端仅管理登录状态标志
 */
import { ref, computed } from 'vue'
import { LS_LOGGED_IN, LS_USER } from '../utils/constants'

// 登录状态标志（非敏感，仅用于 UI 判断）
const loggedIn = ref(localStorage.getItem(LS_LOGGED_IN) === 'true')
const user = ref(JSON.parse(localStorage.getItem(LS_USER) || 'null'))

// 会话过期（main.js 拦截器在 refresh 失败时派发）：同步清掉本地状态，
// 否则路由守卫仍凭过期的 loggedIn 标志放行，导致进主页后满屏 401。
window.addEventListener('aether:session-expired', () => {
  loggedIn.value = false
  user.value = null
})

export function useAuth() {
  const isAuthenticated = computed(() => loggedIn.value)

  function setUser(userData) {
    user.value = userData
    localStorage.setItem(LS_USER, JSON.stringify(userData))
  }

  async function login(username, password) {
    const res = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password }),
    })
    const json = await res.json()
    if (!res.ok) {
      throw new Error(json.detail || json.message || '登录失败')
    }
    loggedIn.value = true
    localStorage.setItem(LS_LOGGED_IN, 'true')
    setUser(json.data.user)
    // 登录拿到全新 access+refresh token，重置 main.js 的刷新失败标志
    // 否则之前的 refreshFailed=true 会拦截后续所有 /api 请求
    window.dispatchEvent(new Event('aether:login-success'))
    return json.data
  }

  async function register(username, password, displayName) {
    const res = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ username, password, display_name: displayName || username }),
    })
    const json = await res.json()
    if (!res.ok) {
      throw new Error(json.detail || json.message || '注册失败')
    }
    loggedIn.value = true
    localStorage.setItem(LS_LOGGED_IN, 'true')
    setUser(json.data.user)
    window.dispatchEvent(new Event('aether:login-success'))
    return json.data
  }

  async function logout() {
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'include',
      })
    } catch {
      // 即使请求失败也要清除本地状态
    }
    loggedIn.value = false
    user.value = null
    localStorage.removeItem(LS_LOGGED_IN)
    localStorage.removeItem(LS_USER)
  }

  return {
    user,
    isAuthenticated,
    login,
    register,
    logout,
  }
}
