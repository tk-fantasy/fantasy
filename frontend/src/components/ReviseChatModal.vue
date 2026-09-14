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
 *
 * 第三种形态 —— pending 模式（传了 pendingId 即启用）：
 * 规则还是聊天里刚解析出的**草稿**（存在会话内存里，10 分钟 TTL），没有 rule_id。
 * 底部换成「确认创建 / 取消创建」，确认走 /api/rules/pending/{id}/confirm 直接落库、
 * 绕过模型 —— 用户点了就生效，不需要再回聊天里打字说「确认」。
 * explain/revise 也改走 pending 端点，规则由后端从草稿取，所以不传 current。
 */
import { ref, computed, watch, nextTick } from 'vue'
import { apiPost, apiPut } from '../utils/api'

const props = defineProps({
  kind: { type: String, required: true, validator: (v) => v === 'rule' || v === 'task' },
  // pending 模式下草稿还没 rule_id，故非必填
  itemId: { type: String, default: '' },
  initial: { type: Object, required: true }, // 当前规则/任务的完整对象
  pendingId: { type: String, default: '' },  // 非空即 pending 模式
  sessionId: { type: String, default: '' },  // 草稿按会话存，pending 端点必带
  // 可选摄像头列表（[{id, name, enabled}]），由调用方从 useCamera() 传入。
  // pending 模式下视觉规则要绑一路，选择器就渲染在这里。
  cameras: { type: Array, default: () => [] },
})
const emit = defineEmits(['applied', 'close', 'confirmed', 'cancelled', 'expired'])

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
const actionError = ref('')  // 确认/取消失败原因，显示在弹窗内（不用 alert）

// 摄像头选择：null=还没选（视觉规则时挡住确认），''=显式选「全部摄像头（全局）」
const selectedCamera = ref(
  String(props.initial?.camera_id || '').trim() || null)

const isRule = computed(() => props.kind === 'rule')
const isPending = computed(() => !!props.pendingId)
// 与后端 PENDING_TTL_SECONDS 一致；revise 会重置计时，所以这里只给个提示不做倒计时
const EXPIRE_MINUTES = 10

// 自动匹配明细：用户说的设备不存在时，后端已强制替换为最接近的真实设备
// （rule.auto_corrections）。横幅亮出来让用户核对——不提示的话，用户核对的
// 只是系统的猜测，替换就失去意义了。revise 会带新规则回来，跟随 pendingJson 刷新。
const autoCorrections = computed(() => {
  const list = pendingJson.value?.auto_corrections
  return Array.isArray(list) ? list : []
})

// type 缺失/非法一律按视觉处理 —— 与后端 pending_rules.is_vision_rule 和
// utils/ruleMismatch.js 同口径。从 pendingJson 现算而不是收 needsCamera prop：
// revise 把规则改成天气/定时后选择器要自己消失，prop 会滞后一轮。
const isVisionRule = computed(() => {
  const t = String(pendingJson.value?.type || '').trim().toLowerCase()
  return t !== 'time' && t !== 'weather'
})
const showCameraPicker = computed(() => isPending.value && isVisionRule.value)
// 视觉规则必须显式做一次选择（选某一路或选全局），没选就不许确认。
// 后端 confirm 也守同一条不变量，这里只是别让用户白点一次。
const cameraChoiceMissing = computed(() => showCameraPicker.value && selectedCamera.value === null)
const isGlobalChoice = computed(() => showCameraPicker.value && selectedCamera.value === '')

const cameraChoices = computed(() => [
  { id: '', name: '全部摄像头（全局）' },
  ...props.cameras
    .filter((c) => c && c.enabled !== false)
    .map((c) => ({ id: c.id, name: c.name || c.id })),
])

// revise 可能改掉 type 或 camera_id（rule_service._resolve_revised_camera 会在
// 转成非视觉时清空绑定），把选择器同步到新状态
watch(() => pendingJson.value?.camera_id, (cam) => {
  if (!showCameraPicker.value) return
  const next = String(cam || '').trim()
  // 只在草稿真的换了绑定时跟随；用户手动选了全局('')而草稿仍是旧值时不要抢回去
  if (next && next !== selectedCamera.value) selectedCamera.value = next
})

