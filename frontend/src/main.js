import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { LS_LOGGED_IN, LS_USER } from './utils/constants'
import './style.css'

// 全局拦截 fetch：自动携带 cookie；遇 401 自动刷新 token；
// 刷新失败则会话不可恢复 → 清理本地状态并跳登录（只跳一次）。
const originalFetch = window.fetch
let refreshFailed = false       // 刷新是否已失败（会话不可恢复）
let loginRedirectPending = false
let refreshPromise = null       // 当前正在进行的 refresh（共享 Promise，杜绝并发重复刷新）

// 会话过期：清理本地状态并跳转登录页（保证只跳一次）
function handleSessionExpired() {
  refreshFailed = true
  localStorage.removeItem(LS_LOGGED_IN)
  localStorage.removeItem(LS_USER)
  // 通知 useAuth 同步 loggedIn 状态，避免路由守卫用过期标志放行
  window.dispatchEvent(new Event('aether:session-expired'))
  if (!loginRedirectPending) {
    loginRedirectPending = true
    router.push('/login').finally(() => { loginRedirectPending = false })
  }
}

// 构造一个"会话已过期"的合成 401 响应（每次新建，避免 Response 被重复消费）
function makeExpiredResponse() {
  return new Response(
    JSON.stringify({ detail: '会话已过期，请重新登录' }),
    { status: 401, headers: { 'Content-Type': 'application/json' } }
  )
}

// 单例刷新：所有并发 401 共享同一个 refresh 请求，避免风暴。
// 返回 true=刷新成功（可重试原请求），false=刷新失败（会话不可恢复）。
function doRefresh() {
  if (refreshPromise) return refreshPromise
  refreshPromise = (async () => {
    try {
      const refreshRes = await originalFetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',
      })
      if (refreshRes.ok) {
        refreshFailed = false
        return true
      }
      handleSessionExpired()
      return false
    } catch {
      handleSessionExpired()
      return false
    } finally {
      // 稍延迟清空，让本轮并发排队者能复用同一结果，新一轮 401 再触发刷新
      setTimeout(() => { refreshPromise = null }, 0)
    }
  })()
  return refreshPromise
}

// 后端 startup 期间（健康检查 + RAG 索引约需 8s）vite proxy 转发会返回 502/503。
// 浏览器对每次失败的 fetch 都会记录 502，前端重试只会让 console 红字更多、无法消除。
// 正确做法：用 vite proxy 层重试（对浏览器透明）+ 前端启动门控（后端没就绪不发请求）。
// 启动门控见 App.vue 的 waitForBackend；proxy 层重试见 vite.config.js。

window.fetch = async function (url, options = {}) {
  const urlStr = typeof url === 'string' ? url : url.url || ''
  const isAuthReq = urlStr.includes('/auth/')
  options.credentials = options.credentials || 'include'

  // 会话已不可恢复：非 auth 请求直接短路，不再发往后端，避免 401 风暴
  if (refreshFailed && !isAuthReq) {
    return makeExpiredResponse()
  }

  const response = await originalFetch.call(this, url, options)

  // 非 401 或 auth 请求自身（login/refresh/logout 自身）不在此拦截
  if (response.status !== 401 || isAuthReq) return response

  if (refreshFailed) return response

  // 共享单例刷新：并发 401 只会触发一次 /api/auth/refresh
  const ok = await doRefresh()
  if (!ok) return response
  // 刷新成功：用新 cookie 重试当前请求
  return await originalFetch.call(this, url, options)
}

// 登录成功后重置刷新失败标志（否则会拦截后续所有 /api 请求）
window.addEventListener('aether:login-success', () => {
  refreshFailed = false
})

// PWA service worker 注册（仅生产构建生效；dev 模式 devOptions.enabled=false，注册为空操作）
import { registerSW } from 'virtual:pwa-register'
registerSW({ immediate: true })

createApp(App).use(router).mount('#app')
