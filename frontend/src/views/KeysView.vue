<script setup>
import { ref, onMounted } from 'vue'
import FlowSelect from '../components/FlowSelect.vue'
import { apiGet } from '../utils/api'

const keys = ref([])
const loading = ref(true)
const showForm = ref(false)
const deleting = ref(null)

const newKey = ref({
  base_url: '',
  model: '',
  type: 'chat',
  api_key: '',
})

const typeOptions = ['chat', 'summary', 'vision', 'embed', 'stt']
const typeSelectOptions = typeOptions.map(t => ({ value: t, label: t }))

async function loadKeys() {
  try {
    loading.value = true
    keys.value = await apiGet('/api/llm_keys') || []
  } catch (e) {
    console.error('Failed to load keys:', e)
  } finally {
    loading.value = false
  }
}

async function addKey() {
  try {
    const res = await fetch('/api/llm_keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newKey.value),
    })
    if (res.ok) {
      newKey.value = { base_url: '', model: '', type: 'chat', api_key: '' }
      showForm.value = false
      await loadKeys()
    }
  } catch (e) {
    console.error('Failed to add key:', e)
  }
}

async function deleteKey(id) {
  try {
    deleting.value = id
    const res = await fetch(`/api/llm_keys/${id}`, { method: 'DELETE' })
    if (res.ok) {
      await loadKeys()
    }
  } catch (e) {
    console.error('Failed to delete key:', e)
  } finally {
    deleting.value = null
  }
}

onMounted(() => {
  loadKeys()
})
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div class="header-left">
        <h1>API Keys</h1>
        <p class="page-sub">管理 LLM 服务的 API 密钥配置</p>
      </div>
      <button class="btn-add" @click="showForm = !showForm">
        {{ showForm ? '取消' : '+ 添加 Key' }}
      </button>
    </header>

    <!-- Add Key Form -->
    <div v-if="showForm" class="setting-card form-card">
      <div class="setting-row">
        <label class="setting-label">
          <span class="label-text">Base URL</span>
          <span class="label-desc">API 服务地址</span>
        </label>
        <input v-model="newKey.base_url" class="setting-input" placeholder="https://api.openai.com/v1" />
      </div>
      <div class="setting-row">
        <label class="setting-label">
          <span class="label-text">Model</span>
          <span class="label-desc">模型名称</span>
        </label>
        <input v-model="newKey.model" class="setting-input" placeholder="gpt-4o" />
      </div>
      <div class="setting-row">
        <label class="setting-label">
          <span class="label-text">Type</span>
          <span class="label-desc">用途类型</span>
        </label>
        <FlowSelect v-model="newKey.type" :options="typeSelectOptions" />
      </div>
      <div class="setting-row">
        <label class="setting-label">
          <span class="label-text">API Key</span>
          <span class="label-desc">密钥内容</span>
        </label>
        <input v-model="newKey.api_key" class="setting-input" type="password" placeholder="sk-..." />
      </div>
      <div class="form-actions">
        <button class="btn-save" @click="addKey">保存</button>
      </div>
    </div>

    <!-- Keys List -->
    <div v-if="loading" class="loading-state">加载中...</div>
    <div v-else-if="keys.length === 0" class="empty-state">暂无配置的 API Key</div>
    <div v-else class="setting-card">
      <div
        v-for="key in keys"
        :key="key.id"
        class="setting-row key-row"
      >
        <div class="key-info">
          <span class="key-model">{{ key.model }}</span>
          <span class="key-meta">
            <span class="key-type-badge">{{ key.type }}</span>
            <span class="key-url">{{ key.base_url }}</span>
            <span v-if="key.api_key_set" class="key-set">已配置</span>
            <span v-else class="key-unset">未配置</span>
          </span>
        </div>
        <button
          class="btn-delete"
          :disabled="deleting === key.id"
          @click="deleteKey(key.id)"
        >
          {{ deleting === key.id ? '删除中...' : '删除' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.form-card {
  margin-bottom: var(--space-16);
}

.setting-input {
  width: 240px;
}

/* Select 下拉框样式 */
select.setting-input {
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23888'/%3E%3C/svg%3E");
  background-repeat: no-repeat;
  background-position: right 10px center;
  padding-right: var(--space-16);
  cursor: pointer;
}

select.setting-input option {
  background: var(--color-bg-app);
  color: var(--color-text);
}

.key-row {
  gap: var(--space-8);
}

.key-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  flex: 1;
}

.key-model {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.key-meta {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.key-type-badge {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.key-url {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}

.key-set {
  font-size: var(--text-xs);
  color: var(--color-success);
}

.key-unset {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.setting-label {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.setting-label .label-text {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.setting-label .label-desc {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.btn-delete {
  background: transparent;
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-border);
  padding: var(--space-3) var(--space-10);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
  white-space: nowrap;
}

.btn-delete:hover:not(:disabled) {
  color: #e74c3c;
  border-color: rgba(231, 76, 60, 0.3);
  background: rgba(231, 76, 60, 0.08);
}

.btn-delete:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .key-row {
    flex-direction: column;
    align-items: flex-start;
  }

  .btn-delete {
    align-self: flex-end;
  }
}
</style>
