<template>
  <div class="plugin-page">
    <header class="page-header">
      <h1>🔌 插件管理</h1>
      <p class="subtitle">管理已安装的集成插件：启用/禁用、导出、上传、删除</p>
    </header>

    <!-- 上传区 -->
    <section class="upload-section">
      <div
        class="upload-zone"
        :class="{ dragging }"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="handleDrop"
        @click="$refs.fileInput.click()"
      >
        <span class="upload-hint">📦 拖拽插件 zip 到此处，或点击选择文件</span>
        <input ref="fileInput" type="file" accept=".zip" hidden @change="handleFileSelect" />
      </div>
      <div v-if="uploadMsg" class="upload-msg" :class="{ ok: uploadOk }">{{ uploadMsg }}</div>
    </section>

    <!-- 插件列表 -->
    <section class="plugins-section">
      <div v-if="loading" class="loading">加载中…</div>
      <div v-else-if="plugins.length === 0" class="empty">
        暂无插件。上传一个 zip 包，或确认 integrations/ 目录有插件。
      </div>
      <div v-else class="plugin-grid">
        <div v-for="p in plugins" :key="p.id" class="plugin-card" :class="{ disabled: !p.enabled }" @click="openDetail(p)">
          <div class="card-header">
            <span class="plugin-name">{{ p.name || p.id }}</span>
            <span class="plugin-version">v{{ p.version }}</span>
          </div>
          <div class="plugin-id">{{ p.id }}</div>
          <div v-if="p.description" class="plugin-desc">{{ p.description }}</div>
          <div class="plugin-meta">
            <span class="badge" :class="{ alive: p.alive, dead: !p.alive }">
              {{ p.alive ? '运行中' : (p.enabled ? '未启动' : '已禁用') }}
            </span>
            <span v-for="cap in p.capabilities" :key="cap" class="cap-badge">{{ cap }}</span>
            <span v-if="hasConfig(p)" class="cap-badge config-badge">可配置</span>
          </div>
          <div class="card-actions" @click.stop>
            <button class="action-btn" @click="toggleEnabled(p)">
              {{ p.enabled ? '禁用' : '启用' }}
            </button>
            <button class="action-btn" @click="openDetail(p)">详情</button>
            <button class="action-btn danger" @click="deletePlugin(p)" :disabled="p.alive">
              删除
            </button>
          </div>
        </div>
      </div>
    </section>

    <!-- 插件详情/配置弹窗 -->
    <div v-if="detail" class="modal-mask" @click.self="closeDetail">
      <div class="modal">
        <div class="modal-header">
          <div>
            <h2 class="modal-title">{{ detail.name || detail.id }}
              <span class="plugin-version">v{{ detail.version }}</span>
            </h2>
            <div class="plugin-id">{{ detail.id }}</div>
          </div>
          <button class="modal-close" @click="closeDetail">×</button>
        </div>
        <p class="modal-desc">{{ detail.description || '（无描述）' }}</p>
        <div class="modal-meta">
          <span class="badge" :class="{ alive: detail.alive, dead: !detail.alive }">
            {{ detail.alive ? '运行中' : (detail.enabled ? '未启动' : '已禁用') }}
          </span>
          <span v-for="cap in detail.capabilities" :key="cap" class="cap-badge">{{ cap }}</span>
        </div>

        <!-- 配置表单（声明了 config_schema 的插件才有） -->
        <div v-if="hasConfig(detail)" class="config-section">
          <h3 class="config-title">配置</h3>
          <div v-if="configLoading" class="config-loading">加载配置中…</div>
          <template v-else>
            <div v-for="(field, key) in configSchema" :key="key" class="config-field">
              <label class="field-label">
                {{ field.label || key }}
                <span v-if="field.required" class="required">*</span>
              </label>
              <select
                v-if="field.type === 'enum'"
                v-model="configForm[key]"
                class="field-input"
              >
                <option v-for="opt in field.options" :key="opt" :value="opt">{{ opt }}</option>
              </select>
              <input
                v-else
                :type="field.type === 'secret' ? 'password' : 'text'"
                v-model="configForm[key]"
                class="field-input"
                :placeholder="secretPlaceholder(key, field)"
                autocomplete="new-password"
              />
              <div v-if="field.type === 'secret' && secretIsSet(key)" class="field-hint">
                已配置（{{ configValues[key]?.masked }}），留空保持不变
              </div>
            </div>
            <div v-if="configError" class="config-error">{{ configError }}</div>
            <div class="config-actions">
              <button class="save-btn" :disabled="configSaving" @click="saveConfig">
                {{ configSaving ? '保存中…' : '保存并生效' }}
              </button>
              <span v-if="configSaved" class="saved-hint">✓ 已保存</span>
            </div>
          </template>
        </div>
        <div v-else class="config-none">此插件无可配置项</div>

        <div class="modal-footer">
          <button class="action-btn" @click="exportPlugin(detail)">导出插件包</button>
        </div>
      </div>
    </div>

    <button class="back-btn" @click="$router.push('/chat')">返回聊天</button>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '../utils/api'

