<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../composables/useAuth'

const router = useRouter()
const { user } = useAuth()

const currentStep = ref(1)
const totalSteps = 2
const loading = ref(false)
const error = ref('')

// Step 1: 家庭信息
const homeForm = ref({
  home_name: '',
  owner_name: '',
  province: '',
  city: '',
  district: '',
})

// Step 2: LLM 模型配置（4 个角色）
const llmRoles = [
  { key: 'chat', label: '对话模型', desc: '用于聊天和自动化规则评估', icon: '💬' },
  { key: 'vision', label: '视觉模型', desc: '用于摄像头画面分析', icon: '👁️' },
  { key: 'embed', label: '嵌入模型', desc: '用于文本向量化和语义搜索', icon: '🔢' },
  { key: 'summary', label: '摘要模型', desc: '用于对话历史压缩', icon: '📝' },
]

const llmForms = ref({
  chat: { base_url: '', api_key: '', model: '' },
  vision: { base_url: '', api_key: '', model: '' },
  embed: { base_url: '', api_key: '', model: '' },
  summary: { base_url: '', api_key: '', model: '' },
})

const activeRole = ref('chat')

// Step 3: Home Assistant
const haForm = ref({
  url: 'http://localhost:8123',
  token: '',
})

const stepTitle = computed(() => {
  const titles = ['家庭信息', 'LLM 模型配置']
  return titles[currentStep.value - 1]
})

const stepDesc = computed(() => {
  const descs = [
    '设置你的家庭信息，让 AI 更了解你。',
    '配置 AI 模型，至少配置对话模型才能使用核心功能。',
  ]
  return descs[currentStep.value - 1]
})

const canNext = computed(() => {
  if (currentStep.value === 1) {
    return homeForm.value.home_name && homeForm.value.owner_name
  }
  if (currentStep.value === 2) {
    // 至少 chat 角色必须配置
    const chat = llmForms.value.chat
    return chat.base_url && chat.api_key && chat.model
  }
  return false
})

async function submitHomeInfo() {
  loading.value = true
  error.value = ''

  try {
    const res = await fetch('/api/home/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(homeForm.value),
    })

    if (!res.ok) {
      throw new Error('保存失败')
    }

    currentStep.value = 2
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

async function submitLLMConfig() {
  loading.value = true
  error.value = ''

  try {
    // 收集所有已填写的 keys
    const keysToSave = []
    for (const role of llmRoles) {
      const form = llmForms.value[role.key]
      if (form.base_url && form.api_key && form.model) {
        // 先测试连接
        const testRes = await fetch('/api/llm_keys', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            base_url: form.base_url,
            api_key: form.api_key,
            model: form.model,
            type: role.key,
          }),
        })

        if (!testRes.ok) {
          const json = await testRes.json()
          throw new Error(`${role.label}配置失败: ${json.detail || '未知错误'}`)
        }

        const result = await testRes.json()
        // 获取刚创建的 key
        const newKey = result.data?.find(k => k.type === role.key)
        if (newKey) {
          keysToSave.push({
            id: newKey.id,
            base_url: form.base_url,
            api_key: form.api_key,
            model: form.model,
            type: role.key,
            api_key_env: newKey.api_key_env,
          })
        }
      }
    }

    // 同时保存到当前用户的 user_settings（per-user）
    if (keysToSave.length > 0) {
      const userRes = await fetch('/api/auth/me')
      const userJson = await userRes.json()
      const username = userJson.data?.username
      
      if (username) {
        await fetch(`/api/users/${username}/llm_keys`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ keys: keysToSave }),
        })
      }
    }

    finishSetup()
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function finishSetup() {
  router.push('/chat')
}

function nextStep() {
  if (currentStep.value === 1) {
    submitHomeInfo()
  } else if (currentStep.value === 2) {
    submitLLMConfig()
  }
}
</script>

