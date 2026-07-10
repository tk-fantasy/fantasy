<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../composables/useAuth'
import { markBackendReady } from '../router'

const router = useRouter()
const { user } = useAuth()

const loadingText = ref('')
const statusText = ref('')
const isReady = ref(false)
const hasError = ref(false)
const showRetry = ref(false)

// 问候语
function getGreeting(ownerName) {
  const hour = new Date().getHours()
  let greeting = ''

  if (hour >= 5 && hour < 12) {
    greeting = '早上好'
  } else if (hour >= 12 && hour < 14) {
    greeting = '中午好'
  } else if (hour >= 14 && hour < 18) {
    greeting = '下午好'
  } else {
    greeting = '晚上好'
  }

  return ownerName ? `${greeting}，${ownerName}` : greeting
}

// 逐字显示效果
async function typeText(text, target, delay = 80) {
  target.value = ''
  for (let i = 0; i < text.length; i++) {
    target.value += text[i]
    await new Promise(resolve => setTimeout(resolve, delay))
  }
}

// 时间估算的兜底文案：仅在进度端口不可达（如生产环境 / 端口被占）时使用
const STARTUP_PHASES = [
  { until: 3, text: '正在初始化后端服务...' },
  { until: 10, text: '正在加载 AI 模型与向量索引...' },
  { until: 18, text: '正在注册工具与构建智能体...' },
  { until: 26, text: '正在连接摄像头与智能家居...' },
  { until: Infinity, text: '正在做最后准备...' },
]

function getStartupPhase(elapsedSec) {
  for (const p of STARTUP_PHASES) {
    if (elapsedSec < p.until) return p.text
  }
  return STARTUP_PHASES[STARTUP_PHASES.length - 1].text
}

// 拉取后端真实启动阶段（冷启动期间主端口 8010 未就绪，由轻量进度端口 8011 提供）
async function fetchStartupStage() {
  try {
    const res = await fetch('/api/startup-progress')
    if (res.ok) {
      const data = await res.json()
      if (data && data.stage) return data.stage
    }
  } catch (e) {
    // 进度端口尚未就绪，返回 null 由调用方回退到时间估算
  }
  return null
}

// 轮询等待后端就绪：展示真实加载阶段，主端口 /api/health 通过即就绪
async function waitForBackend(timeoutMs = 60000, intervalMs = 1500) {
  const start = Date.now()
  while (Date.now() - start < timeoutMs) {
    const elapsed = Math.floor((Date.now() - start) / 1000)
    const stage = await fetchStartupStage()
    statusText.value = stage
      ? `${stage}（已等待 ${elapsed}s）`
      : `${getStartupPhase(elapsed)}（已等待 ${elapsed}s）`
    // 主端口健康检查才是真正的就绪判据：
    // - fetch 抛异常（连接拒绝）或 502/503/504（代理层错误）→ 后端尚未监听
    // - 任何 < 500 的响应（含 401 未认证）→ 后端 HTTP 服务已就绪，
    //   认证问题留给后续 checkServices 处理（401 → 跳登录）
    try {
      const res = await fetch('/api/health')
      if (res.status < 500) return true
    } catch (e) {
      // 后端尚未就绪（连接拒绝 / 502），继续等待
    }
    await new Promise(resolve => setTimeout(resolve, intervalMs))
  }
  return false
}

// 检查服务状态
async function checkServices() {
  hasError.value = false
  showRetry.value = false
  statusText.value = '正在检查服务...'

  // 1. 等待后端冷启动完成
  const backendReady = await waitForBackend()
  if (!backendReady) {
    hasError.value = true
    showRetry.value = true
    statusText.value = '后端启动超时，请确认后端已运行'
    return false
  }

  // 2. 配置状态（需后端在线）
  try {
    const setupRes = await fetch('/api/setup/status')

    // 401：拦截器已尝试刷新 token；若仍 401 说明 refresh 也失败，
    // 拦截器会负责清理状态并跳转登录，这里只需中止后续流程
    if (setupRes.status === 401) {
      statusText.value = '登录已过期，正在跳转登录...'
      return false
    }

    const setupJson = await setupRes.json()
    const setupData = setupJson.data

    // 如果配置不完整，跳转到引导页
    if (!setupData.setup_complete) {
      statusText.value = '需要完成初始配置...'
      await new Promise(resolve => setTimeout(resolve, 1000))
      router.push('/setup')
      return false
    }
  } catch (e) {
    console.error('Failed to check setup status:', e)
    hasError.value = true
    showRetry.value = true
    statusText.value = '获取配置状态失败，请重试'
    return false
  }

  // 3. 最终健康确认
  statusText.value = '正在连接 后端服务...'
  try {
    const ok = await fetch('/api/health').then(r => r.ok)
    if (!ok) throw new Error('health check not ok')
  } catch (e) {
    hasError.value = true
    showRetry.value = true
    statusText.value = '后端服务连接失败，请重试'
    return false
  }

  // 后端就绪且服务检查通过：置全局标志，router 守卫此后放行已登录用户
  markBackendReady()
  return true
}