const loading = ref(true)
const plugins = ref([])
const dragging = ref(false)
const uploadMsg = ref('')
const uploadOk = ref(false)
const fileInput = ref(null)

// ============ 详情/配置弹窗 ============
const detail = ref(null)            // 当前打开的插件
const configSchema = ref({})        // {key: {type,label,required,...}}
const configValues = ref({})        // 读取值（secret 为 {is_set, masked}）
const configForm = ref({})          // 表单值（secret 留空=保持原值）
const configLoading = ref(false)
const configSaving = ref(false)
const configSaved = ref(false)
const configError = ref('')

function hasConfig(p) {
  return !!(p && p.config_schema && Object.keys(p.config_schema).length)
}

async function openDetail(p) {
  detail.value = p
  configError.value = ''
  configSaved.value = false
  configSchema.value = p.config_schema || {}
  if (!hasConfig(p)) return
  configLoading.value = true
  configForm.value = {}
  try {
    const data = await apiGet(`/api/integrations/${p.id}/config`)
    configSchema.value = data?.schema || {}
    configValues.value = data?.values || {}
    const form = {}
    for (const [key, field] of Object.entries(configSchema.value)) {
      if (field.type === 'secret') form[key] = ''  // 密码框不回显明文
      else if (field.type === 'enum') form[key] = configValues.value[key] || field.default || (field.options || [])[0]
      else form[key] = configValues.value[key] ?? field.default ?? ''
    }
    configForm.value = form
  } catch (e) {
    configError.value = '配置读取失败：' + (e?.message || e)
  } finally {
    configLoading.value = false
  }
}

function closeDetail() {
  detail.value = null
}

function secretIsSet(key) {
  return !!configValues.value[key]?.is_set
}

function secretPlaceholder(key, field) {
  if (secretIsSet(key)) return ''
  return field.placeholder || '未配置'
}

async function saveConfig() {
  if (!detail.value) return
  configSaving.value = true
  configError.value = ''
  configSaved.value = false
  try {
    const data = await apiPost(`/api/integrations/${detail.value.id}/config`, {
      values: configForm.value,
    })
    if (data?.applied === 'not_found') {
      configError.value = '配置已保存，但插件未在运行，下次启动时生效'
    } else {
      configSaved.value = true
    }
    await loadPlugins()
    // 同步弹窗内的插件对象（重启后 alive 状态变化）
    const fresh = plugins.value.find(x => x.id === detail.value.id)
    if (fresh) detail.value = fresh
    // 重新拉取脱敏值（让「已配置（xxxx…xxxx）」提示立即出现）
    const cfg = await apiGet(`/api/integrations/${detail.value.id}/config`)
    configValues.value = cfg?.values || {}
    for (const key of Object.keys(configForm.value)) {
      if (configSchema.value[key]?.type === 'secret') configForm.value[key] = ''
    }
  } catch (e) {
    configError.value = e?.message || '保存失败'
  } finally {
    configSaving.value = false
  }
}

