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

// 顶层 tab：我的模型 / 全局配置
const activeTab = ref('mine')

const keys = ref([])
const selectedKeys = ref({
  chat: '',
  vision: '',
  summary: '',
  embed: '',
  stt: '',
})
// use_global 标志：chat/summary/stt 是否走全局兜底（true=全局，false=私有）
const useGlobal = ref({
  chat: false,
  summary: false,
  stt: false,
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

    // 提取每个角色的 key_id 和 use_global
    for (const role of roles) {
      selectedKeys.value[role] = current[role]?.key_id || ''
    }
    for (const role of PERSONAL_ROLES) {
      useGlobal.value[role] = bool(current[role]?.use_global)
    }
  } catch (e) {
    console.error('Failed to load data:', e)
  } finally {
    loading.value = false
  }
}

function bool(v) {
  return v === true || v === 'true' || v === 1
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
      use_global: useGlobal.value[role],
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

// 切换私有/全局：true=该角色走全局兜底，false=走 per-user
async function toggleUseGlobal(role, value) {
  useGlobal.value[role] = value
  // 切到全局时清空 per-user key_id 选择（resolver 会因 use_global=true 直接返回 None 走全局）
  if (value) {
    selectedKeys.value[role] = ''
  }
  await saveRole(role)
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

// ============ 全局配置 tab ============

// 二级密码状态：'unknown' | 'unset' | 'locked' | 'unlocked'
const passwordStatus = ref('unknown')
const passwordInput = ref('')
const passwordConfirm = ref('')
const passwordError = ref('')
const passwordLoading = ref(false)
// 解锁后持有密码的 ref —— 写操作（CRUD 全局 key / 改全局 providers）需每次带密码
// 验证成功后存这里，passwordInput 输入框清空只影响显示。
// 退出解锁或刷新页时清空（session 级，不持久化）。
const sessionPassword = ref('')

// 全局 key 列表 + 全局 providers
const globalKeys = ref([])
const globalProviders = ref({})
const globalLoading = ref(false)

// 编辑/新增全局 key 的表单
const editingKey = ref(null) // null=不在编辑；{}=新增；{id:...}=编辑
const keyForm = ref({ id: '', base_url: '', model: '', type: 'chat', api_key: '' })
const keyFormError = ref('')
const keySaving = ref(false)

// 全局 providers 编辑（每个角色选 key_id）
const globalSelectedKeys = ref({ chat: '', vision: '', summary: '', embed: '', stt: '' })
const globalSaving = ref(false)
const restartNotice = ref('') // 热重载失败时提示需重启

async function loadGlobalPanel() {
  // 查密码状态
  try {
    const status = await apiGet('/api/global/password/status')
    if (!status.set) {
      passwordStatus.value = 'unset'
    } else {
      passwordStatus.value = 'locked'
    }
  } catch (e) {
    passwordStatus.value = 'unknown'
    console.error('Failed to check password status:', e)
  }
  // 全局 key 列表 + 全局 providers 读操作不需密码
  await Promise.all([loadGlobalKeys(), loadGlobalSettings()])
}

async function loadGlobalKeys() {
  try {
    globalKeys.value = (await apiGet('/api/global/llm_keys')) || []
  } catch (e) {
    globalKeys.value = []
    console.error('Failed to load global keys:', e)
  }
}

async function loadGlobalSettings() {
  try {
    const data = await apiGet('/api/global/llm/settings')
    const current = data.current || {}
    globalProviders.value = current
    for (const role of roles) {
      globalSelectedKeys.value[role] = current[role]?.key_id || ''
    }
  } catch (e) {
    console.error('Failed to load global settings:', e)
  }
}

// 首次设置二级密码
async function setupPassword() {
  passwordError.value = ''
  if (passwordInput.value.length < 6) {
    passwordError.value = '密码至少 6 位'
    return
  }
  if (passwordInput.value !== passwordConfirm.value) {
    passwordError.value = '两次输入不一致'
    return
  }
  try {
    passwordLoading.value = true
    await apiPost('/api/global/password', { password: passwordInput.value })
    sessionPassword.value = passwordInput.value  // 保留供后续写操作使用
    passwordStatus.value = 'unlocked'
    passwordInput.value = ''
    passwordConfirm.value = ''
  } catch (e) {
    passwordError.value = e.message || '设置失败'
  } finally {
    passwordLoading.value = false
  }
}

// 验证二级密码解锁
async function verifyPassword() {
  passwordError.value = ''
  try {
    passwordLoading.value = true
    await apiPost('/api/global/password/verify', { password: passwordInput.value })
    sessionPassword.value = passwordInput.value  // 保留供后续写操作使用
    passwordStatus.value = 'unlocked'
    passwordInput.value = ''
  } catch (e) {
    passwordError.value = e.message || '验证失败'
  } finally {
    passwordLoading.value = false
  }
}

function exitUnlocked() {
  passwordStatus.value = 'locked'
  passwordInput.value = ''
  sessionPassword.value = ''  // 退出解锁时清掉 session 密码
  editingKey.value = null
}

// 重置（清除）二级密码——丢了密码的自救入口。设计上不验证原密码（谁都能点），
// 前端二次确认防止误触。清除后回到"未设置"状态，可重新设置新密码。
async function resetPassword() {
  if (!confirm(
    '确定重置二级密码？\n\n' +
    '· 清除后已配置的全局 key 仍保留，但任何修改全局 key 的操作都会被拒\n' +
    '· 需要重新设置新二级密码才能继续管理全局配置\n' +
    '· 此操作不可撤销'
  )) return
  passwordError.value = ''
  try {
    passwordLoading.value = true
    const res = await fetch('/api/global/password', {
      method: 'DELETE',
      credentials: 'include',
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.message || `重置失败：HTTP ${res.status}`)
    }
    passwordStatus.value = 'unset'
    passwordInput.value = ''
    passwordConfirm.value = ''
    sessionPassword.value = ''
  } catch (e) {
    passwordError.value = e.message || '重置失败'
  } finally {
    passwordLoading.value = false
  }
}

function getGlobalRoleOptions(role) {
  const opts = [{ value: '', label: '-- 未选择 --' }]
  for (const key of globalKeys.value) {
    if (key.type === role) {
      opts.push({ value: key.id, label: `${key.model} (${key.base_url})` })
    }
  }
  return opts
}

function startAddKey() {
  editingKey.value = {}
  keyForm.value = { id: '', base_url: '', model: '', type: 'chat', api_key: '' }
  keyFormError.value = ''
}

function startEditKey(k) {
  editingKey.value = k
  keyForm.value = {
    id: k.id,
    base_url: k.base_url,
    model: k.model,
    type: k.type,
    api_key: '', // 编辑时留空 = 不改密钥
  }
  keyFormError.value = ''
}

function cancelEditKey() {
  editingKey.value = null
  keyFormError.value = ''
}

async function saveGlobalKey() {
  keyFormError.value = ''
  if (!keyForm.value.base_url || !keyForm.value.model) {
    keyFormError.value = 'base_url 和 model 必填'
    return
  }
  const isEdit = !!editingKey.value?.id
  if (!isEdit && !keyForm.value.api_key) {
    keyFormError.value = '新增 key 必须填 api_key'
    return
  }
  try {
    keySaving.value = true
    const result = await apiPost('/api/global/llm_keys', {
      id: keyForm.value.id || '',
      base_url: keyForm.value.base_url,
      model: keyForm.value.model,
      type: keyForm.value.type,
      api_key: keyForm.value.api_key,
      password: sessionPassword.value,
    })
    if (result.restart_required) {
      restartNotice.value = '全局对话模型已变更，部分在飞请求可能受影响。若聊天报错请重启服务。'
    } else {
      restartNotice.value = ''
    }
    globalKeys.value = result.keys || []
    editingKey.value = null
    // 同步刷新全局 providers（key 列表变了，下拉选项要更新）
    await loadGlobalSettings()
  } catch (e) {
    keyFormError.value = e.message || '保存失败'
  } finally {
    keySaving.value = false
  }
}

async function deleteGlobalKey(keyId) {
  if (!confirm(`确认删除全局 key ${keyId}？.env 中的密钥会保留，但该 key 将从全局配置移除。`)) return
  try {
    const res = await fetch(`/api/global/llm_keys/${encodeURIComponent(keyId)}?password=${encodeURIComponent(sessionPassword.value)}`, {
      method: 'DELETE',
      credentials: 'include',
    })
    if (!res.ok) {
      const j = await res.json().catch(() => ({}))
      throw new Error(j.message || `删除失败：HTTP ${res.status}`)
    }
    const j = await res.json()
    globalKeys.value = j.data?.keys || []
    await loadGlobalSettings()
  } catch (e) {
    alert(e.message || '删除失败')
  }
}

async function saveGlobalProvider(role) {
  try {
    globalSaving.value = true
    const result = await apiPost('/api/global/llm/settings', {
      role,
      key_id: globalSelectedKeys.value[role],
      password: sessionPassword.value,
    })
    if (result.restart_required) {
      restartNotice.value = '全局对话模型已变更，部分在飞请求可能受影响。若聊天报错请重启服务。'
    } else {
      restartNotice.value = ''
    }
    await loadGlobalSettings()
  } catch (e) {
    alert(e.message || '保存失败')
  } finally {
    globalSaving.value = false
  }
}

onUnmounted(() => {
  if (docPollTimer) { clearInterval(docPollTimer); docPollTimer = null }
})

onMounted(() => {
  loadData()
  loadGlobalPanel()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>模型配置</h1>
      <p class="page-sub">为不同角色分配 LLM 模型</p>
    </header>

    <!-- 顶层 tab -->
    <div class="tabs-bar">
      <button :class="{ active: activeTab === 'mine' }" @click="activeTab = 'mine'">我的模型</button>
      <button :class="{ active: activeTab === 'global' }" @click="activeTab = 'global'">全局配置</button>
    </div>

    <div v-if="loading && activeTab === 'mine'" class="loading-state">加载中...</div>

    <!-- ============ 我的模型 tab ============ -->
    <div v-show="activeTab === 'mine'" class="settings-sections">
      <!-- 个人模型：自由配置 -->
      <div class="setting-card">
        <div class="card-title">个人模型</div>
        <div
          v-for="role in PERSONAL_ROLES"
          :key="role"
          class="setting-row setting-row-with-toggle"
        >
          <div class="setting-label">
            <span class="label-text">{{ roleLabels[role] }}</span>
            <span class="label-desc">{{ role }} 角色使用的模型</span>
          </div>
          <div class="setting-controls">
            <!-- 私有/全局开关 -->
            <div class="seg-toggle">
              <button
                :class="{ active: !useGlobal[role] }"
                @click="toggleUseGlobal(role, false)"
              >私有 key</button>
              <button
                :class="{ active: useGlobal[role] }"
                @click="toggleUseGlobal(role, true)"
              >全局 key</button>
            </div>
            <!-- 选全局时禁用下拉，灰显"使用全局 key" -->
            <FlowSelect
              v-if="!useGlobal[role]"
              :model-value="selectedKeys[role]"
              :options="getRoleOptions(role)"
              @update:model-value="v => { selectedKeys[role] = v; saveRole(role) }"
            />
            <div v-else class="global-placeholder">使用全局 key</div>
          </div>
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

    <!-- ============ 全局配置 tab ============ -->
    <div v-show="activeTab === 'global'" class="settings-sections">
      <!-- 未设二级密码：首次设置 -->
      <div v-if="passwordStatus === 'unset'" class="setting-card">
        <div class="card-title">首次设置全局配置二级密码</div>
        <div class="password-hint">
          全局 key 影响所有用户的模型与费用，请设置一个二级密码用于门禁。
          密码哈希存 config.json。如忘记密码，可在下方解锁页点"重置"清除后重新设置。
        </div>
        <div class="password-form">
          <input
            v-model="passwordInput"
            type="password"
            placeholder="二级密码（至少 6 位）"
            class="setting-input"
          />
          <input
            v-model="passwordConfirm"
            type="password"
            placeholder="确认密码"
            class="setting-input"
          />
          <button class="btn-primary" :disabled="passwordLoading" @click="setupPassword">
            {{ passwordLoading ? '设置中...' : '设置密码' }}
          </button>
        </div>
        <div v-if="passwordError" class="form-error">{{ passwordError }}</div>
      </div>

      <!-- 已设但未解锁：输入密码 -->
      <div v-else-if="passwordStatus === 'locked'" class="setting-card">
        <div class="card-title">输入二级密码</div>
        <div class="password-hint">全局配置已设置二级密码，请输入以解锁管理面板。</div>
        <div class="password-form">
          <input
            v-model="passwordInput"
            type="password"
            placeholder="二级密码"
            class="setting-input"
            @keyup.enter="verifyPassword"
          />
          <button class="btn-primary" :disabled="passwordLoading" @click="verifyPassword">
            {{ passwordLoading ? '验证中...' : '解锁' }}
          </button>
        </div>
        <div v-if="passwordError" class="form-error">{{ passwordError }}</div>
        <div class="password-footer">
          <button class="btn-link btn-danger" :disabled="passwordLoading" @click="resetPassword">
            忘记密码？重置
          </button>
        </div>
      </div>

      <!-- 已解锁：全局 key 管理 + 全局 providers -->
      <div v-else-if="passwordStatus === 'unlocked'" class="settings-sections">
        <div class="setting-card">
          <div class="card-title">
            全局 LLM Keys
            <button class="btn-link" @click="exitUnlocked">锁定</button>
          </div>
          <div class="password-hint">
            全局 key 存 config.json，所有用户共享。密钥本身存 .env，config.json 只存 env 变量名。
            修改全局对话(chat)模型会触发热重载，部分在飞请求可能受影响。
          </div>

          <!-- key 列表 -->
          <div v-if="globalKeys.length === 0" class="empty-hint">暂无全局 key，点击"新增"添加。</div>
          <div v-else class="key-list">
            <div v-for="k in globalKeys" :key="k.id" class="key-item">
              <div class="key-info">
                <span class="key-type-badge" :class="`type-${k.type}`">{{ k.type }}</span>
                <span class="key-model">{{ k.model }}</span>
                <span class="key-base">{{ k.base_url }}</span>
                <span class="key-env" :class="{ set: k.api_key_set }">
                  {{ k.api_key_set ? '密钥已配置' : '密钥未配置' }}
                </span>
              </div>
              <div class="key-actions">
                <button class="btn-link" @click="startEditKey(k)">编辑</button>
                <button class="btn-link btn-danger" @click="deleteGlobalKey(k.id)">删除</button>
              </div>
            </div>
          </div>

          <button v-if="!editingKey" class="btn-primary" @click="startAddKey">新增全局 key</button>

          <!-- 新增/编辑表单 -->
          <div v-if="editingKey" class="key-form">
            <div class="form-title">{{ editingKey.id ? `编辑 ${editingKey.id}` : '新增全局 key' }}</div>
            <div class="form-row">
              <label>类型</label>
              <select v-model="keyForm.type" class="setting-input">
                <option value="chat">chat</option>
                <option value="summary">summary</option>
                <option value="vision">vision</option>
                <option value="embed">embed</option>
                <option value="stt">stt</option>
              </select>
            </div>
            <div class="form-row">
              <label>Base URL</label>
              <input v-model="keyForm.base_url" class="setting-input" placeholder="https://api.example.com/v1" />
            </div>
            <div class="form-row">
              <label>模型名</label>
              <input v-model="keyForm.model" class="setting-input" placeholder="glm-4-flash" />
            </div>
            <div class="form-row">
              <label>API Key</label>
              <input
                v-model="keyForm.api_key"
                type="password"
                class="setting-input"
                :placeholder="editingKey.id ? '留空=不修改' : '必填'"
              />
            </div>
            <div v-if="keyFormError" class="form-error">{{ keyFormError }}</div>
            <div class="form-actions">
              <button class="btn-cancel" @click="cancelEditKey">取消</button>
              <button class="btn-primary" :disabled="keySaving" @click="saveGlobalKey">
                {{ keySaving ? '保存中...' : '保存' }}
              </button>
            </div>
          </div>
        </div>

        <!-- 全局 providers：为每个角色绑定 key_id -->
        <div class="setting-card">
          <div class="card-title">全局角色分配</div>
          <div class="password-hint">为每个角色指定默认使用的全局 key。所有未配置 per-user key 的用户会走这里的全局绑定。</div>
          <div
            v-for="role in roles"
            :key="role"
            class="setting-row"
          >
            <div class="setting-label">
              <span class="label-text">{{ roleLabels[role] }}</span>
              <span class="label-desc">{{ role }} 角色全局默认</span>
            </div>
            <FlowSelect
              :model-value="globalSelectedKeys[role]"
              :options="getGlobalRoleOptions(role)"
              @update:model-value="v => { globalSelectedKeys[role] = v; saveGlobalProvider(role) }"
            />
          </div>
        </div>

        <div v-if="restartNotice" class="embed-changed-banner">
          <span class="banner-icon">&#9888;</span>
          <span class="banner-text">{{ restartNotice }}</span>
        </div>
        <div v-if="globalSaving" class="save-bar">
          <span class="saving-text">保存中...</span>
        </div>
      </div>

      <!-- 加载中/未知状态 -->
      <div v-else class="loading-state">加载全局配置状态...</div>
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

/* ============ 顶层 tab ============ */
.tabs-bar {
  display: flex;
  gap: var(--space-4);
  border-bottom: 1px solid var(--color-border);
  margin-bottom: var(--space-16);
}
.tabs-bar button {
  padding: var(--space-6) var(--space-12);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.tabs-bar button:hover {
  color: var(--color-text);
}
.tabs-bar button.active {
  color: var(--color-primary);
  border-bottom-color: var(--color-primary);
}

/* ============ 我的模型：私有/全局开关 ============ */
.setting-row-with-toggle {
  flex-wrap: wrap;
}
.setting-controls {
  display: flex;
  align-items: center;
  gap: var(--space-8);
}
.seg-toggle {
  display: inline-flex;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  overflow: hidden;
}
.seg-toggle button {
  padding: var(--space-2) var(--space-8);
  background: transparent;
  border: none;
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.seg-toggle button.active {
  background: var(--color-primary);
  color: #fff;
}
.global-placeholder {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
  padding: var(--space-2) var(--space-6);
  background: var(--color-surface);
  border-radius: var(--radius-md);
  min-width: 140px;
  text-align: center;
}

/* ============ 全局配置 tab ============ */
.password-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  line-height: 1.6;
  margin-bottom: var(--space-10);
}
.password-form {
  display: flex;
  gap: var(--space-6);
  align-items: center;
  flex-wrap: wrap;
}
.password-form .setting-input {
  flex: 1;
  min-width: 180px;
  width: auto;
}
.form-error {
  color: var(--color-danger, #e53935);
  font-size: var(--text-xs);
  margin-top: var(--space-4);
}
.password-footer {
  margin-top: var(--space-8);
  text-align: right;
}
.btn-primary {
  padding: var(--space-3) var(--space-12);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
  background: var(--color-primary);
  color: #fff;
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  white-space: nowrap;
}
.btn-primary:hover:not(:disabled) {
  opacity: 0.9;
}
.btn-primary:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-link {
  background: transparent;
  border: none;
  color: var(--color-primary);
  font-size: var(--text-xs);
  cursor: pointer;
  padding: var(--space-2) var(--space-4);
  text-decoration: underline;
}
.btn-link.btn-danger {
  color: var(--color-danger, #e53935);
}
.empty-hint {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
  padding: var(--space-10) 0;
}
.key-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  margin-bottom: var(--space-10);
}
.key-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-6) var(--space-8);
  background: var(--color-surface);
  border-radius: var(--radius-md);
  gap: var(--space-8);
  flex-wrap: wrap;
}
.key-info {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  flex-wrap: wrap;
}
.key-type-badge {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-4);
  border-radius: var(--radius-sm);
  background: var(--color-primary-light);
  color: var(--color-primary);
  font-weight: var(--weight-semibold);
  text-transform: uppercase;
}
.key-model {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}
.key-base {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}
.key-env {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}
.key-env.set {
  color: var(--color-success, #43a047);
}
.key-actions {
  display: flex;
  gap: var(--space-4);
}
.key-form {
  margin-top: var(--space-10);
  padding: var(--space-12);
  background: var(--color-surface);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}
.form-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  margin-bottom: var(--space-4);
}
.form-row {
  display: flex;
  align-items: center;
  gap: var(--space-8);
}
.form-row label {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  min-width: 70px;
}
.form-row .setting-input {
  flex: 1;
  width: auto;
}
.form-actions {
  display: flex;
  gap: var(--space-6);
  justify-content: flex-end;
  margin-top: var(--space-4);
}
</style>