// 失败后手动重试
async function retry() {
  const allOk = await checkServices()
  if (allOk) {
    statusText.value = '一切就绪'
    await new Promise(resolve => setTimeout(resolve, 800))
    isReady.value = true
    setTimeout(() => {
      router.push('/chat')
    }, 500)
  }
}

onMounted(async () => {
  // 获取用户称呼
  let ownerName = ''
  try {
    const homeRes = await fetch('/api/home/info')
    if (homeRes.ok) {
      const homeJson = await homeRes.json()
      ownerName = homeJson.data?.owner_name || ''
    }
  } catch (e) {
    console.error('Failed to load home info:', e)
  }
  
  // 显示问候语（逐字）
  await typeText(getGreeting(ownerName), loadingText, 100)
  await new Promise(resolve => setTimeout(resolve, 500))

  // 检查服务
  const allOk = await checkServices()

  if (allOk) {
    statusText.value = '一切就绪'
    await new Promise(resolve => setTimeout(resolve, 800))
    isReady.value = true
    // 跳转到聊天页
    setTimeout(() => {
      router.push('/chat')
    }, 500)
  }
})
</script>

<template>
  <div class="loading-page" :class="{ ready: isReady }">
    <div class="loading-content">
      <div class="greeting">
        <span
          v-for="(char, index) in loadingText"
          :key="index"
          class="greeting-char"
          :style="{ animationDelay: `${index * 0.1}s` }"
        >
          {{ char === ' ' ? '\u00A0' : char }}
        </span>
      </div>

      <div class="status">
        <div class="status-dot" :class="{ error: hasError }"></div>
        <span class="status-text">{{ statusText }}</span>
      </div>

      <div class="loader">
        <div class="loader-bar"></div>
      </div>

      <button v-if="showRetry" class="retry-btn" @click="retry">重试</button>
    </div>
  </div>
</template>

<style scoped>
.loading-page {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg);
  font-family: var(--font-family);
  transition: opacity 0.5s ease;
}

.loading-page.ready {
  opacity: 0;
}

.loading-content {
  text-align: center;
}

.greeting {
  font-size: var(--text-4xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin-bottom: var(--space-24);
  min-height: 1.2em;
}

.greeting-char {
  display: inline-block;
  animation: bounce 0.6s ease infinite;
}

@keyframes bounce {
  0%, 100% {
    transform: translateY(0);
  }
  50% {
    transform: translateY(-8px);
  }
}

.status {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-8);
  margin-bottom: var(--space-20);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: pulse 1.5s ease-in-out infinite;
}

.status-dot.error {
  background: var(--color-danger);
}

@keyframes pulse {
  0%, 100% {
    opacity: 1;
    transform: scale(1);
  }
  50% {
    opacity: 0.5;
    transform: scale(0.8);
  }
}

.status-text {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.loader {
  width: 200px;
  height: 2px;
  background: var(--color-surface);
  border-radius: 1px;
  overflow: hidden;
  margin: 0 auto;
}

.loader-bar {
  width: 30%;
  height: 100%;
  background: var(--color-primary);
  border-radius: 1px;
  animation: loading 1.5s ease-in-out infinite;
}

@keyframes loading {
  0% {
    transform: translateX(-100%);
  }
  100% {
    transform: translateX(400%);
  }
}

.retry-btn {
  margin-top: var(--space-20);
  padding: 8px 24px;
  font-size: var(--text-sm);
  color: var(--color-primary);
  background: transparent;
  border: 1px solid var(--color-primary);
  border-radius: 999px;
  cursor: pointer;
  transition: background 0.2s ease, color 0.2s ease;
}

.retry-btn:hover {
  background: var(--color-primary);
  color: var(--color-bg);
}

@media (max-width: 480px) {
  .greeting {
    font-size: var(--text-2xl);
  }
}
</style>
