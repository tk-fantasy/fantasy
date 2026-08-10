<script setup>
/**
 * 对话式弹窗 — 用于「自动化规则」和「定时任务」。
 *
 * 两种模式：
 * - plan（默认）：只读 Q&A。问 AI 这条规则/任务现在是怎么配置的，AI 用人话解释。
 *   典型问题「这个任务是执行一次还是每天跑？」「这条规则什么时候触发？」
 * - modify：对话式修改。自然语言说怎么改 → LLM 输出预览 → 满意后「应用修改」落库。
 *
 * 后端无状态：explain 端点只读解释，revise 端点只做 LLM 推理不落库，update 端点才写库。
 */
import { ref, computed, watch, nextTick } from 'vue'
import { apiPost, apiPut } from '../utils/api'

const props = defineProps({
  kind: { type: String, required: true, validator: (v) => v === 'rule' || v === 'task' },
  itemId: { type: String, required: true },
  initial: { type: Object, required: true }, // 当前规则/任务的完整对象
})
const emit = defineEmits(['applied', 'close'])

// 模式：plan（只读解释）/ modify（修改）。默认 plan，先理解再动手。
const mode = ref('plan')
// 待应用的 JSON：初始深拷贝 props.initial，每轮 revise 更新它
const pendingJson = ref(JSON.parse(JSON.stringify(props.initial)))
const messages = ref([]) // { role: 'user'|'assistant', content, error? }
const inputText = ref('')
const loading = ref(false)
const hasRevision = ref(false) // 至少成功 revise 一次后亮起「应用修改」
const applying = ref(false)
const scrollRef = ref(null)
const showJson = ref(false) // 折叠的原始 JSON 视图

const isRule = computed(() => props.kind === 'rule')

// plan 模式的建议问题（点击即问）
const suggestedQuestions = computed(() => {
  if (isRule.value) {
    return [
      '这条规则什么时候会触发？',
      '触发后会做什么？',
      '冷却时间是多久？',
    ]
  }
  return [
    '这个任务是执行一次还是每天重复？',
    '下次什么时候执行？',
    '触发时会做什么？',
  ]
})

// 切换模式：plan 模式保留消息历史（问答上下文）；modify 模式保留修改历史。
// 两者消息分开存，切换时不互清，避免来回切丢失上下文。
const planMessages = ref([])
const modifyMessages = ref([])
watch(mode, (m) => {
  // 把当前 messages 存回对应历史，切到另一边时恢复
  if (m === 'plan') {
    modifyMessages.value = messages.value
    messages.value = planMessages.value
  } else {
    planMessages.value = messages.value
    messages.value = modifyMessages.value
  }
  inputText.value = ''
  scrollToBottom()
})

// 顶部摘要（随 pendingJson 变化刷新；plan 模式始终显示当前真实配置）
const summaryParts = computed(() => {
  if (isRule.value) {
    const r = pendingJson.value
    return [
      { label: '如果', value: formatCondition(r.condition) },
      { label: '则', value: formatActionsShort(r.actions, r.action_descriptions) },
    ]
  }
  const t = pendingJson.value
  return [
    { label: '触发', value: formatSchedule(t.schedule) },
    { label: '执行', value: formatPayload(t.payload) },
  ]
})

async function scrollToBottom() {
  await nextTick()
  if (scrollRef.value) scrollRef.value.scrollTop = scrollRef.value.scrollHeight
}

async function sendInstruction() {
  const text = inputText.value.trim()
  if (!text || loading.value) return
  messages.value.push({ role: 'user', content: text })
  inputText.value = ''
  loading.value = true
  messages.value.push({ role: 'assistant', content: '', loading: true })
  await scrollToBottom()

  if (mode.value === 'plan') {
    await callExplain(text)
  } else {
    await callRevise(text)
  }
}

// plan 模式：只读解释
async function callExplain(question) {
  const url = isRule.value
    ? `/api/rules/${props.itemId}/explain`
    : `/api/scheduled-tasks/${props.itemId}/explain`
  try {
    const result = await apiPost(url, { current: pendingJson.value, question })
    messages.value[messages.value.length - 1] = { role: 'assistant', content: result.answer || '(无回复)' }
  } catch (e) {
    messages.value[messages.value.length - 1] = {
      role: 'assistant',
      content: `❌ 解释失败：${e.message || e}`,
      error: true,
    }
  } finally {
    loading.value = false
    await scrollToBottom()
  }
}

