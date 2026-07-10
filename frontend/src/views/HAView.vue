<script setup>
import { ref, onMounted } from 'vue'
import { apiGet } from '../utils/api'

const config = ref({
  url: '',
  token_set: false,
  token_preview: '',
})
const tokenInput = ref('')
const loading = ref(true)
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)

async function loadConfig() {
  try {
    loading.value = true
    const data = await apiGet('/api/ha/config')
    config.value.url = data.url || ''
    config.value.token_set = data.token_set || false
    config.value.token_preview = data.token_preview || ''
    tokenInput.value = ''
  } catch (e) {
    console.error('Failed to load HA config:', e)
  } finally {
    loading.value = false
  }
}

async function saveConfig() {
  try {
    saving.value = true
    const payload = { url: config.value.url }
    if (tokenInput.value.trim()) {
      payload.token = tokenInput.value.trim()
    }
    await fetch('/api/ha/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    await loadConfig()
  } catch (e) {
    console.error('Failed to save HA config:', e)
  } finally {
    saving.value = false
  }
}

async function testConnection() {
  try {
    testing.value = true
    testResult.value = null
    const res = await fetch('/api/ha/test', { method: 'POST' })
    const json = await res.json()
    const data = json.data || json || {}
    testResult.value = data.connected ? 'success' : 'fail'
  } catch (e) {
    console.error('Failed to test connection:', e)
    testResult.value = 'fail'
  } finally {
    testing.value = false
  }
}

onMounted(() => {
  loadConfig()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>Home Assistant</h1>
      <p class="page-sub">配置 Home Assistant 连接以集成智能家居</p>
    </header>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="settings-sections">
      <!-- Connection Status -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128279;</span>
          连接状态
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">HA URL</span>
              <span class="label-desc">{{ config.url || '未配置' }}</span>
            </div>
            <span v-if="config.url" class="status-badge configured">已配置</span>
            <span v-else class="status-badge unconfigured">未配置</span>
          </div>
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">Token</span>
              <span class="label-desc">{{ config.token_preview || '未设置' }}</span>
            </div>
            <span v-if="config.token_set" class="status-badge configured">已设置</span>
            <span v-else class="status-badge unconfigured">未设置</span>
          </div>
        </div>
      </section>

      <!-- Edit Form -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#9881;</span>
          连接配置
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">URL</span>
              <span class="label-desc">Home Assistant 地址</span>
            </label>
            <input v-model="config.url" class="setting-input" placeholder="http://homeassistant.local:8123" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">Token</span>
              <span class="label-desc">留空表示不修改</span>
            </label>
            <input v-model="tokenInput" class="setting-input" type="password" placeholder="eyJhbGciOi..." />
          </div>
          <div class="form-actions">
            <button class="btn-save" :disabled="saving" @click="saveConfig">
              {{ saving ? '保存中...' : '保存配置' }}
            </button>
          </div>
        </div>
      </section>

      <!-- Test Connection -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#9889;</span>
          连接测试
        </h2>
        <div class="setting-card">
          <div class="setting-row test-row">
            <div class="setting-label">
              <span class="label-text">测试连接</span>
              <span class="label-desc">验证 HA 配置是否正确</span>
            </div>
            <div class="test-actions">
              <button
                class="btn-test"
                :disabled="testing || !config.url"
                @click="testConnection"
              >
                {{ testing ? '测试中...' : '测试' }}
              </button>
              <span v-if="testResult === 'success'" class="test-result success">连接成功</span>
              <span v-else-if="testResult === 'fail'" class="test-result fail">连接失败</span>
            </div>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.setting-input {
  width: 260px;
}

.status-badge {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  padding: var(--space-1) var(--space-6);
  border-radius: var(--radius-full);
}

.status-badge.configured {
  color: var(--color-success);
  background: var(--color-success-bg);
}

.status-badge.unconfigured {
  color: var(--color-text-muted);
  background: rgba(255, 255, 255, 0.04);
}

.test-row {
  gap: var(--space-8);
}

.test-actions {
  display: flex;
  align-items: center;
  gap: var(--space-6);
}

.btn-test {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid rgba(74, 124, 112, 0.25);
  padding: var(--space-3) var(--space-12);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
  white-space: nowrap;
}

.btn-test:hover:not(:disabled) {
  background: var(--color-primary-hover);
  border-color: rgba(74, 124, 112, 0.35);
}

.btn-test:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.test-result {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
}

.test-result.success {
  color: var(--color-success);
}

.test-result.fail {
  color: #e74c3c;
}

@media (max-width: 768px) {
  .test-row {
    flex-direction: column;
    align-items: flex-start;
  }

  .test-actions {
    width: 100%;
  }
}
</style>
