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
// 全局共享的角色（仅提示，不拦截）— 修改需确认
const SYSTEM_ROLES = ['vision', 'embed']
const PERSONAL_ROLES = ['chat', 'summary', 'stt']

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

// vision/embed 默认锁定，点击锁弹确认框
const lockedRoles = ref({ vision: true, embed: true })
const pendingUnlock = ref(null) // 'vision' | 'embed' | null

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
    // vision/embed 保存后自动锁回
    if (SYSTEM_ROLES.includes(role)) {
      lockedRoles.value[role] = true
    }
  } catch (e) {
    console.error('Failed to save settings:', e)
  } finally {
    saving.value = false
  }
}

// 锁定相关
function clickLock(role) {
  pendingUnlock.value = role
}
function confirmUnlock() {
  if (pendingUnlock.value) {
    lockedRoles.value[pendingUnlock.value] = false
  }
  pendingUnlock.value = null
}
function cancelUnlock() {
  pendingUnlock.value = null
}
function currentModelName(role) {
  const keyId = selectedKeys.value[role]
  if (!keyId) return '未选择'
  const k = keys.value.find(x => x.id === keyId)
  return k ? `${k.model}` : '未选择'
}
function unlockDesc(role) {
  return role === 'embed'
    ? '嵌入模型变更后，所有用户的向量索引将失效，需要重建文档向量和语义图。'
    : '视觉模型变更会影响所有用户的摄像头识别能力。'
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
      <!-- 个人模型：自由配置 -->
      <div class="setting-card">
        <div class="card-title">个人模型</div>
        <div
          v-for="role in PERSONAL_ROLES"
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

      <!-- 系统模型：修改需确认 -->
      <div class="setting-card">
        <div class="card-title">
          系统模型
          <span class="card-title-hint">&#128274; 修改需确认</span>
        </div>
        <div
          v-for="role in SYSTEM_ROLES"
          :key="role"
          class="setting-row"
        >
          <div class="setting-label">
            <span class="label-text">{{ roleLabels[role] }}</span>
            <span class="label-desc">{{ role }} 角色使用的模型（全局共享）</span>
          </div>
          <!-- 锁定时：显示锁图标 + 当前模型名 -->
          <div v-if="lockedRoles[role]" class="role-locked" @click="clickLock(role)" :title="'点击解锁修改'">
            <span class="lock-icon">&#128274;</span>
            <span class="locked-model-name">{{ currentModelName(role) }}</span>
          </div>
          <!-- 解锁后：显示下拉 -->
          <FlowSelect
            v-else
            :model-value="selectedKeys[role]"
            :options="getRoleOptions(role)"
            @update:model-value="v => { selectedKeys[role] = v; saveRole(role) }"
          />
        </div>
      </div>

      <!-- 确认弹窗 -->
      <div v-if="pendingUnlock" class="confirm-overlay" @click.self="cancelUnlock">
        <div class="confirm-card">
          <div class="confirm-icon">&#9888;</div>
          <div class="confirm-title">修改{{ roleLabels[pendingUnlock] }}模型？</div>
          <div class="confirm-desc">{{ unlockDesc(pendingUnlock) }}</div>
          <div class="confirm-actions">
            <button class="btn-cancel" @click="cancelUnlock">取消</button>
            <button class="btn-confirm" @click="confirmUnlock">确认修改</button>
          </div>
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

/* 卡片标题 */
.card-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-8);
  display: flex;
  align-items: center;
  gap: var(--space-4);
}
.card-title-hint {
  font-size: var(--text-xs);
  font-weight: var(--weight-regular);
  color: var(--color-text-muted);
}

/* 锁定行 */
.role-locked {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  cursor: pointer;
  padding: var(--space-2) var(--space-6);
  border-radius: var(--radius-md);
  transition: background var(--duration-fast) var(--ease-out);
  min-width: 140px;
}
.role-locked:hover {
  background: var(--color-surface);
}
.lock-icon {
  font-size: var(--text-sm);
  flex-shrink: 0;
}
.locked-model-name {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  white-space: nowrap;
}

/* 确认弹窗 */
.confirm-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg, rgba(0, 0, 0, 0.5));
  backdrop-filter: blur(4px);
  -webkit-backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.confirm-card {
  width: 380px;
  max-width: calc(100vw - 32px);
  background: var(--dialog-bg, var(--color-surface));
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xl);
  padding: var(--space-20) var(--space-16);
  text-align: center;
}
.confirm-icon {
  font-size: 32px;
  margin-bottom: var(--space-6);
}
.confirm-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  margin-bottom: var(--space-6);
}
.confirm-desc {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
  margin-bottom: var(--space-12);
}
.confirm-actions {
  display: flex;
  gap: var(--space-6);
  justify-content: center;
}
.btn-cancel, .btn-confirm {
  padding: var(--space-3) var(--space-12);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.btn-cancel {
  border: 1px solid var(--color-border-hover);
  background: transparent;
  color: var(--color-text-secondary);
}
.btn-cancel:hover {
  background: var(--color-surface);
}
.btn-confirm {
  border: 1px solid var(--color-warning, #ffc107);
  background: var(--color-warning-bg, rgba(255, 193, 7, 0.1));
  color: var(--color-warning, #ffc107);
}
.btn-confirm:hover {
  background: var(--color-warning, #ffc107);
  color: #1a1a1a;
}
</style>