const modalTitle = computed(() => {
  if (isPending.value) return '确认创建规则'
  return isRule.value ? '规则详情' : '任务详情'
})

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
  // pending 草稿走独立端点：规则由后端从会话草稿里取，不传 current
  const url = isPending.value
    ? `/api/rules/pending/${props.pendingId}/explain`
    : (isRule.value
      ? `/api/rules/${props.itemId}/explain`
      : `/api/scheduled-tasks/${props.itemId}/explain`)
  const body = isPending.value
    ? { session_id: props.sessionId, question }
    : { current: pendingJson.value, question }
  try {
    const result = await apiPost(url, body)
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
  const url = isPending.value
    ? `/api/rules/pending/${props.pendingId}/revise`
    : (isRule.value
      ? `/api/rules/${props.itemId}/revise`
      : `/api/scheduled-tasks/${props.itemId}/revise`)
  const body = isPending.value
    ? { session_id: props.sessionId, instruction }
    : { instruction, current: pendingJson.value }
  try {
    const result = await apiPost(url, body)
    const updated = isRule.value ? result.rule : result.task
    const summary = result.summary || '已更新'
    if (updated) {
      pendingJson.value = { ...pendingJson.value, ...updated }
      hasRevision.value = true
    }
    actionError.value = ''
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

// ===== pending 模式：确认 / 取消（直接落库，不经模型）=====

async function confirmPending() {
  if (applying.value) return
  applying.value = true
  actionError.value = ''
  const body = { session_id: props.sessionId }
  // 只有视觉规则才传 camera_id：'' 是「全部摄像头（全局）」的显式选择，
  // 非视觉规则不传，让后端沿用草稿现状。
  // 没选摄像头时按钮本身就是 disabled（cameraChoiceMissing），这里不必再兜一层；
  // 真正的不变量由后端 confirm 的 camera_required 守着。
  if (showCameraPicker.value) body.camera_id = selectedCamera.value ?? ''
  try {
    const saved = await apiPost(
      `/api/rules/pending/${props.pendingId}/confirm`,
      body,
    )
    emit('confirmed', saved)
  } catch (e) {
    // 草稿过期/进程重启后草稿必丢（不持久化）：让上层提示用户重说需求并关掉弹窗；
    // 其它失败（如设备已不存在）留在弹窗内，用户还能改。
    if (e?.status === 404) {
      emit('expired', e.message || '待确认规则不存在或已过期')
      return
    }
    actionError.value = e.message || String(e)
  } finally {
    applying.value = false
  }
}

async function cancelPending() {
  if (applying.value) return
  applying.value = true
  actionError.value = ''
  try {
    const result = await apiPost(
      `/api/rules/pending/${props.pendingId}/cancel`,
      { session_id: props.sessionId },
    )
    emit('cancelled', result?.name || '')
  } catch (e) {
    if (e?.status === 404) {
      emit('expired', e.message || '待确认规则不存在或已过期')
      return
    }
    actionError.value = e.message || String(e)
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
        <div class="revise-container aurora-before">
          <div class="revise-header">
            <h2>{{ modalTitle }}</h2>
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
            <p v-if="isPending" class="pending-notice">
              这条规则<strong>还没有创建</strong>，点下方「确认创建」才会生效并开始自动执行。
              <span class="pending-ttl">草稿 {{ EXPIRE_MINUTES }} 分钟内有效</span>
            </p>
            <div v-if="autoCorrections.length" class="auto-fix-banner">
              <p v-for="c in autoCorrections" :key="c.action_index" class="auto-fix-item">
                ⚠️ 没找到 <s>{{ c.from }}</s>，已自动匹配为「{{ c.to_name }}」——请核对该设备是否是你想要的，
                不对可在下方修改模式里更换。
              </p>
            </div>
            <div v-for="part in summaryParts" :key="part.label" class="summary-row">
              <span class="summary-label">{{ part.label }}</span>
              <span class="summary-value">{{ part.value }}</span>
            </div>

            <!-- 视觉规则的摄像头绑定：必须显式选一次（某一路 / 全部摄像头） -->
            <div v-if="showCameraPicker" class="camera-picker">
              <div class="camera-picker-head">
                <span class="summary-label">看哪路</span>
                <span class="vision-tag">识别为：视觉规则</span>
              </div>
              <div class="camera-options">
                <button
                  v-for="c in cameraChoices"
                  :key="c.id || '__global__'"
                  type="button"
                  class="camera-chip"
                  :class="{ active: selectedCamera === c.id, global: c.id === '' }"
                  @click="selectedCamera = c.id"
                >{{ c.name }}</button>
              </div>
              <p v-if="cameraChoiceMissing" class="camera-hint required">
                必须选一路 —— 不绑定的话任意一路摄像头有人都会触发这条规则
              </p>
              <p v-else-if="isGlobalChoice" class="camera-hint danger">
                ⚠️ 已选「全部摄像头」：每一路画面里出现目标都会触发。确认这是你要的
              </p>
              <p v-else class="camera-hint">只有这一路的画面会触发这条规则</p>
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
            <p v-if="actionError" class="action-error">❌ {{ actionError }}</p>
            <div class="action-row">
              <span class="hint">
                <template v-if="isPending">
                  {{ cameraChoiceMissing
                    ? '还需选择这条规则看哪一路摄像头'
                    : (mode === 'plan'
                      ? '只读问答，不会因此创建规则'
                      : (hasRevision ? '改动已同步到草稿，确认无误后创建' : '可先对话修改，再确认创建')) }}
                </template>
                <template v-else>
                  {{ mode === 'plan'
                    ? '只读问答，不会改动配置'
                    : (hasRevision ? '预览已更新，确认无误后应用' : '先对话修改，再应用') }}
                </template>
              </span>
              <div class="action-btns">
                <!-- pending：确认/取消直接落库或弃稿，不提供「应用修改」（草稿没有 rule_id） -->
                <template v-if="isPending">
                  <button class="btn-cancel" :disabled="applying" @click="cancelPending">
                    取消创建
                  </button>
                  <button
                    class="btn-apply"
                    :disabled="applying || cameraChoiceMissing"
                    @click="confirmPending"
                  >
                    {{ applying ? '处理中…' : '✅ 确认创建' }}
                  </button>
                </template>
                <template v-else>
                  <button class="btn-cancel" @click="emit('close')">关闭</button>
                  <button
                    v-if="mode === 'modify'"
                    class="btn-apply"
                    :disabled="!hasRevision || applying"
                    @click="applyChanges"
                  >
                    {{ applying ? '保存中…' : '应用修改' }}
                  </button>
                </template>
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

/* pending 模式：还没落库的醒目提示 + TTL */
.pending-notice {
  font-size: var(--text-xs);
  line-height: 1.7;
  color: var(--color-text-secondary);
  background: var(--color-warning-bg, rgba(255, 176, 32, 0.12));
  border: 1px solid var(--color-warning-border, rgba(255, 176, 32, 0.35));
  border-radius: var(--radius-lg);
  padding: var(--space-6) var(--space-10);
  margin: 0 0 var(--space-6);
}
.pending-notice strong { color: var(--color-text); }
.pending-ttl {
  display: block;
  color: var(--color-text-muted);
}

/* 自动匹配横幅：幻觉设备被替换为近似真实设备，醒目提示用户核对 */
.auto-fix-banner {
  margin: 0 0 var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}
.auto-fix-item {
  font-size: var(--text-xs);
  line-height: 1.7;
  color: var(--color-text);
  background: var(--color-warning-bg, rgba(255, 176, 32, 0.12));
  border: 1px solid var(--color-warning-border, rgba(255, 176, 32, 0.35));
  border-left: 3px solid var(--color-warning-border, rgba(255, 176, 32, 0.6));
  border-radius: var(--radius-md);
  padding: var(--space-5) var(--space-8);
  margin: 0;
  word-break: break-all;
}
.auto-fix-item s { color: var(--color-text-muted); }

/* 视觉规则的摄像头绑定选择器 */
.camera-picker {
  margin-top: var(--space-8);
  padding-top: var(--space-8);
  border-top: 1px dashed var(--color-border);
}
.camera-picker-head {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  margin-bottom: var(--space-6);
}
.vision-tag {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: 1px var(--space-6);
}
.camera-options {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6);
}
.camera-chip {
  padding: var(--space-5) var(--space-10);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.camera-chip:hover { background: var(--color-surface-hover); }
.camera-chip.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}
/* 「全部摄像头（全局）」选中时用警示色而非主色 —— 它是合法但危险的选择 */
.camera-chip.global.active {
  background: var(--color-warning, #b07000);
  border-color: var(--color-warning, #b07000);
}
.camera-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin: var(--space-6) 0 0;
  line-height: 1.6;
}
.camera-hint.required { color: var(--color-text-secondary); }
.camera-hint.danger { color: var(--color-warning, #b07000); }

/* 确认/取消失败：留在弹窗内展示，不用 alert（用户还要能改） */
.action-error {
  font-size: var(--text-xs);
  color: var(--color-error, #ff6b6b);
  margin: var(--space-6) 0 0;
  word-break: break-word;
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
