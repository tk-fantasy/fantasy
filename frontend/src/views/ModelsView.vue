<script setup>
import { ref, computed, onMounted } from 'vue'
import FlowSelect from '../components/FlowSelect.vue'
import { apiGet } from '../utils/api'

const roles = ['chat', 'vision', 'summary', 'embed', 'stt']
const roleLabels = {
  chat: '对话',
  vision: '视觉',
  summary: '摘要',
  embed: '嵌入',
  stt: '语音',
}

const keys = ref([])
const selectedKeys = ref({
  chat: '',
  vision: '',
  summary: '',
  embed: '',
  stt: '',
})
const loading = ref(true)
const saving = ref(false)

async function loadData() {
  try {
    loading.value = true
    const [keysData, settings] = await Promise.all([
      apiGet('/api/llm_keys'),
      apiGet('/api/llm/settings'),
    ])
    keys.value = keysData || []
    const current = settings.current || {}
    
    // 提取每个角色的 key_id
    for (const role of roles) {
      selectedKeys.value[role] = current[role]?.key_id || ''
    }
  } catch (e) {
    console.error('Failed to load data:', e)
  } finally {
    loading.value = false
  }
}

function getRoleOptions(role) {
  const opts = [{ value: '', label: '-- 未选择 --' }]
  for (const key of keys.value) {
    if (key.type === role) {
      opts.push({ value: key.id, label: `${key.model} (${key.base_url})` })
    }
  }
  return opts
}

async function saveRole(role) {
  try {
    saving.value = true
    await fetch('/api/llm/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        role,
        key_id: selectedKeys.value[role],
      }),
    })
  } catch (e) {
    console.error('Failed to save settings:', e)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadData()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>模型配置</h1>
      <p class="page-sub">为不同角色分配 LLM 模型</p>
    </header>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="settings-sections">
      <div class="setting-card">
        <div
          v-for="role in roles"
          :key="role"
          class="setting-row"
        >
          <div class="setting-label">
            <span class="label-text">{{ roleLabels[role] }}</span>
            <span class="label-desc">{{ role }} 角色使用的模型</span>
          </div>
          <FlowSelect
            :model-value="selectedKeys[role]"
            :options="getRoleOptions(role)"
            @update:model-value="v => { selectedKeys[role] = v; saveRole(role) }"
          />
        </div>
      </div>

      <div class="save-bar" v-if="saving">
        <span class="saving-text">保存中...</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-sections {
  gap: var(--space-16);
}

.setting-input {
  width: 240px;
  cursor: pointer;
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
}

select.setting-input option {
  background: var(--color-bg-app);
  color: var(--color-text);
}

.saving-text {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}

@media (max-width: 768px) {
  .setting-input {
    width: 100%;
    text-align: left;
  }
}
</style>