async function loadPlugins() {
  loading.value = true
  try {
    const data = await apiGet('/api/integrations')
    plugins.value = data?.plugins || []
  } catch (e) {
    console.error('load plugins failed:', e)
    plugins.value = []
  } finally {
    loading.value = false
  }
}

async function toggleEnabled(p) {
  try {
    const data = await apiPost(`/api/integrations/${p.id}/toggle-enabled`, {})
    if (data?.enabled !== undefined) {
      p.enabled = data.enabled
    }
  } catch (e) {
    console.error('toggle failed:', e)
  }
}

function exportPlugin(p) {
  // 浏览器直接下载 zip
  window.location.href = `/api/integrations/${p.id}/export`
}

async function deletePlugin(p) {
  if (!confirm(`确定删除插件「${p.name || p.id}」？此操作不可恢复。`)) return
  try {
    const res = await fetch(`/api/integrations/${p.id}`, { method: 'DELETE', credentials: 'include' })
    const data = await res.json()
    if (data.success) {
      plugins.value = plugins.value.filter(x => x.id !== p.id)
    } else {
      alert(data.message || '删除失败')
    }
  } catch (e) {
    alert('删除请求失败: ' + e)
  }
}

async function uploadZip(file) {
  if (!file || !file.name.endsWith('.zip')) {
    uploadMsg.value = '请选择 zip 文件'
    uploadOk.value = false
    return
  }
  const formData = new FormData()
  formData.append('file', file)
  try {
    const res = await fetch('/api/integrations/upload', {
      method: 'POST',
      body: formData,
      credentials: 'include',
    })
    const data = await res.json()
    if (data.success) {
      uploadMsg.value = `✓ ${data.data.name || data.data.id} 上传成功，重启 Aether 后生效`
      uploadOk.value = true
      await loadPlugins()
    } else {
      uploadMsg.value = `✗ ${data.message || '上传失败'}`
      uploadOk.value = false
    }
  } catch (e) {
    uploadMsg.value = '上传请求失败: ' + e
    uploadOk.value = false
  }
}

function handleFileSelect(e) {
  const file = e.target.files[0]
  if (file) uploadZip(file)
}

function handleDrop(e) {
  dragging.value = false
  const file = e.dataTransfer.files[0]
  if (file) uploadZip(file)
}

onMounted(loadPlugins)
</script>