// modify 模式：迭代修改
async function callRevise(instruction) {
  const url = isRule.value
    ? `/api/rules/${props.itemId}/revise`
    : `/api/scheduled-tasks/${props.itemId}/revise`
  try {
    const result = await apiPost(url, { instruction, current: pendingJson.value })
    const updated = isRule.value ? result.rule : result.task
    const summary = result.summary || '已更新'
    if (updated) {
      pendingJson.value = { ...pendingJson.value, ...updated }
      hasRevision.value = true
    }
    messages.value[messages.value.length - 1] = { role: 'assistant', content: `✅ ${summary}` }
  } catch (e) {
    messages.value[messages.value.length - 1] = {
      role: 'assistant',
      content: `❌ 修改失败：${e.message || e}`,
      error: true,
    }
  } finally {
    loading.value = false
    await scrollToBottom()
  }
}

function askSuggested(q) {
  if (loading.value) return
  inputText.value = q
  sendInstruction()
}

async function applyChanges() {
  if (!hasRevision.value || applying.value) return
  applying.value = true
  const url = isRule.value
    ? `/api/rules/${props.itemId}`
    : `/api/scheduled-tasks/${props.itemId}`
  const body = isRule.value
    ? { rule: pendingJson.value }
    : { task: pendingJson.value }
  try {
    const updated = await apiPut(url, body)
    emit('applied', updated)
  } catch (e) {
    alert('保存失败：' + (e.message || e))
  } finally {
    applying.value = false
  }
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendInstruction()
  }
}

// ===== 摘要格式化（从 TaskView / ScheduledTasksView 复刻，保持自包含）=====

function formatCondition(condition) {
  if (!condition) return '—'
  if (typeof condition === 'string') return condition
  if (condition.description) return condition.description
  if (condition.type) return condition.type
  return JSON.stringify(condition)
}

// 优先用 LLM 生成的中文描述(action_descriptions,如"关闭大门"),
// 缺了才从 actions 的 entity_id 解析。entity_id 常是机器拼音/ID 乱码。
function formatActionsShort(actions, descriptions) {
  if (!actions || !actions.length) {
    // 没有动作,但有描述也显示描述
    if (Array.isArray(descriptions) && descriptions.length) return descriptions.join('，')
    return '—'
  }
  const descs = Array.isArray(descriptions) ? descriptions : []
  return actions.map((a, idx) => {
    if (descs[idx]) return descs[idx]
    return formatSingleAction(a)
  }).join('，')
}

function formatSingleAction(action) {
  if (!action) return ''
  if (typeof action === 'string') return action
  const ti = action.mcp_tool_input || action
  const eid = ti.entity_id || ''
  const name = (eid.split('.')[1] || eid).replace(/_/g, ' ')
  const svc = ti.service || ''
  const map = {
    turn_on: '打开', turn_off: '关闭',
    open_cover: '打开', close_cover: '关闭',
    set_temperature: '设置温度', set_brightness: '设置亮度',
  }
  return `${name} ${map[svc] || svc}`
}

