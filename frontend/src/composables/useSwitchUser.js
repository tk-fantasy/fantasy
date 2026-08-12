/**
 * 用户切换 composable（从 SidebarNav 拆出）。
 *
 * 方案 A：切换到其它用户需输入目标用户的密码确认。
 *
 * 依赖 useAuth 的 user / isAuthenticated（判断当前登录态）和 router
 * （未登录时跳登录页）。切换成功后整页刷新加载新用户配置（会话按用户命名空间隔离，无需清）。
 */
import { ref } from 'vue'
import { LS_USER } from '../utils/constants'

export function useSwitchUser(user, isAuthenticated, router) {
  const users = ref([])
  const switchingUser = ref(false)
  // 切换用户时的密码确认子状态
  const pendingSwitch = ref(null) // { username, displayName }
  const switchPassword = ref('')
  const switchError = ref('')

  // 加载用户列表
  async function loadUsers() {
    try {
      const res = await fetch('/api/users')
      if (res.ok) {
        const json = await res.json()
        users.value = json.data || []
      } else {
        // 未认证时用户列表为空
        users.value = []
      }
    } catch (e) {
      console.error('Failed to load users:', e)
      users.value = []
    }
  }

  // 切换用户：点击用户后先要求输入目标用户密码（方案A：切换需密码确认）
  function promptSwitchUser(u) {
    if (u.username === user.value?.username) return
    // 未登录时，跳转到登录页（路由切换后 SidebarNav 卸载，菜单自然消失）
    if (!isAuthenticated.value) {
      router.push('/login')
      return
    }
    pendingSwitch.value = { username: u.username, displayName: u.display_name || u.username }
    switchPassword.value = ''
    switchError.value = ''
  }

  function cancelSwitch() {
    pendingSwitch.value = null
    switchPassword.value = ''
    switchError.value = ''
  }

  async function confirmSwitchUser() {
    if (!pendingSwitch.value || !switchPassword.value) return
    const username = pendingSwitch.value.username
    switchingUser.value = true
    switchError.value = ''
    try {
      const res = await fetch('/api/users/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password: switchPassword.value })
      })

      if (res.ok) {
        const json = await res.json()
        // 更新用户信息
        if (json.data.user) {
          localStorage.setItem(LS_USER, JSON.stringify(json.data.user))
        }
        // 命名空间隔离：各用户会话键独立，无需清（reload 后 ChatView 读新用户的键）
        // 切换成功后刷新页面以加载新用户的配置
        window.location.reload()
      } else {
        const json = await res.json().catch(() => ({}))
        switchError.value = json.message || '切换用户失败'
      }
    } catch (e) {
      console.error('Failed to switch user:', e)
      switchError.value = '切换用户失败'
    } finally {
      switchingUser.value = false
    }
  }

  return {
    users, switchingUser, pendingSwitch, switchPassword, switchError,
    loadUsers, promptSwitchUser, cancelSwitch, confirmSwitchUser,
  }
}
