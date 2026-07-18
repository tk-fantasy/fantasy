<script setup>
import { ref, computed, onMounted, watch } from 'vue'
import BaseToggle from '../components/BaseToggle.vue'
import EmojiPicker from '../components/EmojiPicker.vue'
import { apiGet, apiPost } from '../utils/api'

const tasks = ref([])
const loading = ref(true)
const showCreateForm = ref(false)
const creating = ref(false)

// Emoji 偏好管理
const emojiPrefs = ref({}) // { "scheduled_task:xxx": "🔔" }
const showEmojiPicker = ref(false)
const currentEmojiTarget = ref(null) // { scope, key, defaultEmoji }

async function loadEmojiPrefs() {
  try {
    const res = await fetch('/api/emoji/preferences')
    const json = await res.json()
    const prefs = {}
    for (const item of (json.data || [])) {
      prefs[`${item.scope}:${item.key}`] = item.emoji_char
    }
    emojiPrefs.value = prefs
  } catch (e) {
    console.error('Failed to load emoji prefs:', e)
  }
}

function openEmojiPicker(scope, key, defaultEmoji) {
  currentEmojiTarget.value = { scope, key, defaultEmoji }
  showEmojiPicker.value = true
}

async function onEmojiSelect(item) {
  if (!currentEmojiTarget.value) return
  const { scope, key } = currentEmojiTarget.value
  const prefKey = `${scope}:${key}`
  try {
    await fetch('/api/emoji/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, key, emoji_char: item.char }),
    })
    emojiPrefs.value[prefKey] = item.char
  } catch (e) {
    console.error('Failed to save emoji pref:', e)
  }
}

// 新建表单状态
const form = ref(emptyForm())

function emptyForm() {
  return {
    // 触发：自然语言翻译
    schedulePhrase: '',      // 用户输入的自然语言
    schedule: null,          // 翻译结果 {kind, ...}
    scheduleSummary: '',     // 翻译返回的人话摘要
    // 执行内容
    payloadKind: 'reminder', // reminder（提醒我）| message（我发消息）
    content: '',             // 提醒备注 或 要发的消息/指令
  }
}

// ===== 触发：自然语言 → schedule =====
const parsing = ref(false)
const parseError = ref('')

async function parsePhrase() {
  const phrase = (form.value.schedulePhrase || '').trim()
  if (!phrase) {
    parseError.value = '请先输入时间描述'
    return
  }
  parsing.value = true
  parseError.value = ''
  try {
    const data = await apiPost('/api/scheduled-tasks/parse-schedule', { phrase })
    form.value.schedule = data.schedule
    form.value.scheduleSummary = data.summary || ''
  } catch (e) {
    parseError.value = '翻译失败：' + (e.message || e)
    form.value.schedule = null
    form.value.scheduleSummary = ''
  } finally {
    parsing.value = false
  }
}

// 编辑时间描述时清掉旧翻译结果，避免不一致
watch(() => form.value.schedulePhrase, () => {
  form.value.schedule = null
  form.value.scheduleSummary = ''
  parseError.value = ''
})

function buildSchedule() {
  if (!form.value.schedule) throw new Error('请先点「翻译」确认触发时间')
  return { ...form.value.schedule }
}

// ===== 执行内容：两种纯消息模式 =====
function buildPayload() {
  const k = form.value.payloadKind
  const content = (form.value.content || '').trim()
  if (!content) throw new Error('请填写内容')
  // 提醒我：走 reminder 链路，绕开 ReAct，AI 主动开口说一句话（不碰工具/不建任务）
  // 我发消息：走 message 链路，原样注入 dispatch，AI 按 ReAct 执行（开灯/查新闻/…）
  if (k === 'reminder') {
    return { kind: 'reminder', intent: content, original: content }
  }
  return { kind: 'message', message: content }
}

// ===== 任务 CRUD =====
async function loadTasks() {
  try {
    loading.value = true
    const data = await apiGet('/api/scheduled-tasks')
    tasks.value = (data || []).map((t) => ({ ...t, enabled: t.enabled !== false }))
  } catch (e) {
    console.error('Failed to load scheduled tasks:', e)
  } finally {
    loading.value = false
  }
}

async function createTask() {
  creating.value = true
  try {
    const schedule = buildSchedule()
    const payload = buildPayload()
    const task = await apiPost('/api/scheduled-tasks', {
      schedule,
      payload,
      enabled: true,
    })
    tasks.value.unshift({ ...task, enabled: task.enabled !== false })
    form.value = emptyForm()
    showCreateForm.value = false
  } catch (e) {
    console.error('Failed to create task:', e)
    alert('创建失败：' + (e.message || e))
  } finally {
    creating.value = false
  }
}

