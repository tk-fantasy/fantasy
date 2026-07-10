import { createRouter, createWebHistory } from 'vue-router'
import { useAuth } from '../composables/useAuth'

const routes = [
  { path: '/', redirect: '/landing' },
  {
    path: '/login',
    name: 'Login',
    component: () => import('../views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/landing',
    name: 'Landing',
    component: () => import('../views/LandingView.vue'),
  },
  {
    path: '/loading',
    name: 'Loading',
    component: () => import('../views/LoadingView.vue'),
  },
  {
    path: '/setup',
    name: 'Setup',
    component: () => import('../views/SetupWizardView.vue'),
  },
  {
    path: '/chat',
    name: 'Chat',
    component: () => import('../views/ChatView.vue'),
  },
  {
    path: '/settings',
    name: 'Settings',
    component: () => import('../views/SettingsView.vue'),
  },
  {
    path: '/keys',
    name: 'Keys',
    component: () => import('../views/KeysView.vue'),
  },
  {
    path: '/models',
    name: 'Models',
    component: () => import('../views/ModelsView.vue'),
  },
  {
    path: '/ha',
    name: 'HA',
    component: () => import('../views/HAView.vue'),
  },
  {
    path: '/focus',
    name: 'Focus',
    component: () => import('../views/FocusView.vue'),
  },
  {
    path: '/task',
    name: 'Task',
    component: () => import('../views/TaskView.vue'),
  },
  {
    path: '/scheduled',
    name: 'ScheduledTasks',
    component: () => import('../views/ScheduledTasksView.vue'),
  },
  {
    path: '/halist',
    name: 'HAList',
    component: () => import('../views/HAListView.vue'),
  },
  {
    path: '/sessions',
    name: 'Sessions',
    component: () => import('../views/SessionsView.vue'),
  },
  {
    path: '/unique',
    name: 'Unique',
    component: () => import('../views/UniqueView.vue'),
  },
  {
    path: '/doc',
    name: 'DocChat',
    component: () => import('../views/DocChat.vue'),
  },
  {
    path: '/sg',
    name: 'SemanticGraph',
    component: () => import('../views/SgView.vue'),
  },
  {
    path: '/doc/KGraph',
    name: 'KGraph',
    component: () => import('../views/KGraphView.vue'),
  },
  {
    path: '/advanced',
    name: 'Advanced',
    component: () => import('../views/AdvancedView.vue'),
  },
  {
    path: '/monitor',
    name: 'Monitor',
    component: () => import('../views/MonitorView.vue'),
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 后端就绪标志：LoadingView 轮询 /api/health 通过后置 true。
// 已登录用户刷新页面时 router 会直接放行，绕过 Loading 的就绪检测，
// 业务组件在后端 startup 期发 /api/* → 502。用此标志做全局门控。
let backendReady = false
export function markBackendReady() { backendReady = true }
export function isBackendReady() { return backendReady }

// 路由守卫：未登录重定向到 /login；后端未就绪重定向到 /loading
router.beforeEach((to, from, next) => {
  const { isAuthenticated } = useAuth()

  // 公开路由（不需要登录）
  const publicRoutes = ['Login']
  if (publicRoutes.includes(to.name)) {
    // 已登录用户访问登录页，跳转到首页
    if (isAuthenticated.value && to.name === 'Login') {
      return next('/landing')
    }
    return next()
  }

  // 未登录，重定向到登录页
  if (!isAuthenticated.value) {
    return next({ name: 'Login', query: { redirect: to.fullPath } })
  }

  // 已登录但后端尚未就绪：强制走 Loading 轮询就绪后再放行，
  // 避免业务组件在后端 startup 空窗期发 /api/* 触发 502。
  // Loading 自身、Setup 等不需此后端就绪（Loading 就是来确认就绪的）。
  if (!backendReady && !['Loading', 'Setup', 'Landing'].includes(to.name)) {
    return next({ name: 'Loading', query: { redirect: to.fullPath } })
  }

  next()
})

export default router
