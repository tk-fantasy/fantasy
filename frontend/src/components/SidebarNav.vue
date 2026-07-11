<script setup>
import { computed, ref, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuth } from '../composables/useAuth'
import { LS_USER, SS_CHAT_SESSION } from '../utils/constants'

const route = useRoute()
const router = useRouter()
const { user, isAuthenticated, logout } = useAuth()

const emit = defineEmits(['open-advanced'])

const homeName = ref('我的家')
const ownerName = ref('')
const showUserMenu = ref(false)
const users = ref([])
const switchingUser = ref(false)
// 切换用户时的密码确认子状态
const pendingSwitch = ref(null) // { username, displayName }
const switchPassword = ref('')
const switchError = ref('')

// 从 API 加载家庭信息
async function loadHomeInfo() {
  try {
    const res = await fetch('/api/home/info')
    if (!res.ok) return
    const json = await res.json()
    const data = json.data || {}
    homeName.value = data.home_name || '我的家'
    ownerName.value = data.owner_name || ''
  } catch (e) {
    console.error('Failed to load home info:', e)
  }
}

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
  // 未登录时，跳转到登录页
  if (!isAuthenticated.value) {
    showUserMenu.value = false
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
      // 清空 sessionStorage 中的会话 ID（不同用户的对话隔离）
      sessionStorage.removeItem(SS_CHAT_SESSION)
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

// 登出
function handleLogout() {
  // 清空当前会话 ID，下一个用户进来是空白页面
  sessionStorage.removeItem(SS_CHAT_SESSION)
  logout()
  router.push('/login')
}

// 跳转到注册页
function goToRegister() {
  showUserMenu.value = false
  router.push('/login?mode=register')
}

// 跳转到登录页
function goToLogin() {
  showUserMenu.value = false
  router.push('/login')
}

// 监听设置变更事件
window.addEventListener('home-info-changed', () => {
  loadHomeInfo()
})

const navItems = [
  { path: '/chat', icon: '&#128172;', label: '管家' },
  { path: '/settings', icon: '&#9881;', label: '设置' },
  { path: '/advanced', icon: '&#128295;', label: '高级', modal: true },
]

const activePath = computed(() => route.path)

function goTo(path) {
  router.push(path)
}

// 高级设置改为弹窗触发，不再路由跳转
function onNavClick(item) {
  if (item.modal) {
    emit('open-advanced')
  } else {
    goTo(item.path)
  }
}

const timeStr = ref('')
const dateStr = ref('')
let timer

function updateClock() {
  const now = new Date()
  timeStr.value = now.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  dateStr.value = now.toLocaleDateString('zh-CN', { weekday: 'short', month: 'numeric', day: 'numeric' })
}

// 点击外部关闭菜单
function handleClickOutside(event) {
  const menu = document.querySelector('.user-menu-container')
  if (menu && !menu.contains(event.target)) {
    showUserMenu.value = false
  }
}

onMounted(() => {
  updateClock()
  timer = setInterval(updateClock, 1000)
  loadHomeInfo()
  loadUsers()
  document.addEventListener('click', handleClickOutside)
})

onUnmounted(() => {
  clearInterval(timer)
  document.removeEventListener('click', handleClickOutside)
})

// 路由变化或用户变化时重新加载用户列表（注册/登录后返回）
watch(() => route.path, () => {
  loadUsers()
})

watch(user, () => {
  loadUsers()
})
</script>

<template>
  <aside class="sidebar">
    <div class="sidebar-brand" @click="goTo('/halist')">
      <div class="sidebar-logo">&#10022;</div>
      <span class="sidebar-title">Aether</span>
    </div>
    <nav class="sidebar-nav">
      <button
        v-for="item in navItems"
        :key="item.path"
        class="sidebar-item"
        :class="{ active: !item.modal && activePath === item.path }"
        @click="onNavClick(item)"
      >
        <span class="sidebar-item-icon" v-html="item.icon"></span>
        <span class="sidebar-item-label">{{ item.label }}</span>
      </button>
    </nav>
    <div class="sidebar-footer">
      <div class="sidebar-clock">
        <span class="sidebar-time">{{ timeStr }}</span>
        <span class="sidebar-date">{{ dateStr }}</span>
      </div>

      <!-- 未登录时显示登录按钮 -->
      <div v-if="!isAuthenticated" class="login-btn-container">
        <button class="login-btn" @click.stop="showUserMenu = !showUserMenu">
          <span class="login-btn-icon">&#128274;</span>
          <span class="login-btn-text">登录</span>
        </button>

        <!-- 登录下拉菜单 -->
        <div v-if="showUserMenu" class="user-dropdown">
          <div class="user-dropdown-header">选择账号</div>

          <div class="user-list">
            <div
              v-for="u in users"
              :key="u.id"
              class="user-item"
              @click.stop="promptSwitchUser(u)"
            >
              <div class="user-item-avatar">{{ (u.display_name || u.username).charAt(0).toUpperCase() }}</div>
              <div class="user-item-info">
                <div class="user-item-name">{{ u.display_name || u.username }}</div>
                <div class="user-item-username">@{{ u.username }}</div>
              </div>
            </div>
          </div>

          <div v-if="users.length === 0" class="no-users-hint">
            暂无账号，请先注册
          </div>

          <div class="user-dropdown-divider"></div>

          <div class="user-dropdown-actions">
            <button class="user-action-btn" @click.stop="goToLogin">
              <span>&rarr;</span> 登录账号
            </button>
            <button class="user-action-btn" @click.stop="goToRegister">
              <span>+</span> 注册新账号
            </button>
          </div>
        </div>
      </div>

      <!-- 已登录时显示用户菜单 -->
      <div v-else class="user-menu-container">
        <div class="sidebar-user" @click.stop="showUserMenu = !showUserMenu">
          <div class="sidebar-avatar">{{ (ownerName || user?.username || 'U').charAt(0).toUpperCase() }}</div>
          <div class="sidebar-user-info">
            <div class="sidebar-user-name">{{ ownerName || user?.username || '用户' }}</div>
            <div class="sidebar-user-status">&#9679; 在线</div>
          </div>
          <div class="sidebar-user-menu-icon">&#8943;</div>
        </div>

        <!-- 用户菜单下拉框 — 可切换到其他用户（需密码确认）或登出 -->
        <div v-if="showUserMenu" class="user-dropdown">
          <div class="user-dropdown-header">{{ user?.username }}</div>

          <div class="user-list">
            <div
              v-for="u in users"
              :key="u.id"
              class="user-item"
              :class="{ 'is-current': u.username === user?.username, switching: switchingUser }"
              @click.stop="promptSwitchUser(u)"
            >
              <div class="user-item-avatar">{{ (u.display_name || u.username).charAt(0).toUpperCase() }}</div>
              <div class="user-item-info">
                <div class="user-item-name">{{ u.display_name || u.username }}</div>
                <div class="user-item-username">@{{ u.username }}</div>
              </div>
              <span v-if="u.username === user?.username" class="user-item-current">当前</span>
            </div>
          </div>

          <div v-if="users.length === 0" class="no-users-hint">
            暂无其他账号
          </div>

          <div class="user-dropdown-divider"></div>

          <div class="user-dropdown-actions">
            <button class="user-action-btn logout" @click.stop="handleLogout">
              <span>&rarr;</span> 退出登录
            </button>
          </div>
        </div>

        <!-- 切换用户密码确认弹层 -->
        <div v-if="pendingSwitch" class="switch-confirm" @click.self="cancelSwitch">
          <div class="switch-confirm-card">
            <div class="switch-confirm-title">切换到 {{ pendingSwitch.displayName }}</div>
            <div class="switch-confirm-hint">请输入该账号的密码以确认切换</div>
            <input
              v-model="switchPassword"
              type="password"
              class="switch-confirm-input"
              placeholder="密码"
              autocomplete="current-password"
              @keyup.enter="confirmSwitchUser"
            />
            <div v-if="switchError" class="switch-confirm-error">{{ switchError }}</div>
            <div class="switch-confirm-actions">
              <button class="switch-confirm-btn cancel" @click="cancelSwitch" :disabled="switchingUser">取消</button>
              <button class="switch-confirm-btn confirm" @click="confirmSwitchUser" :disabled="switchingUser || !switchPassword">
                {{ switchingUser ? '切换中…' : '确认切换' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  position: fixed;
  top: 0;
  left: 0;
  bottom: 0;
  width: var(--sidebar-width);
  background: var(--sidebar-bg);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border-right: 1px solid var(--color-border);
  display: flex;
  flex-direction: column;
  z-index: 200;
  overflow: hidden;
}

.sidebar::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  z-index: 0;
  pointer-events: none;
  width: 1px;
  height: 1px;
  border-radius: 50%;
  will-change: transform;
  box-shadow:
    34px 719px 0 1px rgba(255,255,255,0.11),
    51px 390px 0 1px rgba(255,255,255,0.12),
    23px 442px 0 2px rgba(255,255,255,0.06),
    92px 304px 0 1px rgba(255,255,255,0.1),
    48px 449px 0 1px rgba(255,255,255,0.11),
    165px 492px 0 2px rgba(255,255,255,0.15),
    141px 745px 0 2px rgba(255,255,255,0.1),
    5px 766px 0 1px rgba(255,255,255,0.04),
    74px 615px 0 1px rgba(255,255,255,0.15),
    141px 657px 0 1px rgba(255,255,255,0.16),
    75px 446px 0 2px rgba(255,255,255,0.11),
    184px 353px 0 2px rgba(255,255,255,0.03),
    113px 548px 0 1px rgba(255,255,255,0.12),
    34px 584px 0 1px rgba(255,255,255,0.09),
    25px 279px 0 2px rgba(255,255,255,0.17),
    7px 377px 0 2px rgba(255,255,255,0.16),
    102px 397px 0 1px rgba(255,255,255,0.15),
    117px 80px 0 1px rgba(255,255,255,0.16),
    142px 445px 0 2px rgba(255,255,255,0.07),
    28px 633px 0 2px rgba(255,255,255,0.07),
    182px 191px 0 2px rgba(255,255,255,0.11),
    72px 441px 0 1px rgba(255,255,255,0.11),
    75px 211px 0 1px rgba(255,255,255,0.06),
    180px 438px 0 1px rgba(255,255,255,0.17),
    135px 302px 0 1px rgba(255,255,255,0.1),
    129px 276px 0 1px rgba(255,255,255,0.13),
    167px 188px 0 1px rgba(255,255,255,0.15),
    98px 116px 0 2px rgba(255,255,255,0.09),
    22px 515px 0 1px rgba(255,255,255,0.08),
    184px 453px 0 1px rgba(255,255,255,0.14),
    186px 142px 0 2px rgba(255,255,255,0.07),
    170px 382px 0 2px rgba(255,255,255,0.1),
    128px 286px 0 1px rgba(255,255,255,0.12),
    65px 645px 0 1px rgba(255,255,255,0.12),
    117px 58px 0 2px rgba(255,255,255,0.1),
    156px 755px 0 2px rgba(255,255,255,0.05),
    169px 727px 0 2px rgba(255,255,255,0.07),
    42px 118px 0 2px rgba(255,255,255,0.17),
    123px 425px 0 1px rgba(255,255,255,0.1),
    13px 255px 0 1px rgba(255,255,255,0.12),
    151px 476px 0 2px rgba(255,255,255,0.17),
    111px 517px 0 2px rgba(255,255,255,0.18),
    13px 610px 0 1px rgba(255,255,255,0.06),
    28px 504px 0 1px rgba(255,255,255,0.05),
    13px 59px 0 1px rgba(255,255,255,0.1),
    46px 167px 0 1px rgba(255,255,255,0.17),
    83px 374px 0 1px rgba(255,255,255,0.05),
    69px 110px 0 2px rgba(255,255,255,0.11),
    94px 73px 0 2px rgba(255,255,255,0.08),
    8px 4px 0 2px rgba(255,255,255,0.13),
    187px 776px 0 1px rgba(255,255,255,0.06),
    189px 66px 0 1px rgba(255,255,255,0.18),
    79px 394px 0 1px rgba(255,255,255,0.09),
    169px 520px 0 1px rgba(255,255,255,0.05),
    127px 347px 0 2px rgba(255,255,255,0.14),
    108px 157px 0 1px rgba(255,255,255,0.17),
    52px 711px 0 2px rgba(255,255,255,0.09),
    60px 753px 0 2px rgba(255,255,255,0.17),
    81px 204px 0 2px rgba(255,255,255,0.18),
    195px 427px 0 2px rgba(255,255,255,0.11);
  animation: particlesFloat 12s ease-in-out infinite;
}

@keyframes particlesFloat {
  0%, 100% {
    transform: translateY(-50px);
  }
  50% {
    transform: translateY(50px);
  }
}

.light-mode .sidebar::before {
  box-shadow:
    34px 719px 0 1px rgba(0,0,0,0.08),
    51px 390px 0 1px rgba(0,0,0,0.09),
    23px 442px 0 2px rgba(0,0,0,0.04),
    92px 304px 0 1px rgba(0,0,0,0.07),
    48px 449px 0 1px rgba(0,0,0,0.08),
    165px 492px 0 2px rgba(0,0,0,0.11),
    141px 745px 0 2px rgba(0,0,0,0.07),
    5px 766px 0 1px rgba(0,0,0,0.03),
    74px 615px 0 1px rgba(0,0,0,0.11),
    141px 657px 0 1px rgba(0,0,0,0.12),
    75px 446px 0 2px rgba(0,0,0,0.08),
    184px 353px 0 2px rgba(0,0,0,0.02),
    113px 548px 0 1px rgba(0,0,0,0.09),
    34px 584px 0 1px rgba(0,0,0,0.07),
    25px 279px 0 2px rgba(0,0,0,0.13),
    7px 377px 0 2px rgba(0,0,0,0.12),
    102px 397px 0 1px rgba(0,0,0,0.11),
    117px 80px 0 1px rgba(0,0,0,0.12),
    142px 445px 0 2px rgba(0,0,0,0.05),
    28px 633px 0 2px rgba(0,0,0,0.05),
    182px 191px 0 2px rgba(0,0,0,0.08),
    72px 441px 0 1px rgba(0,0,0,0.08),
    75px 211px 0 1px rgba(0,0,0,0.04),
    180px 438px 0 1px rgba(0,0,0,0.13),
    135px 302px 0 1px rgba(0,0,0,0.07),
    129px 276px 0 1px rgba(0,0,0,0.1),
    167px 188px 0 1px rgba(0,0,0,0.11),
    98px 116px 0 2px rgba(0,0,0,0.07),
    22px 515px 0 1px rgba(0,0,0,0.06),
    184px 453px 0 1px rgba(0,0,0,0.1),
    186px 142px 0 2px rgba(0,0,0,0.05),
    170px 382px 0 2px rgba(0,0,0,0.07),
    128px 286px 0 1px rgba(0,0,0,0.09),
    65px 645px 0 1px rgba(0,0,0,0.09),
    117px 58px 0 2px rgba(0,0,0,0.07),
    156px 755px 0 2px rgba(0,0,0,0.04),
    169px 727px 0 2px rgba(0,0,0,0.05),
    42px 118px 0 2px rgba(0,0,0,0.13),
    123px 425px 0 1px rgba(0,0,0,0.07),
    13px 255px 0 1px rgba(0,0,0,0.09),
    151px 476px 0 2px rgba(0,0,0,0.13),
    111px 517px 0 2px rgba(0,0,0,0.14),
    13px 610px 0 1px rgba(0,0,0,0.04),
    28px 504px 0 1px rgba(0,0,0,0.04),
    13px 59px 0 1px rgba(0,0,0,0.07),
    46px 167px 0 1px rgba(0,0,0,0.13),
    83px 374px 0 1px rgba(0,0,0,0.04),
    69px 110px 0 2px rgba(0,0,0,0.08),
    94px 73px 0 2px rgba(0,0,0,0.06),
    8px 4px 0 2px rgba(0,0,0,0.1),
    187px 776px 0 1px rgba(0,0,0,0.04),
    189px 66px 0 1px rgba(0,0,0,0.14),
    79px 394px 0 1px rgba(0,0,0,0.07),
    169px 520px 0 1px rgba(0,0,0,0.04),
    127px 347px 0 2px rgba(0,0,0,0.1),
    108px 157px 0 1px rgba(0,0,0,0.13),
    52px 711px 0 2px rgba(0,0,0,0.07),
    60px 753px 0 2px rgba(0,0,0,0.13),
    81px 204px 0 2px rgba(0,0,0,0.14),
    195px 427px 0 2px rgba(0,0,0,0.08);
}

.sidebar::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 100%;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(ellipse 120% 60% at 30% 100%, rgba(74, 124, 112, 0.2) 0%, transparent 60%),
    radial-gradient(ellipse 100% 50% at 70% 100%, rgba(232, 168, 124, 0.12) 0%, transparent 55%),
    radial-gradient(ellipse 80% 40% at 50% 100%, rgba(183, 128, 176, 0.1) 0%, transparent 50%);
  animation: sidebarGlow 8s ease-in-out infinite;
}

