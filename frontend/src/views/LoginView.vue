<script setup>
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuth } from '../composables/useAuth'

const router = useRouter()
const route = useRoute()
const { login, register } = useAuth()

const isRegister = ref(false)
const username = ref('')
const password = ref('')
const displayName = ref('')
const error = ref('')
const loading = ref(false)

// 检查 URL 参数是否指定了注册模式
onMounted(() => {
  if (route.query.mode === 'register') {
    isRegister.value = true
  }
})

async function handleSubmit() {
  error.value = ''
  loading.value = true

  try {
    if (isRegister.value) {
      await register(username.value, password.value, displayName.value)
    } else {
      await login(username.value, password.value)
    }
    router.push('/loading')
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-bg">
      <div class="bg-gradient"></div>
      <div class="bg-grid"></div>
    </div>

    <div class="login-card">
      <div class="card-header">
        <div class="brand">
          <div class="brand-dot"></div>
          <div class="brand-line"></div>
        </div>
        <h1 class="title">{{ isRegister ? '创建账户' : '欢迎回来' }}</h1>
        <p class="subtitle">{{ isRegister ? '注册你的 Aether 账户' : '登录以继续' }}</p>
      </div>

      <form class="card-form" @submit.prevent="handleSubmit">
        <div class="form-group">
          <label class="form-label">用户名</label>
          <input
            v-model="username"
            type="text"
            class="form-input"
            placeholder="输入用户名"
            autocomplete="username"
            required
          />
        </div>

        <div v-if="isRegister" class="form-group">
          <label class="form-label">显示名称</label>
          <input
            v-model="displayName"
            type="text"
            class="form-input"
            placeholder="你的名字（可选）"
          />
        </div>

        <div class="form-group">
          <label class="form-label">密码</label>
          <input
            v-model="password"
            type="password"
            class="form-input"
            placeholder="输入密码"
            autocomplete="current-password"
            required
          />
        </div>

        <div v-if="error" class="form-error">{{ error }}</div>

        <button type="submit" class="submit-btn" :disabled="loading">
          <span v-if="loading" class="btn-loading"></span>
          <span>{{ loading ? '处理中...' : (isRegister ? '注册' : '登录') }}</span>
        </button>
      </form>

      <div class="card-footer">
        <button class="switch-btn" @click="isRegister = !isRegister">
          {{ isRegister ? '已有账户？去登录' : '没有账户？去注册' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: fixed;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-family);
}

.login-bg {
  position: absolute;
  inset: 0;
  background: var(--color-bg);
}

.bg-gradient {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 50% 40% at 20% 25%, rgba(45, 90, 78, 0.15) 0%, transparent 45%),
    radial-gradient(ellipse 40% 35% at 75% 20%, rgba(183, 128, 176, 0.12) 0%, transparent 40%),
    radial-gradient(ellipse 45% 30% at 50% 80%, rgba(74, 124, 112, 0.1) 0%, transparent 50%),
    radial-gradient(ellipse 35% 25% at 85% 60%, rgba(147, 197, 114, 0.08) 0%, transparent 45%),
    radial-gradient(ellipse 30% 20% at 15% 70%, rgba(110, 157, 204, 0.08) 0%, transparent 40%);
  background-size: 300% 300%;
  animation: bgFlow 20s cubic-bezier(0.45, 0, 0.55, 1) infinite;
}

@keyframes bgFlow {
  0% {
    background-position: 10% 20%, 80% 15%, 50% 85%, 90% 55%, 20% 65%;
  }
  25% {
    background-position: 25% 35%, 65% 30%, 40% 70%, 75% 45%, 35% 50%;
  }
  50% {
    background-position: 40% 50%, 50% 45%, 30% 55%, 60% 35%, 50% 35%;
  }
  75% {
    background-position: 55% 65%, 35% 60%, 20% 40%, 45% 25%, 65% 20%;
  }
  100% {
    background-position: 10% 20%, 80% 15%, 50% 85%, 90% 55%, 20% 65%;
  }
}

.bg-grid {
  position: absolute;
  inset: 0;
  background-image: 
    linear-gradient(rgba(255, 255, 255, 0.02) 1px, transparent 1px),
    linear-gradient(90deg, rgba(255, 255, 255, 0.02) 1px, transparent 1px);
  background-size: 60px 60px;
}

.login-card {
  position: relative;
  width: 100%;
  max-width: 400px;
  padding: var(--space-32);
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-2xl);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
}

.card-header {
  text-align: center;
  margin-bottom: var(--space-24);
}

.brand {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-10);
  margin-bottom: var(--space-20);
}

.brand-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: pulse-dot 2s ease-in-out infinite;
}

@keyframes pulse-dot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.8); }
}

.brand-line {
  width: 40px;
  height: 1px;
  background: rgba(255, 255, 255, 0.2);
}

.title {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin-bottom: var(--space-6);
}

.subtitle {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.card-form {
  display: flex;
  flex-direction: column;
  gap: var(--space-16);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.form-label {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-secondary);
}

.form-input {
  padding: var(--space-12) var(--space-14);
  background: rgba(255, 255, 255, 0.04);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  color: var(--color-text);
  font-size: var(--text-base);
  font-family: var(--font-family);
  transition: all var(--duration-fast) var(--ease-out);
}

.form-input:focus {
  outline: none;
  border-color: var(--color-border-active);
  background: rgba(255, 255, 255, 0.06);
}

.form-input::placeholder {
  color: var(--color-text-muted);
}

.form-error {
  padding: var(--space-10) var(--space-12);
  background: var(--color-danger-bg);
  border: 1px solid rgba(231, 76, 60, 0.2);
  border-radius: var(--radius-md);
  color: var(--color-danger);
  font-size: var(--text-sm);
}

.submit-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-8);
  padding: var(--space-14);
  margin-top: var(--space-8);
  background: var(--color-primary);
  border: none;
  border-radius: var(--radius-lg);
  color: #fff;
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  font-family: var(--font-family);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.submit-btn:hover:not(:disabled) {
  background: var(--color-primary-dark);
  transform: translateY(-1px);
}

.submit-btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.btn-loading {
  width: 16px;
  height: 16px;
  border: 2px solid rgba(255, 255, 255, 0.3);
  border-top-color: #fff;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

.card-footer {
  margin-top: var(--space-20);
  text-align: center;
}

.switch-btn {
  background: none;
  border: none;
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  font-family: var(--font-family);
  cursor: pointer;
  transition: color var(--duration-fast);
}

.switch-btn:hover {
  color: var(--color-primary);
}

@media (max-width: 480px) {
  .login-card {
    margin: var(--space-16);
    padding: var(--space-24);
  }
}
</style>