function formatSchedule(schedule) {
  if (!schedule) return '—'
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
  if (!payload) return '—'
  if (payload.kind === 'reminder') return `提醒：${payload.original || payload.intent || ''}`
  if (payload.kind === 'message') return `发消息：${payload.message || ''}`
  if (payload.kind === 'tool') {
    const input = payload.tool_input || {}
    return `调用工具 ${payload.tool_name}${input.entity_id ? ' · ' + input.entity_id : ''}`
  }
  return JSON.stringify(payload)
}
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div class="revise-overlay" @click.self="emit('close')">
        <div class="revise-container">
          <div class="revise-header">
            <h2>{{ isRule ? '规则详情' : '任务详情' }}</h2>
            <div class="header-actions">
              <button class="btn-toggle-json" :class="{ active: showJson }" @click="showJson = !showJson">
                {{ showJson ? '收起 JSON' : '查看 JSON' }}
              </button>
              <button class="btn-close" @click="emit('close')">&times;</button>
            </div>
          </div>

          <!-- 模式切换：plan（解释） / modify（修改） -->
          <div class="mode-switch">
            <button class="mode-btn" :class="{ active: mode === 'plan' }" @click="mode = 'plan'">
              💡 了解 (Plan)
            </button>
            <button class="mode-btn" :class="{ active: mode === 'modify' }" @click="mode = 'modify'">
              ✏️ 修改 (Modify)
            </button>
          </div>

          <!-- 折叠的原始 JSON 视图 -->
          <div v-if="showJson" class="json-panel">
            <pre class="json-view">{{ JSON.stringify(pendingJson, null, 2) }}</pre>
          </div>

          <!-- 顶部：当前项可读摘要（随 pendingJson 刷新） -->
          <div class="revise-summary">
            <div v-for="part in summaryParts" :key="part.label" class="summary-row">
              <span class="summary-label">{{ part.label }}</span>
              <span class="summary-value">{{ part.value }}</span>
            </div>
          </div>

          <!-- 中部：对话消息列表 -->
          <div ref="scrollRef" class="revise-messages">
            <!-- plan 模式：建议问题 chip -->
            <div v-if="mode === 'plan' && !messages.length" class="empty-hint">
              <p class="hint-title">了解这条{{ isRule ? '规则' : '任务' }}现在是怎么配置的，可以问：</p>
              <div class="chip-row">
                <button
                  v-for="q in suggestedQuestions"
                  :key="q"
                  class="suggestion-chip"
                  :disabled="loading"
                  @click="askSuggested(q)"
                >{{ q }}</button>
              </div>
              <p class="hint-sub">或直接在下方输入你的问题</p>
            </div>
            <div v-else-if="mode === 'modify' && !messages.length" class="empty-hint">
              用自然语言告诉我要怎么改，例如：<br>
              <template v-if="isRule">
                「把条件改成下雨天」「再加一个关窗帘的动作」「冷却时间改成 60 秒」
              </template>
              <template v-else>
                「改成每天早上8点」「把提醒内容改成下班」「改成每30分钟一次」
              </template>
            </div>
            <div
              v-for="(msg, i) in messages"
              :key="i"
              class="msg-bubble"
              :class="msg.role + (msg.error ? ' error' : '')"
            >
              <span v-if="msg.loading" class="typing">思考中<span class="dots">...</span></span>
              <span v-else>{{ msg.content }}</span>
            </div>
          </div>

          <!-- 底部：输入框 + 操作按钮 -->
          <div class="revise-footer">
            <div class="input-row">
              <input
                v-model="inputText"
                class="revise-input"
                :placeholder="mode === 'plan'
                  ? (isRule ? '问点什么来了解这条规则…' : '问点什么来了解这个任务…')
                  : (isRule ? '描述你想怎么改这条规则…' : '描述你想怎么改这个任务…')"
                :disabled="loading"
                @keydown="onKeydown"
              />
              <button class="btn-send" :disabled="!inputText.trim() || loading" @click="sendInstruction">
                {{ loading ? '…' : '发送' }}
              </button>
            </div>
            <div class="action-row">
              <span class="hint">
                {{ mode === 'plan'
                  ? '只读问答，不会改动配置'
                  : (hasRevision ? '预览已更新，确认无误后应用' : '先对话修改，再应用') }}
              </span>
              <div class="action-btns">
                <button class="btn-cancel" @click="emit('close')">关闭</button>
                <button
                  v-if="mode === 'modify'"
                  class="btn-apply"
                  :disabled="!hasRevision || applying"
                  @click="applyChanges"
                >
                  {{ applying ? '保存中…' : '应用修改' }}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.revise-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-12);
}

.revise-container {
  position: relative;
  isolation: isolate;
  background: var(--dialog-bg-glass);
  -webkit-backdrop-filter: blur(12px);
  backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-3xl);
  width: 100%;
  max-width: 720px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xl);
  overflow: hidden;
}

.revise-container::before {
  content: '';
  position: absolute;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(ellipse 60% 50% at 25% 30%, var(--g1) 0%, transparent 50%),
    radial-gradient(ellipse 50% 45% at 70% 25%, var(--g2) 0%, transparent 45%),
    radial-gradient(ellipse 55% 50% at 45% 70%, var(--g3) 0%, transparent 45%),
    radial-gradient(ellipse 50% 45% at 60% 50%, var(--g4) 0%, transparent 45%),
    radial-gradient(ellipse 45% 40% at 35% 55%, var(--g5) 0%, transparent 40%);
  background-size: 300% 300%, 300% 300%, 300% 300%, 300% 300%, 300% 300%;
  animation: auroraFlow 12s cubic-bezier(0.45, 0, 0.55, 1) infinite;
  opacity: 0.9;
  will-change: background-position;
}

.revise-container > * {
  position: relative;
  z-index: 1;
}

.light-mode .revise-container::before {
  opacity: 0.4;
}

.revise-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-10) var(--space-16);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}
.revise-header h2 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin: 0;
}
.btn-close {
  background: none;
  border: none;
  font-size: var(--text-2xl);
  color: var(--color-text-muted);
  cursor: pointer;
  line-height: 1;
  padding: 0 var(--space-2);
}
.btn-close:hover { color: var(--color-text); }

.header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-8);
}
.btn-toggle-json {
  padding: var(--space-3) var(--space-8);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.btn-toggle-json:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
}
.btn-toggle-json.active {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}