@keyframes sidebarGlow {
  0%, 100% { opacity: 0.5; }
  50% { opacity: 1; }
}

.sidebar-brand {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  padding: var(--space-14) var(--space-14) var(--space-20);
  cursor: pointer;
  position: relative;
  z-index: 1;
}

.sidebar-logo {
  width: 32px;
  height: 32px;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  font-size: 16px;
  flex-shrink: 0;
  box-shadow: 0 0 12px 4px rgba(74, 124, 112, 0.15);
  animation: logoBreath 3s var(--ease-in-out) infinite;
}

@keyframes logoBreath {
  0%, 100% {
    box-shadow: 0 0 12px 4px rgba(74, 124, 112, 0.15);
    transform: scale(1);
  }
  50% {
    box-shadow: 0 0 20px 8px rgba(74, 124, 112, 0.25);
    transform: scale(1.04);
  }
}

.sidebar-title {
  font-size: 17px;
  font-weight: var(--weight-bold);
  letter-spacing: -0.3px;
  color: var(--color-text);
  text-shadow: 0 1px 3px rgba(0, 0, 0, 0.5);
}

.sidebar-nav {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: 0 var(--space-6);
  position: relative;
  z-index: 1;
}

.sidebar-item {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-5) var(--space-8);
  border-radius: var(--radius-lg);
  border: none;
  background: transparent;
  color: var(--color-text-tertiary);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
  text-align: left;
  width: 100%;
  position: relative;
  overflow: hidden;
}

