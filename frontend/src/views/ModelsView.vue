<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import FlowSelect from '../components/FlowSelect.vue'
import { apiGet, apiPost } from '../utils/api'

const router = useRouter()

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

// embed 模型变更后提示重建向量索引
const embedChanged = ref(false)
const docRebuilding = ref(false)
let docPollTimer = null

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
    await apiPost('/api/llm/settings', {
      role,
      key_id: selectedKeys.value[role],
    })
    if (role === 'embed') {
      embedChanged.value = true
    }
  } catch (e) {
    console.error('Failed to save settings:', e)
  } finally {
    saving.value = false
  }
}

// 重建文档向量：POST /api/doc/rebuild + 轮询状态
async function startDocRebuild() {
  try {
    docRebuilding.value = true
    await fetch('/api/doc/rebuild', { method: 'POST' })
    docPollTimer = setInterval(pollDocRebuild, 2000)
  } catch (e) {
    console.error('Failed to start doc rebuild:', e)
    docRebuilding.value = false
  }
}

async function pollDocRebuild() {
  try {
    const res = await fetch('/api/doc/rebuild/status')
    const json = await res.json()
    const data = json.data || json
    if (!data.rebuilding) {
      if (docPollTimer) { clearInterval(docPollTimer); docPollTimer = null }
      docRebuilding.value = false
      embedChanged.value = false
    }
  } catch (e) {
    console.error('Failed to poll doc rebuild status:', e)
  }
}

onUnmounted(() => {
  if (docPollTimer) { clearInterval(docPollTimer); docPollTimer = null }
})

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

      <!-- embed 模型变更提示横幅 -->
      <div v-if="embedChanged" class="embed-changed-banner">
        <span class="banner-icon">&#9888;</span>
        <span class="banner-text">Embed 模型已变更，建议重建向量索引</span>
        <div class="banner-actions">
          <button class="btn-rebuild" :disabled="docRebuilding" @click="startDocRebuild">
            {{ docRebuilding ? '重建中...' : '重建文档向量' }}
          </button>
          <button class="btn-rebuild" @click="router.push('/sg')">重建语义图</button>
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

.embed-changed-banner {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-10) var(--space-14);
  background: var(--color-warning-bg, rgba(255, 193, 7, 0.1));
  border: 1px solid var(--color-warning, #ffc107);
  border-radius: var(--radius-lg);
}

.banner-icon {
  font-size: var(--text-lg);
  flex-shrink: 0;
}

.banner-text {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--color-text);
}

.banner-actions {
  display: flex;
  gap: var(--space-4);
  flex-shrink: 0;
}

.btn-rebuild {
  padding: var(--space-3) var(--space-10);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  white-space: nowrap;
}

.btn-rebuild:hover:not(:disabled) {
  background: var(--color-primary);
  color: #fff;
}

.btn-rebuild:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .setting-input {
    width: 100%;
    text-align: left;
  }
}
</style>