async function toggleTask(id) {
  const task = tasks.value.find((t) => t.id === id)
  if (!task) return
  const newVal = !task.enabled
  try {
    const updated = await apiPost(`/api/scheduled-tasks/${id}/enabled`, { enabled: newVal })
    task.enabled = newVal
    if (updated?.next_run_at !== undefined) task.next_run_at = updated.next_run_at
  } catch (e) {
    console.error('Failed to toggle task:', e)
    alert('操作失败，请重新登录')
  }
}

async function deleteTask(id) {
  if (!confirm('确定删除这个定时任务吗？')) return
  try {
    const res = await fetch(`/api/scheduled-tasks/${id}`, { method: 'DELETE', credentials: 'include' })
    if (!res.ok) {
      alert('删除失败')
      return
    }
    tasks.value = tasks.value.filter((t) => t.id !== id)
  } catch (e) {
    console.error('Failed to delete task:', e)
  }
}

async function runTaskNow(id) {
  try {
    await apiPost(`/api/scheduled-tasks/${id}/run`)
    alert('已手动触发，结果见后端日志')
    await loadTasks()
  } catch (e) {
    console.error('Failed to run task:', e)
    alert('触发失败：' + e.message)
  }
}

// ===== 卡片展示 =====
function formatSchedule(schedule) {
  if (!schedule) return ''
  const k = schedule.kind
  if (k === 'at') return `于 ${(schedule.at || '').replace('T', ' ')} 执行一次`
  if (k === 'every') {
    const s = Number(schedule.every_seconds || 0)
    if (s >= 86400 && s % 86400 === 0) return `每 ${s / 86400} 天`
    if (s >= 3600 && s % 3600 === 0) return `每 ${s / 3600} 小时`
    if (s >= 60 && s % 60 === 0) return `每 ${s / 60} 分钟`
    return `每 ${s} 秒`
  }
  if (k === 'cron') return `cron: ${schedule.expr}`
  return JSON.stringify(schedule)
}

function formatPayload(payload) {
  if (!payload) return ''
  if (payload.kind === 'reminder') {
    // reminder 链路：绕开 ReAct，AI 主动开口提醒
    return `提醒：${payload.original || payload.intent || ''}`
  }
  if (payload.kind === 'message') {
    return `发消息：${payload.message || ''}`
  }
  if (payload.kind === 'tool') {
    const input = payload.tool_input || {}
    return `调用工具 ${payload.tool_name}${input.entity_id ? ' · ' + input.entity_id : ''}`
  }
  return JSON.stringify(payload)
}

function formatTime(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
}

function statusText(task) {
  return task.last_status || '未运行'
}

function statusClass(task) {
  const s = task.last_status
  if (s === 'success') return 'ok'
  if (s === 'failed' || s === 'interrupted') return 'err'
  if (s === 'running') return 'running'
  return ''
}

// 任务图标：默认按 payload 推断，用户可覆盖
function getDefaultTaskIcon(task) {
  const p = task.payload
  if (!p) return '⏰'
  if (p.kind === 'reminder') return '🔔'
  if (p.kind === 'message') return '📤'
  if (p.kind === 'tool') return '⚡'
  return '⏰'
}

function getTaskIcon(task) {
  const prefKey = `scheduled_task:${task.id}`
  return emojiPrefs.value[prefKey] || getDefaultTaskIcon(task)
}

const enabledCount = computed(() => tasks.value.filter((t) => t.enabled).length)
const scheduleSummary = computed(() => form.value.scheduleSummary || '（未翻译）')
const previewPayload = computed(() => {
  try { return formatPayload(buildPayload()) } catch (e) { return e.message }
})