.sidebar-item::before {
  content: '';
  position: absolute;
  left: 8px;
  top: 50%;
  transform: translateY(-50%);
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--color-primary);
  opacity: 0;
  transition: all var(--duration-normal) var(--ease-out);
  pointer-events: none;
}

.sidebar-item:hover::before {
  opacity: 0.5;
}

.sidebar-item.active::before {
  opacity: 1;
  width: 6px;
  height: 6px;
  box-shadow: 0 0 12px 4px rgba(74, 124, 112, 0.3);
}

.sidebar-item:hover {
  background: var(--color-surface-hover);
  color: var(--color-text-secondary);
}

.sidebar-item.active {
  background: rgba(74, 124, 112, 0.12);
  color: var(--color-primary);
  font-weight: var(--weight-semibold);
}

.sidebar-item-icon {
  font-size: var(--text-xl);
  width: 24px;
  text-align: center;
  flex-shrink: 0;
}

.sidebar-footer {
  padding: var(--space-10) var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
  position: relative;
  z-index: 1;
}

.sidebar-clock {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-1);
  padding: var(--space-2) 0;
}

.sidebar-time {
  font-size: var(--text-xl);
  font-weight: var(--weight-bold);
  color: var(--color-text);
  letter-spacing: 1px;
  font-variant-numeric: tabular-nums;
}