<style scoped>
.plugin-page {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--space-lg, 24px);
}
.page-header h1 {
  margin: 0 0 4px;
}
.subtitle {
  color: var(--color-text-secondary, #888);
  margin: 0 0 24px;
  font-size: var(--text-sm, 14px);
}
.upload-section {
  margin-bottom: 24px;
}
.upload-zone {
  border: 2px dashed var(--color-border, #444);
  border-radius: var(--radius-md, 8px);
  padding: 24px;
  text-align: center;
  cursor: pointer;
  transition: border-color 0.2s;
}
.upload-zone.dragging {
  border-color: var(--color-primary, #4a9eff);
  background: rgba(74, 158, 255, 0.05);
}
.upload-hint {
  color: var(--color-text-secondary, #888);
}
.upload-msg {
  margin-top: 8px;
  font-size: var(--text-sm, 14px);
}
.upload-msg.ok {
  color: var(--color-success, #2ecc71);
}
.plugin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 16px;
}
.plugin-card {
  background: var(--color-surface, #1e1e2e);
  border: 1px solid var(--color-border, #333);
  border-radius: var(--radius-md, 8px);
  padding: 16px;
}
.plugin-card.disabled {
  opacity: 0.6;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 4px;
}
.plugin-name {
  font-weight: 600;
}
.plugin-version {
  font-size: var(--text-xs, 12px);
  color: var(--color-text-secondary, #888);
}
.plugin-id {
  font-family: monospace;
  font-size: var(--text-xs, 12px);
  color: var(--color-text-secondary, #888);
  margin-bottom: 8px;
}
.plugin-desc {
  font-size: var(--text-sm, 14px);
  color: var(--color-text-secondary, #aaa);
  margin-bottom: 12px;
}
.plugin-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 12px;
}
.badge {
  font-size: var(--text-xs, 12px);
  padding: 2px 8px;
  border-radius: var(--radius-full, 999px);
  background: rgba(255, 255, 255, 0.08);
}
.badge.alive {
  background: rgba(46, 204, 113, 0.15);
  color: var(--color-success, #2ecc71);
}
.badge.dead {
  background: rgba(231, 76, 60, 0.15);
  color: #e74c3c;
}
.cap-badge {
  font-size: var(--text-xs, 12px);
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(74, 158, 255, 0.1);
  color: var(--color-primary, #4a9eff);
}
.card-actions {
  display: flex;
  gap: 8px;
}
.action-btn {
  flex: 1;
  padding: 6px 10px;
  border: 1px solid var(--color-border, #444);
  border-radius: var(--radius-sm, 6px);
  background: var(--color-surface-hover, #2a2a3e);
  color: var(--color-text, #eee);
  cursor: pointer;
  font-size: var(--text-sm, 14px);
}
.action-btn:hover {
  border-color: var(--color-primary, #4a9eff);
}
.action-btn.danger:hover {
  border-color: #e74c3c;
}
.action-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.loading, .empty {
  text-align: center;
  color: var(--color-text-secondary, #888);
  padding: 48px;
}
.back-btn {
  margin-top: 32px;
  padding: 8px 16px;
  border: 1px solid var(--color-border, #444);
  border-radius: var(--radius-sm, 6px);
  background: transparent;
  color: var(--color-text, #eee);
  cursor: pointer;
}

/* ============ 详情/配置弹窗 ============ */
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.modal {
  width: min(520px, 92vw);
  max-height: 84vh;
  overflow-y: auto;
  background: var(--color-surface, #1e1e2e);
  border: 1px solid var(--color-border, #333);
  border-radius: var(--radius-md, 10px);
  padding: 20px;
}
.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 8px;
}
.modal-title {
  margin: 0;
  font-size: 18px;
}
.modal-close {
  background: none;
  border: none;
  color: var(--color-text-secondary, #888);
  font-size: 22px;
  cursor: pointer;
  line-height: 1;
}
.modal-desc {
  color: var(--color-text-secondary, #aaa);
  font-size: var(--text-sm, 14px);
  margin: 8px 0 12px;
}
.modal-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 16px;
}
.config-badge {
  background: rgba(46, 204, 113, 0.15);
  color: var(--color-success, #2ecc71);
}
.config-section {
  border-top: 1px solid var(--color-border, #333);
  padding-top: 14px;
}
.config-title {
  margin: 0 0 12px;
  font-size: 15px;
}
.config-loading {
  color: var(--color-text-secondary, #888);
  padding: 12px 0;
}
.config-field {
  margin-bottom: 12px;
}
.field-label {
  display: block;
  font-size: var(--text-sm, 14px);
  margin-bottom: 4px;
}
.required {
  color: #e74c3c;
}
.field-input {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--color-border, #444);
  border-radius: var(--radius-sm, 6px);
  background: var(--color-surface-hover, #2a2a3e);
  color: var(--color-text, #eee);
  font-size: var(--text-sm, 14px);
}
.field-hint {
  margin-top: 4px;
  font-size: var(--text-xs, 12px);
  color: var(--color-text-secondary, #888);
}
.config-error {
  color: #e74c3c;
  font-size: var(--text-sm, 14px);
  margin: 8px 0;
}
.config-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}
.save-btn {
  padding: 8px 18px;
  border: none;
  border-radius: var(--radius-sm, 6px);
  background: var(--color-primary, #4a9eff);
  color: #fff;
  cursor: pointer;
  font-size: var(--text-sm, 14px);
}
.save-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.saved-hint {
  color: var(--color-success, #2ecc71);
  font-size: var(--text-sm, 14px);
}
.config-none {
  border-top: 1px solid var(--color-border, #333);
  padding-top: 14px;
  color: var(--color-text-secondary, #888);
  font-size: var(--text-sm, 14px);
}
.modal-footer {
  border-top: 1px solid var(--color-border, #333);
  margin-top: 16px;
  padding-top: 12px;
  display: flex;
  justify-content: flex-end;
}
</style>