onMounted(() => {
  loadTasks()
  loadEmojiPrefs()
})
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div>
        <h1>定时任务</h1>
        <p class="page-sub">{{ enabledCount }} 个任务启用中</p>
      </div>
      <button class="btn-add" @click="showCreateForm = !showCreateForm">
        {{ showCreateForm ? '取消' : '+ 新建任务' }}
      </button>
    </header>

    <!-- 创建表单 -->
    <div v-if="showCreateForm" class="create-form">
      <!-- 触发方��：自然语言翻译 -->
      <div class="form-row">
        <label class="form-label">触发时间（自然语言）</label>
        <div class="parse-row">
          <input v-model="form.schedulePhrase" class="form-input"
                 placeholder="每天早上8点 / 明天10点 / 每30分钟 / 工作日下午5点半"
                 @keyup.enter="parsePhrase" />
          <button class="btn-parse" :disabled="parsing" @click="parsePhrase">
            {{ parsing ? '翻译中…' : 'AI 翻译' }}
          </button>
        </div>
        <span v-if="parseError" class="form-hint form-hint--err">{{ parseError }}</span>
        <span v-else-if="form.scheduleSummary" class="form-hint form-hint--ok">
          ✓ {{ form.scheduleSummary }}
          <details class="raw-schedule"><summary>查看原始配置</summary>
            <code>{{ JSON.stringify(form.schedule) }}</code>
          </details>
        </span>
      </div>

      <!-- 执行内容：提醒我 / 我发消息 -->
      <div class="form-row">
        <label class="form-label">执行内容</label>
        <div class="seg-group">
          <button :class="['seg', form.payloadKind === 'reminder' && 'active']" @click="form.payloadKind = 'reminder'">提醒我</button>
          <button :class="['seg', form.payloadKind === 'message' && 'active']" @click="form.payloadKind = 'message'">我发消息</button>
        </div>
      </div>

      <div class="form-row">
        <label class="form-label">
          {{ form.payloadKind === 'reminder' ? '提醒内容' : '消息 / 指令' }}
        </label>
        <input v-model="form.content" class="form-input"
               :placeholder="form.payloadKind === 'reminder' ? '该起床了' : '打开厨房灯 / 查今天新闻'" />
        <span class="form-hint">
          {{ form.payloadKind === 'reminder'
            ? '到点 AI 会主动提醒你这个内容'
            : '到点 AI 会按这句话执行（控制设备、查信息、或任何它能做的事）' }}
        </span>
      </div>

      <div class="preview-box">
        <span class="preview-label">预览</span>
        <span class="preview-text">{{ scheduleSummary }} · {{ previewPayload }}</span>
      </div>

      <div class="form-actions">
        <button class="btn-create" :disabled="creating" @click="createTask">
          {{ creating ? '创建中...' : '创建任务' }}
        </button>
      </div>
    </div>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="task-list">
      <div v-for="task in tasks" :key="task.id" class="task-card">
        <div class="task-toggle-col">
          <BaseToggle :modelValue="task.enabled" @update:modelValue="toggleTask(task.id)" />
        </div>

        <div class="task-body">
          <div class="task-header">
            <span
              class="task-emoji emoji-trigger"
              :title="'点击更换图标'"
              @click="openEmojiPicker('scheduled_task', task.id, getTaskIcon(task))"
            >{{ getTaskIcon(task) }}</span>
            <h3>{{ task.name }}</h3>
            <span class="task-status" :class="statusClass(task)">{{ statusText(task) }}</span>
          </div>
          <div class="task-meta">
            <div class="meta-row">
              <span class="meta-label">触发</span>
              <span class="meta-value">{{ formatSchedule(task.schedule) }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">执行</span>
              <span class="meta-value">{{ formatPayload(task.payload) }}</span>
            </div>
            <div class="meta-row">
              <span class="meta-label">下次</span>
              <span class="meta-value">{{ formatTime(task.next_run_at) }}</span>
            </div>
            <div v-if="task.last_run_at" class="meta-row">
              <span class="meta-label">上次</span>
              <span class="meta-value">{{ formatTime(task.last_run_at) }}</span>
            </div>
            <div v-if="task.last_error" class="meta-row error">
              <span class="meta-label">错误</span>
              <span class="meta-value">{{ task.last_error }}</span>
            </div>
          </div>
          <div class="task-actions">
            <button class="btn-run" @click="runTaskNow(task.id)" title="立即执行一次">立即执行</button>
            <button class="btn-delete" @click="deleteTask(task.id)" title="删除">删除</button>
          </div>
        </div>
      </div>

      <div v-if="tasks.length === 0" class="empty-state empty-state--card">
        暂无定时任务，点击右上角创建第一个任务
      </div>
    </div>

    <EmojiPicker
      :visible="showEmojiPicker"
      @update:visible="showEmojiPicker = $event"
      @select="onEmojiSelect"
    />
  </div>
</template>

<style scoped>
.loading-state {
  color: var(--color-text-muted);
}

.create-form {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  border: 1px solid var(--color-border);
  padding: var(--space-14);
  margin-bottom: var(--space-20);
}

.form-row {
  margin-bottom: var(--space-10);
}

.form-label {
  display: block;
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-3);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.form-input {
  width: 100%;
  padding: var(--space-4) var(--space-8);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  font-size: var(--text-base);
  font-family: inherit;
  outline: none;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  transition: border-color var(--duration-normal) var(--ease-out);
  box-sizing: border-box;
}

.form-input:focus {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.1);
}