/* JSON 视图面板 */
.json-panel {
  padding: var(--space-6) var(--space-16);
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  flex-shrink: 0;
  max-height: 240px;
  overflow-y: auto;
}
.json-view {
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}

/* 模式切换 */
.mode-switch {
  display: flex;
  gap: var(--space-4);
  padding: var(--space-6) var(--space-16) 0;
  flex-shrink: 0;
}
.mode-btn {
  flex: 1;
  padding: var(--space-6) var(--space-10);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.mode-btn:hover { background: var(--color-surface-hover); }
.mode-btn.active {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}

/* plan 模式建议问题 chip */
.hint-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
  margin: 0 0 var(--space-6);
}
.hint-sub {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin: var(--space-6) 0 0;
}
.chip-row {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  align-items: stretch;
}
.suggestion-chip {
  padding: var(--space-5) var(--space-10);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
  transition: all var(--duration-fast) var(--ease-out);
}
.suggestion-chip:hover:not(:disabled) {
  background: var(--color-surface-hover);
  border-color: var(--color-primary);
}
.suggestion-chip:disabled { opacity: 0.5; cursor: not-allowed; }

/* 顶部摘要 */
.revise-summary {
  padding: var(--space-8) var(--space-16);
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}
.summary-row {
  display: flex;
  align-items: baseline;
  gap: var(--space-6);
}
.summary-label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  min-width: 40px;
  flex-shrink: 0;
}
.summary-value {
  font-size: var(--text-sm);
  color: var(--color-text);
  word-break: break-all;
}

/* 消息列表 */
.revise-messages {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-12) var(--space-16);
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
  min-height: 200px;
}
.empty-hint {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  text-align: center;
  padding: var(--space-16) var(--space-8);
  line-height: 1.8;
}
.msg-bubble {
  max-width: 80%;
  padding: var(--space-4) var(--space-8);
  border-radius: var(--radius-xl);
  font-size: var(--text-sm);
  line-height: 1.6;
  word-break: break-word;
  white-space: pre-wrap;
}
.msg-bubble.user {
  align-self: flex-end;
  background: var(--color-primary);
  color: #fff;
  border-bottom-right-radius: var(--radius-sm);
}
.msg-bubble.assistant {
  align-self: flex-start;
  background: var(--color-surface-hover);
  color: var(--color-text);
  border-bottom-left-radius: var(--radius-sm);
}
.msg-bubble.assistant.error {
  background: var(--color-error-bg, #3a1f1f);
  color: var(--color-error, #ff6b6b);
}
.typing { color: var(--color-text-muted); }
.typing .dots { animation: blink 1.2s infinite; }
@keyframes blink { 0%, 100% { opacity: 0.3; } 50% { opacity: 1; } }

/* 底部 */
.revise-footer {
  padding: var(--space-8) var(--space-16) var(--space-12);
  border-top: 1px solid var(--color-border);
  flex-shrink: 0;
}
.input-row {
  display: flex;
  gap: var(--space-6);
}
.revise-input {
  flex: 1;
  padding: var(--space-6) var(--space-10);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: var(--text-sm);
}
.revise-input:focus {
  outline: none;
  border-color: var(--color-primary);
}
.revise-input:disabled { opacity: 0.5; }

.btn-send {
  padding: var(--space-6) var(--space-12);
  border-radius: var(--radius-lg);
  border: none;
  background: var(--color-primary);
  color: #fff;
  font-size: var(--text-sm);
  cursor: pointer;
  white-space: nowrap;
}
.btn-send:disabled { opacity: 0.4; cursor: not-allowed; }

.action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: var(--space-6);
}
.action-row .hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}
.action-btns {
  display: flex;
  gap: var(--space-6);
}
.btn-cancel {
  padding: var(--space-5) var(--space-12);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}
.btn-cancel:hover { background: var(--color-surface-hover); }

.btn-apply {
  padding: var(--space-5) var(--space-12);
  border-radius: var(--radius-lg);
  border: none;
  background: var(--color-success, #34c759);
  color: #fff;
  font-size: var(--text-sm);
  cursor: pointer;
  font-weight: var(--weight-medium);
}
.btn-apply:disabled { opacity: 0.4; cursor: not-allowed; }

/* Modal Transition（与 AdvancedModal 一致） */
.modal-enter-active, .modal-leave-active { transition: opacity 0.3s var(--ease-out); }
.modal-enter-active .revise-container, .modal-leave-active .revise-container { transition: all 0.3s var(--ease-out); }
.modal-enter-from, .modal-leave-to { opacity: 0; }
.modal-enter-from .revise-container, .modal-leave-to .revise-container { transform: scale(0.95) translateY(20px); }
</style>