.sidebar-date {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  letter-spacing: 0.5px;
}

.sidebar-user {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  padding: var(--space-4) var(--space-8);
  border-radius: var(--radius-lg);
}

.sidebar-user:hover {
  background: var(--color-surface);
}

.sidebar-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  flex-shrink: 0;
}

.sidebar-user-name {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.sidebar-user-status {
  font-size: var(--text-xs);
  color: var(--color-success);
}

.sidebar-user-menu-icon {
  margin-left: auto;
  font-size: var(--text-lg);
  color: var(--color-text-muted);
  cursor: pointer;
}

/* User Menu Container */
.user-menu-container {
  position: relative;
}

/* User Dropdown */
.user-dropdown {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  margin-bottom: var(--space-8);
  background: #1a1a22;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  box-shadow: 0 -4px 24px rgba(0, 0, 0, 0.45), 0 0 12px rgba(74, 124, 112, 0.08);
  z-index: 100;
  overflow: hidden;
  animation: dropdownFadeIn 0.15s ease-out;
}

.user-dropdown::before {
  content: '';
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background:
    radial-gradient(ellipse 60% 50% at 25% 30%, rgba(45, 90, 78, 0.3) 0%, transparent 50%),
    radial-gradient(ellipse 50% 45% at 70% 25%, rgba(74, 124, 112, 0.25) 0%, transparent 45%),
    radial-gradient(ellipse 55% 50% at 45% 70%, rgba(30, 60, 110, 0.25) 0%, transparent 45%),
    radial-gradient(ellipse 50% 45% at 60% 50%, rgba(232, 168, 124, 0.3) 0%, transparent 45%),
    radial-gradient(ellipse 45% 40% at 35% 55%, rgba(183, 128, 176, 0.25) 0%, transparent 40%);
  background-size: 300% 300%, 300% 300%, 300% 300%, 300% 300%, 300% 300%;
  animation: sidebarDropdownFlow 12s cubic-bezier(0.45, 0, 0.55, 1) infinite;
  opacity: 0.55;
}

@keyframes sidebarDropdownFlow {
  0%   { background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%; }
  25%  { background-position: 40% 35%, 55% 50%, 60% 45%, 30% 60%, 50% 35%; }
  50%  { background-position: 25% 15%, 65% 55%, 45% 65%, 55% 30%, 20% 50%; }
  75%  { background-position: 50% 30%, 45% 20%, 30% 55%, 65% 45%, 40% 55%; }
  100% { background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%; }
}

.light-mode .user-dropdown {
  background: #ffffff;
  border-color: rgba(0, 0, 0, 0.1);
  box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.12);
}