.form-input::placeholder {
  color: var(--color-text-muted);
}

.parse-row {
  display: flex;
  gap: var(--space-4);
  align-items: stretch;
}

.parse-row .form-input {
  flex: 1;
  min-width: 0;
}

.btn-parse {
  flex-shrink: 0;
  padding: var(--space-4) var(--space-10);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  background: rgba(74, 124, 112, 0.1);
  color: var(--color-primary);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  white-space: nowrap;
}

.btn-parse:hover:not(:disabled) {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
}

.btn-parse:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.form-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin-top: var(--space-2);
  display: block;
}

.form-hint--err { color: var(--color-danger); }
.form-hint--ok { color: var(--color-primary); }

.raw-schedule {
  margin-top: var(--space-2);
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.raw-schedule summary {
  cursor: pointer;
  user-select: none;
}

.raw-schedule code {
  display: block;
  margin-top: var(--space-2);
  padding: var(--space-2) var(--space-4);
  background: rgba(255, 255, 255, 0.04);
  border-radius: var(--radius-sm);
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  word-break: break-all;
}

.seg-group {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.seg {
  padding: var(--space-3) var(--space-8);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.seg.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.preview-box {
  display: flex;
  gap: var(--space-4);
  align-items: baseline;
  background: rgba(74, 124, 112, 0.06);
  border: 1px dashed var(--color-border-hover);
  border-radius: var(--radius-md);
  padding: var(--space-4) var(--space-8);
  margin: var(--space-8) 0;
  font-size: var(--text-sm);
}

.preview-label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  flex-shrink: 0;
}

.preview-text {
  color: var(--color-text-secondary);
  word-break: break-word;
}

.form-actions {
  margin-top: var(--space-12);
}

.btn-create {
  background: var(--color-primary);
  color: #fff;
  border: none;
  padding: var(--space-5) var(--space-16);
  border-radius: var(--radius-lg);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.btn-create:hover:not(:disabled) {
  background: var(--color-primary-hover);
  transform: translateY(-1px);
}

.btn-create:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.task-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.task-card {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  padding: var(--space-14);
  border: 1px solid var(--color-border);
  display: flex;
  gap: var(--space-10);
  transition: all var(--duration-normal) var(--ease-out);
}

.task-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-active);
}

.task-toggle-col {
  padding-top: var(--space-2);
  flex-shrink: 0;
}

.task-body {
  flex: 1;
  min-width: 0;
}

.task-header {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  margin-bottom: var(--space-8);
}

.task-header h3 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-emoji {
  font-size: var(--text-xl);
  flex-shrink: 0;
  cursor: pointer;
  line-height: 1;
  border-radius: var(--radius-sm);
  padding: var(--space-2);
  transition: transform var(--duration-fast) var(--ease-out),
              background var(--duration-fast) var(--ease-out);
}

.task-emoji:hover {
  transform: scale(1.2);
  background: var(--color-surface-hover);
}

.task-status {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-sm);
  font-weight: var(--weight-medium);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.task-status.ok { background: var(--color-primary-light); color: var(--color-primary); }
.task-status.err { background: var(--color-danger-bg); color: var(--color-danger); }
.task-status.running { background: var(--color-info-bg); color: var(--color-info); }

.task-meta {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  margin-bottom: var(--space-6);
}

.meta-row {
  display: flex;
  gap: var(--space-5);
  font-size: var(--text-sm);
  line-height: 1.5;
}

.meta-row.error .meta-value { color: var(--color-danger); }

.meta-label {
  width: 48px;
  flex-shrink: 0;
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding-top: 2px;
}

.meta-value {
  color: var(--color-text-secondary);
  word-break: break-word;
}

.task-actions {
  display: flex;
  gap: var(--space-4);
}

.btn-run {
  background: rgba(74, 124, 112, 0.1);
  color: var(--color-primary);
  border: 1px solid rgba(74, 124, 112, 0.2);
  padding: var(--space-2) var(--space-8);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.btn-run:hover {
  background: var(--color-primary-light);
}

.btn-delete {
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  cursor: pointer;
  padding: var(--space-2) var(--space-4);
  transition: color var(--duration-fast) var(--ease-out);
}

.btn-delete:hover {
  color: var(--color-danger);
}

@media (max-width: 768px) {
  .task-card {
    flex-direction: column;
  }
}
</style>