<template>
  <div class="setup-page">
    <div class="setup-card">
      <div class="setup-header">
        <h1 class="setup-title">初始配置</h1>
        <p class="setup-subtitle">欢迎使用 Aether！让我们完成一些基础设置。</p>
      </div>

      <div class="progress-bar">
        <div class="progress-fill" :style="{ width: `${(currentStep / totalSteps) * 100}%` }"></div>
      </div>

      <div class="step-indicator">
        步骤 {{ currentStep }} / {{ totalSteps }}
      </div>

      <div class="step-content">
        <!-- Step 1: 家庭信息 -->
        <div v-if="currentStep === 1" class="step-form">
          <h2 class="step-title">{{ stepTitle }}</h2>
          <p class="step-desc">{{ stepDesc }}</p>

          <div class="form-group">
            <label>家庭名称</label>
            <input v-model="homeForm.home_name" type="text" placeholder="我的家" />
          </div>

          <div class="form-group">
            <label>主人称呼</label>
            <input v-model="homeForm.owner_name" type="text" placeholder="小童" />
            <p class="form-hint">AI 会用这个名字称呼你</p>
          </div>

          <div class="form-row">
            <div class="form-group">
              <label>省份</label>
              <input v-model="homeForm.province" type="text" placeholder="上海市" />
            </div>
            <div class="form-group">
              <label>城市</label>
              <input v-model="homeForm.city" type="text" placeholder="上海市" />
            </div>
            <div class="form-group">
              <label>区县</label>
              <input v-model="homeForm.district" type="text" placeholder="宝山区" />
            </div>
          </div>
        </div>

        <!-- Step 2: LLM 配置 -->
        <div v-if="currentStep === 2" class="step-form">
          <h2 class="step-title">{{ stepTitle }}</h2>
          <p class="step-desc">{{ stepDesc }}</p>

          <div class="role-tabs">
            <button
              v-for="role in llmRoles"
              :key="role.key"
              class="role-tab"
              :class="{ active: activeRole === role.key, configured: llmForms[role.key].base_url && llmForms[role.key].api_key && llmForms[role.key].model }"
              @click="activeRole = role.key"
            >
              <span class="role-icon">{{ role.icon }}</span>
              <span class="role-label">{{ role.label }}</span>
            </button>
          </div>

          <div class="role-desc">
            {{ llmRoles.find(r => r.key === activeRole)?.desc }}
          </div>

          <div class="form-group">
            <label>API Base URL</label>
            <input v-model="llmForms[activeRole].base_url" type="text" placeholder="https://api.openai.com/v1" />
          </div>

          <div class="form-group">
            <label>API Key</label>
            <input v-model="llmForms[activeRole].api_key" type="password" placeholder="sk-..." />
          </div>

          <div class="form-group">
            <label>模型名称</label>
            <input v-model="llmForms[activeRole].model" type="text" placeholder="gpt-4o-mini" />
          </div>

          <div class="llm-status">
            <div v-for="role in llmRoles" :key="role.key" class="status-item">
              <span class="status-icon">{{ llmForms[role.key].base_url && llmForms[role.key].api_key && llmForms[role.key].model ? '✅' : '⬜' }}</span>
              <span class="status-label">{{ role.label }}</span>
            </div>
          </div>
        </div>

        <div v-if="error" class="error-message">{{ error }}</div>
      </div>

      <div class="setup-actions">
        <button
          v-if="currentStep > 1"
          class="btn-back"
          @click="currentStep--"
        >
          上一步
        </button>
        <button
          class="btn-primary"
          :disabled="loading || !canNext"
          @click="nextStep"
        >
          {{ loading ? '处理中...' : (currentStep === 2 ? '完成配置' : '下一步') }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setup-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg);
  padding: var(--space-16);
  position: relative;
  overflow: hidden;
}

/* 光影流动效果 */
.setup-page::before {
  content: '';
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
  pointer-events: none;
  z-index: 0;
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

.setup-page > * {
  position: relative;
  z-index: 1;
}

.setup-card {
  width: 100%;
  max-width: 560px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-2xl);
  padding: var(--space-32);
}

.setup-header {
  text-align: center;
  margin-bottom: var(--space-24);
}

.setup-title {
  font-size: var(--text-2xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin-bottom: var(--space-8);
}

.setup-subtitle {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.progress-bar {
  height: 4px;
  background: var(--color-border);
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: var(--space-8);
}

.progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width 0.3s ease;
}

.step-indicator {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  text-align: right;
  margin-bottom: var(--space-24);
}

.step-content {
  margin-bottom: var(--space-24);
}

.step-form {
  animation: fadeIn 0.3s ease;
}

@keyframes fadeIn {
  from { opacity: 0; transform: translateY(10px); }
  to { opacity: 1; transform: translateY(0); }
}

.step-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-medium);
  color: var(--color-text);
  margin-bottom: var(--space-8);
}

.step-desc {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-20);
}

.form-group {
  margin-bottom: var(--space-16);
}

.form-group label {
  display: block;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-6);
}

.form-group input,
.form-group select {
  width: 100%;
  padding: var(--space-10) var(--space-12);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  color: var(--color-text);
  font-size: var(--text-base);
  transition: border-color 0.2s;
}

.form-group input:focus,
.form-group select:focus {
  outline: none;
  border-color: var(--color-primary);
}

.form-group input::placeholder {
  color: var(--color-text-muted);
}

.form-hint {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  margin-top: var(--space-4);
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr 1fr;
  gap: var(--space-12);
}

/* LLM Role Tabs */
.role-tabs {
  display: flex;
  gap: var(--space-8);
  margin-bottom: var(--space-16);
}

.role-tab {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-12) var(--space-8);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  cursor: pointer;
  transition: all 0.2s;
}

.role-tab:hover {
  border-color: var(--color-border-hover);
}

.role-tab.active {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.role-tab.configured {
  border-color: var(--color-success);
}

.role-icon {
  font-size: var(--text-xl);
}

.role-label {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.role-tab.active .role-label {
  color: var(--color-primary);
  font-weight: var(--weight-medium);
}

.role-desc {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  margin-bottom: var(--space-16);
  padding: var(--space-10);
  background: var(--color-bg);
  border-radius: var(--radius-md);
}

.llm-status {
  display: flex;
  gap: var(--space-16);
  margin-top: var(--space-16);
  padding: var(--space-12);
  background: var(--color-bg);
  border-radius: var(--radius-md);
}

.status-item {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.status-icon {
  font-size: var(--text-sm);
}

.error-message {
  padding: var(--space-10) var(--space-12);
  background: var(--color-danger-bg);
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-md);
  color: var(--color-danger);
  font-size: var(--text-sm);
  margin-top: var(--space-16);
}

.setup-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-12);
}

.btn-primary {
  padding: var(--space-10) var(--space-20);
  background: var(--color-primary);
  color: white;
  border: none;
  border-radius: var(--radius-lg);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: background 0.2s;
}

.btn-primary:hover:not(:disabled) {
  background: var(--color-primary-dark);
}

.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-back {
  padding: var(--space-10) var(--space-16);
  background: transparent;
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all 0.2s;
}

.btn-back:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

@media (max-width: 600px) {
  .setup-card {
    padding: var(--space-20);
  }

  .form-row {
    grid-template-columns: 1fr;
  }

  .role-tabs {
    flex-wrap: wrap;
  }

  .role-tab {
    flex: 1 1 calc(50% - var(--space-4));
  }
}
</style>