.light-mode .user-dropdown::before {
  opacity: 0.3;
}

.user-dropdown > * {
  position: relative;
  z-index: 1;
}

@keyframes dropdownFadeIn {
  from {
    opacity: 0;
    transform: translateY(8px);
  }
  to {
    opacity: 1;
    transform: translateY(0);
  }
}

.user-dropdown-header {
  padding: var(--space-8) var(--space-12);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.user-list {
  max-height: 200px;
  overflow-y: auto;
}

.user-item {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  padding: var(--space-8) var(--space-12);
  cursor: pointer;
  transition: background 0.15s;
}

.user-item:hover {
  background: var(--color-surface-hover);
}

.user-item.active {
  background: var(--color-primary-light);
}

.user-item.switching {
  opacity: 0.6;
}

.user-item-avatar {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  flex-shrink: 0;
}

.user-item-info {
  flex: 1;
  min-width: 0;
}

.user-item-name {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.user-item-username {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.user-item-current {
  font-size: var(--text-xs);
  color: var(--color-primary);
  font-weight: var(--weight-medium);
}

.user-item.is-current {
  cursor: default;
  opacity: 0.7;
}

.user-item.is-current:hover {
  background: transparent;
}

.user-item-loading {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.user-dropdown-divider {
  height: 1px;
  background: var(--color-border);
  margin: var(--space-4) 0;
}

.user-dropdown-actions {
  padding: var(--space-4) var(--space-8);
}

.user-action-btn {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  width: 100%;
  padding: var(--space-6) var(--space-8);
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  border-radius: var(--radius-md);
  transition: all 0.15s;
}

.user-action-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.user-action-btn.logout:hover {
  background: var(--color-danger-bg);
  color: var(--color-danger);
}

.user-action-btn span {
  font-size: var(--text-base);
}

/* Login Button Styles */
.login-btn-container {
  position: relative;
}

.login-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-4);
  width: 100%;
  padding: var(--space-5) var(--space-8);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.login-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-primary);
  border-color: var(--color-primary);
}

.login-btn-icon {
  font-size: var(--text-base);
}

.no-users-hint {
  padding: var(--space-12) var(--space-8);
  text-align: center;
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}

/* 切换用户密码确认弹层 */
.switch-confirm {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  animation: dropdownFadeIn 0.15s ease-out;
}

.switch-confirm-card {
  width: 320px;
  max-width: calc(100vw - 32px);
  background: #1a1a22;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5);
  padding: var(--space-16);
  position: relative;
  z-index: 1;
}

.light-mode .switch-confirm-card {
  background: #ffffff;
  border-color: rgba(0, 0, 0, 0.1);
}

.switch-confirm-title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin-bottom: var(--space-2);
}

.switch-confirm-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin-bottom: var(--space-10);
}

.switch-confirm-input {
  width: 100%;
  padding: var(--space-6) var(--space-8);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: var(--text-sm);
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}

.switch-confirm-input:focus {
  border-color: var(--color-primary);
}

.switch-confirm-error {
  margin-top: var(--space-4);
  font-size: var(--text-xs);
  color: var(--color-danger);
}

.switch-confirm-actions {
  display: flex;
  gap: var(--space-6);
  margin-top: var(--space-12);
}

.switch-confirm-btn {
  flex: 1;
  padding: var(--space-6) var(--space-8);
  border: none;
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: all 0.15s;
}

.switch-confirm-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.switch-confirm-btn.cancel {
  background: var(--color-surface-hover);
  color: var(--color-text-secondary);
}

.switch-confirm-btn.cancel:not(:disabled):hover {
  background: var(--color-border);
}

.switch-confirm-btn.confirm {
  background: var(--color-primary);
  color: #fff;
}

.switch-confirm-btn.confirm:not(:disabled):hover {
  background: var(--color-primary-dark);
}

@media (max-width: 768px) {
  .sidebar {
    width: var(--sidebar-width-collapsed);
  }

  .sidebar-title,
  .sidebar-item-label,
  .sidebar-user-info,
  .sidebar-clock,
  .sidebar::after,
  .login-btn-text {
    display: none;
  }

  .sidebar-brand {
    justify-content: center;
    padding: var(--space-10) 0;
  }

  .sidebar-item {
    justify-content: center;
    padding: var(--space-5) 0;
    margin: 0;
  }

  .sidebar-item::before {
    display: none;
  }

  .sidebar-footer {
    padding: var(--space-6) 0;
  }

  .sidebar-user {
    justify-content: center;
    padding: var(--space-4);
  }

  .login-btn {
    padding: var(--space-4);
    justify-content: center;
  }
}
</style>
