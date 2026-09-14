<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { getChatSessionId, setChatSessionId, clearChatSession } from '../utils/storage'
import { toolIcon, summarizeToolCall, summarizeToolResult, parseToolResult, shortToolName } from '../utils/toolNames'
import { useVoiceInput } from '../composables/useVoiceInput'
import { useCamera } from '../composables/useCamera'
import { useLlmStatus, ROLE_LABELS } from '../composables/useLlmStatus'
import { usePtz } from '../composables/usePtz'
import { useCameraPreview } from '../composables/useCameraPreview'
import { useAuth } from '../composables/useAuth'
import { apiGet } from '../utils/api'
import PluginSlot from '../components/integration/PluginSlot.vue'
import CameraSwitcher from '../components/CameraSwitcher.vue'
import ReviseChatModal from '../components/ReviseChatModal.vue'
import DeviceSelectModal from '../components/DeviceSelectModal.vue'

const router = useRouter()

// LLM 模型状态（composable 统一封装：模型名静态读 + 悬停懒加载连通性测试）
const { chatModelName, llmStatus, llmStatusLoading, showLlmPopover, onStatusHover, loadChatModelName } = useLlmStatus()

// Task 12:多路摄像头列表 + 当前选中路(D4 AI 预览单例)
const { cameras, loadCameras } = useCamera()
const activeCameraId = ref('')   // 当前弹窗预览的摄像头 id

// PTZ 云台控制（依赖 activeCameraId / cameras）
const { ptzEnabled, ptzMoving, fetchPtzStatus, ptzStep } = usePtz(activeCameraId, cameras)

// 摄像头预览模态框（feed 状态机 + 多路切换 + 状态轮询，依赖上面的三项）
const {
  videoFeedUrl, feedStatus, feedStatusSource, feedRetryCount,
  showCamera, cameraState,
  refreshVideoFeed, onVideoFeedError, onVideoFeedLoad,
  openCamera, closeCamera, switchCamera,
} = useCameraPreview(activeCameraId, cameras, loadCameras)

// 编排：打开/切路后同步 PTZ 配置（composable 不感知 PTZ，由调用方编排）
async function openCameraModal() {
  await openCamera()
  fetchPtzStatus()
}
async function switchCameraRoute(id) {
  await switchCamera(id)
  fetchPtzStatus()
}

// 当前预览的摄像头是否为插件虚拟摄像头（source_type='test'）。
// 测试插件面板（文件路径导入/演练开关/识别日志）只挂在虚拟摄像头这一路，
// 切到真实摄像头时不显示——不污染普通摄像头预览。
const activeCameraIsVirtual = computed(() => {
  const cam = cameras.value.find(c => c.id === activeCameraId.value)
  return cam?.source_type === 'test'
})

// ============ State ============
const messages = ref([])
const inputText = ref('')
const voiceError = ref('')
const voice = useVoiceInput({
  // 识别文本追加到输入框（不自动发送），错误短暂提示
  onResult: (t) => { inputText.value = (inputText.value + t).replace(/\s+$/, '') },
  onError: (e) => {
    voiceError.value = e?.name === 'NotAllowedError' ? '麦克风权限被拒绝' : (e?.message || '语音识别失败')
    setTimeout(() => { voiceError.value = '' }, 3000)
  },
})
const sessionId = ref(null)
const wsConnected = ref(false)
const statusPhase = ref('')
const statusDetail = ref('')
const chatMode = ref('aether')  // 'aether' 或插件声明的 mode 值
const pendingToolCalls = ref([])
let currentStreamingMsg = null
let ws = null
let reconnectTimer = null

// ============ 待确认规则弹窗 ============
// automation_rule_create 只解析不落库，返回 pending_confirm 草稿。网页端在这里
// 接住它，弹确认框让用户点一下直接落库 —— 否则草稿 10 分钟后过期，规则永远建不成。
// pendingRuleDraft 先存着，等本轮 Dialog.Finish 才弹（见 openPendingRuleModal）。
const pendingRuleDraft = ref(null)   // { pendingId, rule }
const showRuleConfirm = ref(false)

// ============ 设备消歧弹窗 ============
// call_service 的消歧闸门判定目标设备有歧义（ambiguous）或设备不存在
// （category_miss）时不执行、也不让模型猜，返回 status="need_selection" + 候选。
// 这里接住它，弹框让用户勾选后直接执行（草稿里存着模型已解析好的动作）。
// 同样等本轮 Dialog.Finish 才弹，理由见 openDeviceSelectModal 的注释。
const pendingSelection = ref(null)   // { pendingId, query, reason, service, candidates }
const showDeviceSelect = ref(false)

const { user } = useAuth()
// 当前用户名：聊天会话按用户命名空间隔离（见 utils/storage.js），组件生命周期内稳定
const currentUsername = () => user.value?.username

// Slash command autocomplete
const showSlashMenu = ref(false)
const slashIndex = ref(0)
const slashFiltered = ref([])

const SLASH_COMMANDS = [
  { cmd: '/undo', desc: '撤销上一轮对话', action: 'api', handler: doUndo },
  { cmd: '/clear', desc: '清空当前会话消息', action: 'api', handler: doClear },
  { cmd: '/compress', desc: '压缩当前上下文生成摘要', action: 'api', handler: doCompress },
  { cmd: '/new', desc: '创建新会话', action: 'fn', handler: doNewSession },
  { cmd: '/camera', desc: '摄像头预览', action: 'fn', handler: openCameraModal },
  { cmd: '/halist', desc: '查看智能家居设备', action: 'nav', url: '/halist' },
  { cmd: '/task', desc: '查看自动化规则', action: 'nav', url: '/task' },
  { cmd: '/scheduled', desc: '查看定时任务', action: 'nav', url: '/scheduled' },
  { cmd: '/report', desc: '家庭报告（告警/周报）', action: 'nav', url: '/report' },
  { cmd: '/models', desc: '模型配置与切换', action: 'nav', url: '/models' },
  { cmd: '/sessions', desc: '浏览并切换历史会话', action: 'nav', url: '/sessions' },
  { cmd: '/doc', desc: '打开RAG文档助手', action: 'nav', url: '/doc' },
  { cmd: '/sg', desc: '构建与管理语义图', action: 'nav', url: '/sg' },
  { cmd: '/semantics', desc: '设备语义映射', action: 'nav', url: '/semantics' },
  { cmd: '/operations', desc: '运维中心', action: 'nav', url: '/operations' },
  { cmd: '/monitor', desc: '查看系统监控', action: 'nav', url: '/monitor' },
  { cmd: '/plugin', desc: '插件管理', action: 'nav', url: '/plugin' },
]

// /operations 是管理员工具，普通成员不显示
const availableSlashCommands = computed(() =>
  SLASH_COMMANDS.filter(c => c.cmd !== '/operations' || !!user.value?.is_admin)
)

const statusText = computed(() => {
  switch (statusPhase.value) {
    case 'thinking': return '正在思考...'
    case 'executing': return `正在执行 ${statusDetail.value}...`
    case 'retrying': return '正在重试...'
    case 'finalizing': return '正在整理回复...'
    default: return ''
  }
})

// ============ WebSocket ============
function connectWS() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  // WS 鉴权靠同源请求自动携带的 httpOnly cookie（aether_token），
  // JS 无法读取该 cookie，故不在 URL 拼 token。
  const wsUrl = `${protocol}//${window.location.host}/ws/chat`

  ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    wsConnected.value = true
    if (reconnectTimer) {
      clearTimeout(reconnectTimer)
      reconnectTimer = null
    }
  }

  ws.onclose = (event) => {
    wsConnected.value = false
    // 1008 = Policy Violation (认证失败)。可能是 access token 过期。
    // 先尝试静默刷新 cookie（复用 /api/auth/refresh），成功则重连（用户无感）；
    // 失败说明 refresh 也过期 → 派发 session-expired 走登录流程，不盲目重连。
    if (event.code === 1008) {
      refreshAndReconnect()
      return
    }
    reconnectTimer = setTimeout(connectWS, 3000)
  }

  ws.onerror = () => {
    ws.close()
  }

  ws.onmessage = (event) => {
    try {
      const instruction = JSON.parse(event.data)
      handleInstruction(instruction)
    } catch (e) {
      console.error('Failed to parse WS message:', e)
    }
  }
}

// access token 过期导致 WS 被 1008 踢：先静默刷新 cookie 再重连，用户无感。
// refresh 也过期则派发 session-expired（useAuth 会清登录态跳登录）。
// WS 不走 fetch 拦截（main.js 的 doRefresh 只管 HTTP），故此处独立刷新。
async function refreshAndReconnect() {
  try {
    const res = await fetch('/api/auth/refresh', { method: 'POST', credentials: 'include' })
    if (res.ok) {
      // 刷新成功：新 cookie 已写入，立即重连（比盲目 3s 重连更快恢复）
      reconnectTimer = setTimeout(connectWS, 500)
      return
    }
  } catch (e) {
    console.warn('WS reconnect refresh failed:', e)
  }
  // refresh 失败：会话不可恢复，通知 useAuth 走登录流程
  window.dispatchEvent(new Event('aether:session-expired'))
}

function handleInstruction(inst) {
  const ns = inst.header?.namespace
  const name = inst.header?.name
  const payload = inst.payload || {}

  // Update session_id from response
  if (inst.header?.session_id && !sessionId.value) {
    sessionId.value = inst.header.session_id
  }

  switch (`${ns}.${name}`) {
    case 'UI.Status':
      statusPhase.value = payload.phase || ''
      statusDetail.value = payload.detail || ''
      break

    case 'Template.TokenStream':
      statusPhase.value = ''
      appendToken(payload.token, payload.is_final)
      break

    case 'Template.CallTool':
      addToolCall({
        id: payload.id,
        type: 'call',
        toolName: payload.tool_name,
        params: payload.tool_params,
        serviceName: payload.service_name,
        friendlyName: payload.friendly_name,
      })
      break

    case 'Template.CallToolResult':
      addToolCall({
        id: payload.id,
        type: 'result',
        toolName: payload.tool_name,
        success: payload.success,
        response: payload.tool_response,
        error: payload.error_message,
      })
      capturePendingRule(payload)
      capturePendingSelection(payload)
      break

    case 'Template.ToastStream': {
      // Final complete message
      const streamingMsg = messages.value.find(m => m.role === 'assistant' && m.streaming)
      if (streamingMsg) {
        // 占位 msg（工具调用创建、还没 token 流）content 为空时，填入完整回复；
        // token 流路径已填充 content 的，跳过避免覆盖
        if (!streamingMsg.content) {
          streamingMsg.content = payload.stream
        }
      } else {
        messages.value.push({
          role: 'assistant',
          content: payload.stream,
          toolCalls: [...pendingToolCalls.value],
        })
        pendingToolCalls.value = []
      }
      scrollToBottom()
      break
    }

    case 'Dialog.Exception':
      messages.value.push({
        role: 'system',
        content: `错误: ${payload.message}`,
      })
      statusPhase.value = ''
      scrollToBottom()
      break

    case 'Dialog.Finish':
      statusPhase.value = ''
      // 直通模式回传文案（如"已转交处理"）显示为助手消息
      if (payload.message) {
        messages.value.push({ role: 'assistant', content: payload.message })
        scrollToBottom()
      }
      finalizeStreaming()
      openPendingRuleModal()
      openDeviceSelectModal()
      break
  }
}

// ============ 待确认规则弹窗 ============

/**
 * 从工具结果里认出 pending_confirm 草稿。
 *
 * tool_response.result 是 **JSON 字符串**（LangChain ToolMessage 只能是字符串），
 * 所以必须走 parseToolResult，不能直接读 .status。
 * revise 也返回 pending_confirm（同一个 pending_id、规则已更新），一并接住，
 * 这样用户在聊天里改完，弹窗里的摘要不会停在旧版本。
 */
function capturePendingRule(payload) {
  if (!payload?.success) return
  const name = shortToolName(payload.tool_name)
  if (name !== 'automation_rule_create' && name !== 'automation_rule_revise') return
  const data = parseToolResult(payload.tool_response)
  if (data?.status !== 'pending_confirm' || !data.pending_id) return
  pendingRuleDraft.value = { pendingId: data.pending_id, rule: data.rule || {} }
}

/**
 * 轮末才弹确认框 —— 这是硬约束，不是体验取舍。
 *
 * dispatcher 在**轮末**才把 user/assistant 消息 append 进 session.model_messages
 * （app/agents/dispatcher.py）。而 REST 确认端点会往同一个列表追加一条
 * 「（我已通过界面确认…）」，好让下一轮 LLM 知道规则已建。若在轮中就弹框、
 * 用户手快点了确认，这条确认会排在原始请求**之前**，模型下一轮读到的是乱序历史。
 * Dialog.Finish 在 append 之后才发，等到它就保证了顺序。
 */
function openPendingRuleModal() {
  if (!pendingRuleDraft.value || showRuleConfirm.value) return
  // 懒加载摄像头列表：ChatView 挂载时并不拉 /api/cameras（只有开摄像头预览才拉），
  // 不补这一下，弹窗的选择器就只剩「全部摄像头」一项，视觉规则没法绑具体某路。
  if (!cameras.value.length) loadCameras()
  showRuleConfirm.value = true
}

function onRuleConfirmed(saved) {
  showRuleConfirm.value = false
  pendingRuleDraft.value = null
  messages.value.push({
    role: 'system',
    content: `✅ 规则已创建：${saved?.name || ''}${saved?.summary ? ` — ${saved.summary}` : ''}`,
  })
  scrollToBottom()
}

function onRuleCancelled(name) {
  showRuleConfirm.value = false
  pendingRuleDraft.value = null
  messages.value.push({ role: 'system', content: `已取消创建规则${name ? `：${name}` : ''}` })
  scrollToBottom()
}

function onRuleExpired(message) {
  showRuleConfirm.value = false
  pendingRuleDraft.value = null
  messages.value.push({
    role: 'system',
    content: `⚠️ ${message}，请重新说一遍需求。`,
  })
  scrollToBottom()
}

function onRuleModalClose() {
  // 只关弹窗、不动后端草稿：TTL 内用户仍可在聊天里说「确认」走口头路径。
  // 前端触发器一并清掉，避免下一轮又自动弹出来烦人。
  showRuleConfirm.value = false
  pendingRuleDraft.value = null
}

// ============ 设备消歧弹窗 ============

/**
 * 从工具结果里认出 need_selection（消歧闸门要求用户挑设备）。
 *
 * 与 capturePendingRule 同构：tool_response.result 是 **JSON 字符串**，必须走
 * parseToolResult。工具短名是 `call_service`（全名 ha_devices___call_service，
 * shortToolName 按 ___ 切分取后半）。
 *
 * need_selection 刻意是 **success 形状**（返回体不含 "error" 键）：带 error 会被
 * langchain_tools 加 "Error:" 前缀 → langgraph 判 is_error → dispatcher 触发失败
 * 重试轮，模型会被逼着自己再猜一个实体。所以这里同样要求 payload.success 为真。
 */
function capturePendingSelection(payload) {
  if (!payload?.success) return
  if (shortToolName(payload.tool_name) !== 'call_service') return
  const data = parseToolResult(payload.tool_response)
  if (data?.status !== 'need_selection' || !data.pending_id) return
  pendingSelection.value = {
    pendingId: data.pending_id,
    query: data.query || '',
    reason: data.reason || 'ambiguous',
    service: data.action?.service || '',
    candidates: data.candidates || [],
  }
}

/**
 * 轮末才弹 —— 与 openPendingRuleModal 是同一条硬约束，不是体验取舍：
 * dispatcher 在**轮末**才把 user/assistant 消息 append 进 session.model_messages，
 * 而 /api/ha/pending/{id}/select 会往同一个列表追加「（我已通过界面选择…）」。
 * 轮中弹框、用户手快点了确认，这条会排在原始请求**之前**，模型下一轮读到乱序历史。
 */
function openDeviceSelectModal() {
  if (!pendingSelection.value || showDeviceSelect.value) return
  showDeviceSelect.value = true
}

function onSelectionConfirmed(data) {
  showDeviceSelect.value = false
  pendingSelection.value = null
  const names = (data?.names || []).join('、')
  messages.value.push({
    role: 'system',
    content: `✅ 已执行：${names || (data?.entity_ids || []).join('、')}`,
  })
  scrollToBottom()
}

function onSelectionCancelled() {
  showDeviceSelect.value = false
  pendingSelection.value = null
  messages.value.push({ role: 'system', content: '已取消，没有操作任何设备' })
  scrollToBottom()
}

function onSelectionExpired(message) {
  showDeviceSelect.value = false
  pendingSelection.value = null
  // 后端 _SELECTION_GONE 自带「请重新说一遍指令」，去重避免同一句说两遍
  const hint = '请重新说一遍指令'
  const body = String(message || '').includes(hint)
    ? `${message}。`
    : `${message}，${hint}。`
  messages.value.push({ role: 'system', content: `⚠️ ${body}` })
  scrollToBottom()
}

function onSelectionModalClose() {
  // 只关弹窗、不动后端草稿：TTL 内用户仍可在聊天里直接说设备名走口头路径
  // （语音渠道就是这么用的）。前端触发器一并清掉，避免下一轮又自动弹出来烦人。
  showDeviceSelect.value = false
  pendingSelection.value = null
}

function addToolCall(tc) {
  // 工具调用通常先于文字到达。此时立即创建占位 streaming message 挂载工具卡片，
  // 让工具反馈即时渲染——否则工具调用会堆积在 pendingToolCalls（模板不渲染它），
  // 直到第一个 token 流到来才转移显示，满屏时用户看不到工具执行过程。
  if (!currentStreamingMsg) {
    const placeholder = {
      role: 'assistant',
      content: '',
      streaming: true,
      toolCalls: [],
    }
    messages.value.push(placeholder)
    // 关键：push 后从 messages.value 取回 Vue 代理对象，否则 currentStreamingMsg
    // 指向的是原始对象，后续 toolCalls.push 不会触发响应式更新（满屏不渲染的根因）
    currentStreamingMsg = messages.value[messages.value.length - 1]
  }
  const target = currentStreamingMsg.toolCalls
  if (tc.type === 'call') {
    // 新工具调用：创建合并对象，状态 running
    target.push({
      id: tc.id,
      toolName: tc.toolName,
      params: tc.params,
      friendlyName: tc.friendlyName,
      status: 'running',
      success: false,
      result: null,
      error: null,
      expanded: false,
    })
  } else {
    // 结果到达：按 id 找到对应的 running 工具，合并结果
    const existing = target.find(t => t.id === tc.id && t.status === 'running')
    if (existing) {
      existing.status = tc.success ? 'done' : 'failed'
      existing.success = tc.success
      existing.result = tc.response
      existing.error = tc.error
    } else {
      // 兜底：result 先到（理论上不应发生，防御性处理）
      target.push({
        id: tc.id,
        toolName: tc.toolName,
        params: null,
        friendlyName: null,
        status: tc.success ? 'done' : 'failed',
        success: tc.success,
        result: tc.response,
        error: tc.error,
        expanded: false,
      })
    }
  }
  // 工具卡片入 DOM 后滚到底，避免满屏时新工具被推到视野外
  scrollToBottom()
}

// ============ 工具调用渲染辅助 ============
function callSummary(tc) {
  return summarizeToolCall(tc.toolName, tc.params, tc.friendlyName)
}

function resultSummary(tc) {
  return summarizeToolResult(tc.toolName, tc.success, tc.result, tc.error)
}

function detailParams(tc) {
  if (!tc.params) return ''
  return JSON.stringify(tc.params, null, 2)
}

function detailResult(tc) {
  if (tc.error) return tc.error
  const data = parseToolResult(tc.result)
  if (!data) return tc.result?.result || ''
  return JSON.stringify(data, null, 2).slice(0, 500)
}

function toggleExpand(tc) {
  tc.expanded = !tc.expanded
}

function appendToken(token, isFinal) {
  const lastMsg = messages.value[messages.value.length - 1]
  if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
    lastMsg.content += token
  } else {
    const newMsg = {
      role: 'assistant',
      content: token,
      streaming: true,
      toolCalls: [],
    }
    messages.value.push(newMsg)
    // 取回 Vue 代理对象，保证后续 toolCalls/content 修改是响应式的
    currentStreamingMsg = messages.value[messages.value.length - 1]
    if (pendingToolCalls.value.length) {
      currentStreamingMsg.toolCalls.push(...pendingToolCalls.value)
      pendingToolCalls.value = []
    }
  }
  if (isFinal) {
    finalizeStreaming()
  }
  scrollToBottom()
}

function finalizeStreaming() {
  const lastMsg = messages.value[messages.value.length - 1]
  if (lastMsg && lastMsg.streaming) {
    lastMsg.streaming = false
    // 占位 msg（工具调用创建）若 content 为空且无工具调用，移除空气泡；
    // 有工具调用的保留（显示工具卡片），即便没文字总结
    if (!lastMsg.content && (!lastMsg.toolCalls || !lastMsg.toolCalls.length)) {
      messages.value.pop()
    }
  }
  currentStreamingMsg = null
}

function scrollToBottom() {
  nextTick(() => {
    const el = document.querySelector('.chat-messages')
    if (el) el.scrollTop = el.scrollHeight
  })
}

// ============ Send Message ============
function onModeChanged(e) {
  chatMode.value = e.detail.mode
}

function handleInterrupt() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'interrupt' }))
  }
  finalizeStreaming()
}

function sendMessage() {
  const text = inputText.value.trim()
  if (!text) return

  // Check slash commands
  if (text.startsWith('/')) {
    // 与自动补全同源（availableSlashCommands）：/operations 仅管理员可见，
    // 发送路径若查未过滤的原表，普通成员手输 /operations 仍会导航过去
    const cmd = availableSlashCommands.value.find(c => c.cmd === text)
    if (cmd) {
      executeSlashCommand(cmd)
      inputText.value = ''
      return
    }
  }

  // Add user message
  messages.value.push({ role: 'user', content: text })
  inputText.value = ''
  pendingToolCalls.value = []
  currentStreamingMsg = null
  // 新一轮开始：丢掉上一轮没处理的草稿触发器，别让它在这轮末尾突然弹出来
  pendingRuleDraft.value = null
  pendingSelection.value = null

  // Send via WebSocket
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'chat',
      query: text,
      session_id: sessionId.value,
      mode: chatMode.value,
    }))
  } else {
    messages.value.push({
      role: 'system',
      content: 'WebSocket 未连接，请刷新页面重试。',
    })
  }
  scrollToBottom()
}

// ============ Slash Commands ============
function onInput(e) {
  const val = e.target.value
  if (val.startsWith('/')) {
    const q = val.slice(1).toLowerCase()
    slashFiltered.value = availableSlashCommands.value.filter(c =>
      c.cmd.startsWith('/' + q) || c.desc.toLowerCase().includes(q)
    )
    if (slashFiltered.value.length) {
      showSlashMenu.value = true
      slashIndex.value = 0
    } else {
      showSlashMenu.value = false
    }
  } else {
    showSlashMenu.value = false
  }
}

function onKeydown(e) {
  if (showSlashMenu.value) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      slashIndex.value = (slashIndex.value + 1) % slashFiltered.value.length
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      slashIndex.value = (slashIndex.value - 1 + slashFiltered.value.length) % slashFiltered.value.length
    } else if (e.key === 'Tab' || e.key === 'Enter') {
      if (slashFiltered.value.length) {
        e.preventDefault()
        executeSlashCommand(slashFiltered.value[slashIndex.value])
      }
    } else if (e.key === 'Escape') {
      showSlashMenu.value = false
    }
  } else if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    sendMessage()
  }
}

function executeSlashCommand(cmd) {
  showSlashMenu.value = false
  inputText.value = ''
  if (cmd.action === 'nav') {
    messages.value.push({ role: 'system', content: `正在跳转到 ${cmd.cmd} ...` })
    setTimeout(() => router.push(cmd.url), 300)
  } else if (cmd.action === 'api' || cmd.action === 'fn') {
    cmd.handler()
  }
}

// ============ Slash Command Handlers ============
async function doUndo() {
  if (!sessionId.value) return
  try {
    const res = await fetch(`/api/sessions/${sessionId.value}/undo`, { method: 'POST' })
    const json = await res.json()
    if (json.data?.undone) {
      // 重新加载会话历史
      await loadSessionHistory(sessionId.value)
      messages.value.push({ role: 'system', content: '已撤销上一轮对话' })
    }
  } catch (e) {
    messages.value.push({ role: 'system', content: `撤销失败: ${e.message}` })
  }
}

async function doClear() {
  if (!sessionId.value) return
  if (!confirm('确定要清空当前会话所有消息吗？')) return
  try {
    const res = await fetch(`/api/sessions/${sessionId.value}/clear`, { method: 'POST' })
    const json = await res.json()
    if (json.data?.cleared) {
      messages.value = []
      messages.value.push({ role: 'system', content: '会话已清空' })
    }
  } catch (e) {
    messages.value.push({ role: 'system', content: `清空失败: ${e.message}` })
  }
}

async function doCompress() {
  if (!sessionId.value) return
  messages.value.push({ role: 'system', content: '正在整理之前的对话摘要...' })
  try {
    const res = await fetch(`/api/sessions/${sessionId.value}/compress`, { method: 'POST' })
    const json = await res.json()
    if (json.data?.compressed) {
      messages.value.push({ role: 'system', content: `压缩完成，当前 ${json.data.message_count} 条消息` })
      const summaries = json.data?.summaries || []
      if (summaries.length > 0) {
        const summaryTexts = summaries.map((s, i) => `第${i + 1}段摘要：${s.text}`).join('\n\n')
        messages.value.push({ role: 'system', content: `之前聊了这些：\n${summaryTexts}` })
      }
    } else {
      messages.value.push({ role: 'system', content: `当前上下文未达压缩阈值（${json.data?.message_count ?? 0} 条消息），无需压缩` })
    }
  } catch (e) {
    messages.value.push({ role: 'system', content: `压缩失败: ${e.message}` })
  }
}

async function doNewSession() {
  try {
    const res = await fetch('/api/sessions', { method: 'POST' })
    const json = await res.json()
    sessionId.value = json.data?.id
    if (sessionId.value) {
      setChatSessionId(currentUsername(), sessionId.value)
    }
    messages.value = []
    pendingToolCalls.value = []
    currentStreamingMsg = null
    messages.value.push({ role: 'system', content: '已创建新会话' })
  } catch (e) {
    messages.value.push({ role: 'system', content: `创建会话失败: ${e.message}` })
  }
}

// ============ Session History ============
async function loadSessionHistory(sid) {
  if (!sid) return
  try {
    const res = await fetch(`/api/sessions/${sid}`)
    if (res.status === 404) {
      // Session no longer exists (e.g. DB reset) — clear stale ID, create fresh
      clearChatSession(currentUsername())
      sessionId.value = null
      return
    }
    const json = await res.json()
    if (json.data?.visible_messages) {
      messages.value = json.data.visible_messages.map(m => ({
        role: m.role,
        content: m.content,
        toolCalls: [],
      }))
    }
  } catch (e) {
    console.error('Failed to load session history:', e)
  }
}

// ============ Lifecycle ============
onMounted(async () => {
  connectWS()

  // 读取当前聊天模式（插件声明的 mode，默认 aether）
  try {
    const resp = await apiGet('/api/integrations/state/current_mode')
    if (resp?.value) chatMode.value = resp.value
  } catch { /* 集成平台未启用，默认 aether */ }

  // 监听插件贡献的模式按钮切换（ModeOptionContribution 派发 mode-changed 事件）
  window.addEventListener('mode-changed', onModeChanged)

  // 静态读取 chat 模型名（composable 封装，不耗测试 API）
  await loadChatModelName()

  // 1. 先检查 URL 参数
  const urlParams = new URLSearchParams(window.location.search)
  const urlSessionId = urlParams.get('session')

  // 2. 再检查 sessionStorage
  const savedSessionId = getChatSessionId(currentUsername())

  // 3. 优先使用 URL 参数，其次 sessionStorage，最后创建新的
  if (urlSessionId) {
    sessionId.value = urlSessionId
    setChatSessionId(currentUsername(), urlSessionId)
    await loadSessionHistory(urlSessionId)
  } else if (savedSessionId) {
    sessionId.value = savedSessionId
    await loadSessionHistory(savedSessionId)
  }
  // If session was stale (404 cleared it), create a fresh one
  if (!sessionId.value) {
    try {
      const res = await fetch('/api/sessions', { method: 'POST' })
      const json = await res.json()
      sessionId.value = json.data?.id
      if (sessionId.value) {
        setChatSessionId(currentUsername(), sessionId.value)
      }
    } catch (e) {
      console.error('Failed to create session:', e)
    }
  }
})

onUnmounted(() => {
  if (ws) ws.close()
  if (reconnectTimer) clearTimeout(reconnectTimer)
  window.removeEventListener('mode-changed', onModeChanged)
  // 摄像头预览模态框（feedRetryTimer / 轮询）与欢迎语计时器（greetingTimer）
  // 均由各自 composable 的 onScopeDispose 自行清理，这里无需感知。
  // 保持 sessionId 在 sessionStorage 中，下次进入可恢复
})
</script>

<template>
  <div class="chat-view">
    <!-- Top Left Controls -->
    <div class="top-left-controls">
      <PluginSlot slot="chat_top_bar" />
    </div>

    <!-- Status Bar -->
    <div class="status-bar" v-if="statusPhase">
      <div class="status-dot" :class="statusPhase"></div>
      <span class="status-text">{{ statusText }}</span>
    </div>

    <!-- Messages -->
    <div class="chat-messages">
      <div v-if="!messages.length" class="empty-state">
        <div class="empty-icon">&#128172;</div>
        <p>开始对话吧</p>
        <p class="empty-hint">输入 / 查看可用命令</p>
      </div>

      <template v-for="(msg, i) in messages" :key="i">
        <!-- User Message -->
        <div v-if="msg.role === 'user'" class="message user-message">
          <div class="message-content">{{ msg.content }}</div>
        </div>

        <!-- Assistant Message -->
        <div v-else-if="msg.role === 'assistant'" class="message assistant-message">
          <div class="message-avatar">&#9733;</div>
          <div class="message-content">
            <div class="message-text">{{ msg.content }}</div>
            <span v-if="msg.streaming" class="streaming-indicator">|</span>
          </div>
        </div>

        <!-- System Message -->
        <div v-else-if="msg.role === 'system'" class="message system-message">
          {{ msg.content }}
        </div>

        <!-- Tool Calls (友好化显示，默认开启) -->
        <template v-if="msg.role === 'assistant' && msg.toolCalls && msg.toolCalls.length">
          <template v-for="tc in msg.toolCalls" :key="tc.id">
            <!-- 通用工具卡片：摘要行 + 可展开详情 -->
            <div class="tool-call-card">
              <div class="tool-summary-row" @click="toggleExpand(tc)">
                <span class="tool-icon">{{ toolIcon(tc.toolName) }}</span>
                <span class="tool-summary-text">{{ callSummary(tc) }}</span>
                <span v-if="tc.status === 'running'" class="tool-status running">◐</span>
                <span v-else-if="tc.status === 'done'" class="tool-status done">✓ {{ resultSummary(tc) }}</span>
                <span v-else-if="tc.status === 'failed'" class="tool-status failed">✗ {{ resultSummary(tc) }}</span>
                <span class="tool-expand-icon" v-if="tc.params || tc.result || tc.error">{{ tc.expanded ? '▲' : '▼' }}</span>
              </div>
              <Transition name="expand">
                <div v-if="tc.expanded" class="tool-detail">
                  <div v-if="detailParams(tc)" class="tool-detail-section">
                    <div class="tool-detail-label">参数</div>
                    <pre class="tool-detail-code">{{ detailParams(tc) }}</pre>
                  </div>
                  <div v-if="detailResult(tc)" class="tool-detail-section">
                    <div class="tool-detail-label">{{ tc.status === 'failed' ? '错误' : '结果' }}</div>
                    <pre class="tool-detail-code">{{ detailResult(tc) }}</pre>
                  </div>
                </div>
              </Transition>
            </div>
          </template>
        </template>
      </template>
    </div>

    <!-- Input Area -->
    <div class="chat-input-area">
      <div class="slash-autocomplete" v-if="showSlashMenu">
        <div
          v-for="(cmd, i) in slashFiltered"
          :key="cmd.cmd"
          class="slash-item"
          :class="{ active: i === slashIndex }"
          @click="executeSlashCommand(cmd)"
        >
          <span class="slash-cmd">{{ cmd.cmd }}</span>
          <span class="slash-desc">{{ cmd.desc }}</span>
        </div>
      </div>
      <div class="input-row">
        <input
          v-model="inputText"
          type="text"
          placeholder="输入消息或 / 命令..."
          @input="onInput"
          @keydown="onKeydown"
          class="chat-input"
        />
        <button
          class="mic-btn"
          :class="{ recording: voice.recording.value, busy: voice.transcribing.value }"
          :disabled="voice.transcribing.value"
          :title="voice.recording.value ? '停止录音' : '语音输入'"
          @click="voice.toggle"
        >
          {{ voice.transcribing.value ? '…' : voice.recording.value ? '■' : '🎤' }}
        </button>
        <button
          @click="statusPhase ? handleInterrupt() : sendMessage()"
          :class="['send-btn', { 'stop-btn': statusPhase }]"
        >
          {{ statusPhase ? '■' : '发送' }}
        </button>
      </div>
      <div class="voice-error" v-if="voiceError">{{ voiceError }}</div>
      <div
        class="connection-status"
        @mouseenter="showLlmPopover = true; onStatusHover()"
        @mouseleave="showLlmPopover = false"
      >
        <span class="ws-dot" :class="{ connected: wsConnected }"></span>
        <span>{{ wsConnected ? '已连接' : '未连接' }}{{ chatModelName && wsConnected ? ' · ' + chatModelName : '' }}</span>

        <!-- 模型连通性浮层（悬停展开，懒加载测试） -->
        <Transition name="llm-popover">
          <div v-if="showLlmPopover" class="llm-popover">
            <div class="llm-popover-title">
              {{ llmStatusLoading ? '正在测试模型连通性…' : '模型连通状态' }}
            </div>
            <div v-if="llmStatus?.roles" class="llm-popover-rows">
              <div
                v-for="role in ['chat', 'vision', 'embed']"
                :key="role"
                class="llm-popover-row"
              >
                <span class="llm-role-label">{{ ROLE_LABELS[role] }}</span>
                <span class="llm-model-name">{{ llmStatus.roles[role]?.model || '未配置' }}</span>
                <span class="llm-source-tag" :class="llmStatus.roles[role]?.source">
                  {{ llmStatus.roles[role]?.source === 'user' ? '私有' : '全局' }}
                </span>
                <span
                  class="llm-status-dot"
                  :class="{
                    connected: llmStatus.roles[role]?.connected,
                    failed: llmStatus.roles[role] && !llmStatus.roles[role].connected
                  }"
                  :title="llmStatus.roles[role]?.error || ''"
                ></span>
              </div>
            </div>
            <div v-else-if="!llmStatusLoading" class="llm-popover-empty">暂无数据</div>
          </div>
        </Transition>
      </div>
    </div>

    <!-- Camera Modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showCamera" class="camera-modal-overlay" @click.self="closeCamera">
          <div class="camera-modal">
            <div class="camera-modal-header">
              <h2>摄像头预览</h2>
              <div class="camera-header-actions">
                <span class="camera-badge" :class="{ active: cameraState?.camera_opened }">
                  {{ cameraState?.camera_opened ? '已连接' : '等待连接' }}
                </span>
                <button class="camera-modal-close" @click="closeCamera">关闭</button>
              </div>
            </div>
            <!-- Task 12:多路切换 — 恒用下拉,高度恒定不随路数/窄屏换行撑弹窗(D4 AI 预览单例) -->
            <CameraSwitcher :cameras="cameras" :modelValue="activeCameraId" @change="switchCameraRoute" />
            <div class="camera-modal-body">
              <div class="camera-stage">
                <img
                  :src="videoFeedUrl"
                  alt="camera stream"
                  class="camera-feed"
                  :class="{ hidden: feedStatus !== 'live' }"
                  @error="onVideoFeedError"
                  @load="onVideoFeedLoad"
                />
                <div v-if="feedStatus !== 'live'" class="camera-disconnected">
                  <div class="camera-disconnected-icon">{{ feedStatus === 'disconnected' ? '📷' : '🔄' }}</div>
                  <div class="camera-disconnected-text">
                    <template v-if="feedStatus === 'disconnected'">
                      摄像头未连接，请检查设备后重试
                    </template>
                    <template v-else-if="feedStatusSource === 'device'">
                      摄像头设备重连中…
                    </template>
                    <template v-else>
                      视频流重连中…（第 {{ feedRetryCount }} 次）
                    </template>
                  </div>
                  <button v-if="feedStatus === 'disconnected'" class="camera-retry-btn" @click="refreshVideoFeed">重试</button>
                  <button v-if="feedStatus === 'reconnecting'" class="camera-retry-btn" @click="closeCamera">关闭预览</button>
                </div>
              </div>
              <!-- PTZ 云台控制：点一下转一小段后自动停（按一下动一下）。仅 ptz.enabled 时显示 -->
              <div v-if="ptzEnabled" class="ptz-panel">
                <div class="ptz-dpad">
                  <button
                    class="ptz-btn ptz-up"
                    :class="{ pressing: ptzMoving }"
                    :disabled="ptzMoving"
                    @pointerdown.prevent="ptzStep('up')"
                    aria-label="上"
                  >▲</button>
                  <button
                    class="ptz-btn ptz-left"
                    :class="{ pressing: ptzMoving }"
                    :disabled="ptzMoving"
                    @pointerdown.prevent="ptzStep('left')"
                    aria-label="左"
                  >◀</button>
                  <div class="ptz-center" aria-hidden="true">
                    <span class="ptz-center-dot"></span>
                  </div>
                  <button
                    class="ptz-btn ptz-right"
                    :class="{ pressing: ptzMoving }"
                    :disabled="ptzMoving"
                    @pointerdown.prevent="ptzStep('right')"
                    aria-label="右"
                  >▶</button>
                  <button
                    class="ptz-btn ptz-down"
                    :class="{ pressing: ptzMoving }"
                    :disabled="ptzMoving"
                    @pointerdown.prevent="ptzStep('down')"
                    aria-label="下"
                  >▼</button>
                </div>
              </div>
              <div class="camera-stats">
                <div class="camera-stat">
                  <div class="label">运动距离</div>
                  <div class="value">{{ cameraState?.motion_distance ?? '-' }}</div>
                </div>
                <div class="camera-stat">
                  <div class="label">累计推理</div>
                  <div class="value">{{ cameraState?.infer_count ?? 0 }}</div>
                </div>
                <div class="camera-stat">
                  <div class="label">模型 FPS</div>
                  <div class="value">{{ cameraState?.model_fps ? cameraState.model_fps.toFixed(1) : '-' }}</div>
                </div>
              </div>
              <div class="camera-feedback">
                <div class="label">识别反馈</div>
                <div class="value">{{ cameraState?.feedback || '等待识别。' }}</div>
              </div>
              <div class="camera-hint">
                💡 当前预览的摄像头即 AI 对话中 vision_chat 工具默认使用的摄像头。切换上方的下拉列表可改变 AI 看哪路。
              </div>
              <!-- 插件面板挂载点（仅当预览的是插件虚拟摄像头时显示，如 test-camera：
                   视频导入/演练开关/识别日志；真实摄像头预览不受影响） -->
              <PluginSlot v-if="activeCameraIsVirtual" slot="camera_preview_panel" />
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <!-- 待确认规则弹窗：聊天里解析出的草稿，点确认直接落库（组件自带 Teleport）。
         cameras 供视觉规则选绑定；弹窗自己按 rule.type 判断要不要显示选择器 -->
    <ReviseChatModal
      v-if="showRuleConfirm && pendingRuleDraft"
      kind="rule"
      :initial="pendingRuleDraft.rule"
      :pending-id="pendingRuleDraft.pendingId"
      :session-id="sessionId || ''"
      :cameras="cameras"
      @confirmed="onRuleConfirmed"
      @cancelled="onRuleCancelled"
      @expired="onRuleExpired"
      @close="onRuleModalClose"
    />

    <!-- 设备消歧弹窗：指令匹配到多个设备（或设备名不存在）时让用户勾选。
         确认走 REST 直接执行草稿里存好的动作，不再回模型 -->
    <DeviceSelectModal
      v-if="showDeviceSelect && pendingSelection"
      :pending-id="pendingSelection.pendingId"
      :session-id="sessionId || ''"
      :query="pendingSelection.query"
      :reason="pendingSelection.reason"
      :service="pendingSelection.service"
      :candidates="pendingSelection.candidates"
      @confirmed="onSelectionConfirmed"
      @cancelled="onSelectionCancelled"
      @expired="onSelectionExpired"
      @close="onSelectionModalClose"
    />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100vh;
  padding: var(--space-10);
  max-width: 900px;
  margin: 0 auto;
}

/* Status Bar */
.status-bar {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-8);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  margin-bottom: var(--space-8);
  animation: fadeIn 0.3s ease;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-info);
  animation: pulse 1.5s infinite;
}

.status-dot.executing { background: var(--color-warning); }
.status-dot.retrying { background: var(--color-danger); }
.status-dot.finalizing { background: var(--color-success); }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

.status-text {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

/* Top Left Controls */
.top-left-controls {
  position: fixed;
  top: var(--space-10);
  left: calc(var(--sidebar-width) + var(--space-10));
  z-index: 100;
  display: flex;
  align-items: center;
  gap: var(--space-6);
}

/* Messages */
.chat-messages {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
  padding: var(--space-8) 0;
  position: relative;
}

.empty-icon {
  font-size: 48px;
  opacity: 0.5;
}

.empty-hint {
  font-size: var(--text-sm);
  opacity: 0.6;
}

.message {
  display: flex;
  gap: var(--space-5);
  animation: msgIn 0.3s ease;
}

@keyframes msgIn {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

.user-message {
  justify-content: flex-end;
}

.user-message .message-content {
  background: var(--color-primary);
  color: #fff;
  padding: var(--space-5) var(--space-8);
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-sm) var(--radius-lg);
  max-width: 70%;
}

.assistant-message {
  align-items: flex-start;
}

.message-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-sm);
  flex-shrink: 0;
}

.assistant-message .message-content {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  padding: var(--space-5) var(--space-8);
  border-radius: var(--radius-lg) var(--radius-lg) var(--radius-lg) var(--radius-sm);
  max-width: 70%;
}

.message-text {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: var(--leading-relaxed);
}

.streaming-indicator {
  animation: blink 0.8s infinite;
  color: var(--color-primary);
}

@keyframes blink {
  0%, 100% { opacity: 1; }
  50% { opacity: 0; }
}

.system-message {
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  padding: var(--space-4);
}

/* Tool Call Cards — 友好化显示 */
.tool-call-card {
  background: rgba(74, 124, 112, 0.08);
  border: 1px solid rgba(74, 124, 112, 0.2);
  border-radius: var(--radius-md);
  margin: var(--space-2) 0 var(--space-2) 44px;
  font-size: var(--text-xs);
  /* overflow:hidden 会让 flex 项的 min-height 变成 0，满屏时卡片被压缩到 0 高度
     而「不渲染」——flex-shrink:0 强制保持自然高度，超出靠滚动条显示。这是
     「对话满一页后工具卡片消失」问题的根因修复。 */
  flex-shrink: 0;
  overflow: hidden;
}

.tool-summary-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-5);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-out);
}

.tool-summary-row:hover {
  background: rgba(74, 124, 112, 0.12);
}

.tool-icon {
  font-size: var(--text-sm);
  flex-shrink: 0;
}

.tool-summary-text {
  color: var(--color-text);
  flex-grow: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.tool-status {
  font-size: var(--text-xs);
  flex-shrink: 0;
}

.tool-status.running {
  color: var(--color-warning);
  animation: spin 1.2s linear infinite;
}

.tool-status.done {
  color: var(--color-success);
}

.tool-status.failed {
  color: var(--color-danger);
}

.tool-expand-icon {
  color: var(--color-text-muted);
  font-size: var(--text-xs);
  flex-shrink: 0;
}

.tool-detail {
  padding: var(--space-2) var(--space-5) var(--space-4);
  border-top: 1px solid var(--color-border);
  background: var(--color-surface);
}

.tool-detail-section {
  margin-top: var(--space-2);
}

.tool-detail-label {
  color: var(--color-text-muted);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--space-1);
}

.tool-detail-code {
  color: var(--color-text-secondary);
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  font-size: var(--text-xs);
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  max-height: 200px;
  overflow-y: auto;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* 展开/折叠过渡动画 */
.expand-enter-active,
.expand-leave-active {
  transition: all var(--duration-fast) var(--ease-out);
  overflow: hidden;
}

.expand-enter-from,
.expand-leave-to {
  opacity: 0;
  max-height: 0;
}

.expand-enter-to,
.expand-leave-from {
  opacity: 1;
  max-height: 300px;
}

/* Input Area */
.chat-input-area {
  position: relative;
  padding-top: var(--space-6);
}

.slash-autocomplete {
  position: absolute;
  bottom: 100%;
  left: 0;
  right: 0;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  margin-bottom: var(--space-4);
  max-height: 200px;
  overflow-y: auto;
  box-shadow: var(--shadow-lg);
}

.slash-item {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-4) var(--space-8);
  cursor: pointer;
  transition: background var(--duration-fast);
}

.slash-item:hover,
.slash-item.active {
  background: var(--color-surface-hover);
}

.slash-cmd {
  font-weight: var(--weight-semibold);
  color: var(--color-primary);
  min-width: 80px;
}

.slash-desc {
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}

.input-row {
  display: flex;
  gap: var(--space-4);
}

.chat-input {
  flex: 1;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  padding: var(--space-5) var(--space-10);
  color: var(--color-text);
  font-size: var(--text-base);
  outline: none;
  transition: all var(--duration-normal);
}

.chat-input:focus {
  border-color: var(--color-primary);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.15);
}

.send-btn {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: #fff;
  border: none;
  border-radius: var(--radius-full);
  padding: 0 var(--space-12);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal);
}

.send-btn:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px rgba(74, 124, 112, 0.3);
}

/* 发送按钮变身为停止按钮（AI 生成中 / broadcasting） */
.send-btn.stop-btn {
  background: rgba(231, 76, 60, 0.15);
  border: 1px solid rgba(231, 76, 60, 0.4);
  color: #e74c3c;
}
.send-btn.stop-btn:hover {
  box-shadow: 0 4px 12px rgba(231, 76, 60, 0.3);
}

.mic-btn {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  width: 40px;
  height: 40px;
  font-size: var(--text-lg);
  cursor: pointer;
  transition: all var(--duration-normal);
  flex-shrink: 0;
}

.mic-btn:hover:not(:disabled) {
  border-color: var(--color-primary);
}

.mic-btn.recording {
  background: rgba(231, 76, 60, 0.15);
  border-color: #e74c3c;
  color: #e74c3c;
  animation: mic-pulse 1.2s infinite;
}

.mic-btn.busy,
.mic-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@keyframes mic-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(231, 76, 60, 0.4); }
  50% { box-shadow: 0 0 0 6px rgba(231, 76, 60, 0); }
}

.voice-error {
  color: #e74c3c;
  font-size: var(--text-sm);
  margin-top: var(--space-2);
}

.connection-status {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  margin-top: var(--space-4);
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.ws-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-danger);}

.ws-dot.connected {
  background: var(--color-success);
}

/* 模型连通性浮层 */
.llm-popover {
  position: absolute;
  bottom: calc(100% + var(--space-3));
  left: 0;
  z-index: 50;
  min-width: 280px;
  padding: var(--space-4);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
}

.llm-popover-title {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-3);
  font-weight: var(--weight-medium);
}

.llm-popover-rows {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.llm-popover-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  font-size: var(--text-xs);
}

.llm-role-label {
  width: 32px;
  color: var(--color-text-muted);
  flex-shrink: 0;
}

.llm-model-name {
  flex: 1;
  color: var(--color-text);
  font-family: var(--font-mono, monospace);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.llm-source-tag {
  padding: 1px 6px;
  border-radius: var(--radius-full);
  font-size: 10px;
  flex-shrink: 0;
}
.llm-source-tag.user {
  background: rgba(74, 124, 112, 0.15);
  color: var(--color-primary);
}
.llm-source-tag.global {
  background: rgba(120, 120, 120, 0.15);
  color: var(--color-text-muted);
}

.llm-status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--color-text-muted);
  flex-shrink: 0;
}
.llm-status-dot.connected {
  background: var(--color-success);
}
.llm-status-dot.failed {
  background: var(--color-danger);
}

.llm-popover-empty {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  text-align: center;
  padding: var(--space-2);
}

/* popover 进出动画 */
.llm-popover-enter-active,
.llm-popover-leave-active {
  transition: opacity 0.15s ease, transform 0.15s ease;
}
.llm-popover-enter-from,
.llm-popover-leave-to {
  opacity: 0;
  transform: translateY(4px);
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

@media (max-width: 768px) {
  .chat-view {
    padding: var(--space-6);
  }
  .top-left-controls {
    left: calc(var(--sidebar-width-collapsed) + var(--space-6));
    top: var(--space-6);
  }
  .user-message .message-content,
  .assistant-message .message-content {
    max-width: 85%;
  }
}

/* Camera Modal */
.camera-modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg);
  backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-16);
}

.camera-modal {
  background: var(--color-bg-app);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-3xl);
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xl);
}

/* 固定头部+滚动主体:矮视口下弹窗内容超高时在 body 内滚动,
   而不是被 modal 的 overflow:hidden 直接裁掉不可达。min-height:0
   是 flex 列布局里允许子项收缩、滚动生效的前提。 */
.camera-modal-body {
  overflow-y: auto;
  min-height: 0;
}

.camera-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-12) var(--space-16);
  border-bottom: 1px solid var(--color-border);
}

.camera-modal-header h2 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin: 0;
}

.camera-header-actions {
  display: flex;
  align-items: center;
  gap: var(--space-6);
}

.camera-badge {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  padding: var(--space-2) var(--space-6);
  border-radius: var(--radius-full);
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-muted);
}

.camera-badge.active {
  background: var(--color-success-bg);
  color: var(--color-success);
}

.camera-modal-close {
  padding: var(--space-3) var(--space-10);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.camera-modal-close:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
}

.camera-stage {
  position: relative;
  padding: var(--space-12);
  background: var(--color-bg);
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: clamp(140px, 32vh, 300px);
}

/* 画面随 stage 实际高度 contain 缩放：stage 被 flex 压扁(短视口/下方面板多)时,
   固有尺寸的 <img> 会垂直居中对称溢出,盖住上方摄像头切换器拦截点击。 */
.camera-feed {
  width: 100%;
  height: 100%;
  max-height: 400px;
  object-fit: contain;
  border-radius: var(--radius-md);
}

.camera-feed.hidden {
  visibility: hidden;
}

.camera-disconnected {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-6);
  color: var(--color-text-secondary);
}

.camera-disconnected-icon {
  font-size: 40px;
  animation: pulse 1.5s ease-in-out infinite;
}

.camera-disconnected-text {
  font-size: var(--text-sm);
  text-align: center;
  padding: 0 var(--space-12);
}

.camera-retry-btn {
  padding: var(--space-4) var(--space-12);
  font-size: var(--text-sm);
  color: var(--color-text-primary);
  background: var(--color-surface-2, rgba(255, 255, 255, 0.12));
  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.2));
  border-radius: var(--radius-md, 8px);
  cursor: pointer;
  transition: background 0.15s ease;
}

.camera-retry-btn:hover {
  background: var(--color-surface-3, rgba(255, 255, 255, 0.2));
}

/* PTZ 云台控制面板 */
.ptz-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-10);
  padding: var(--space-12) var(--space-16);
  border-bottom: 1px solid var(--color-border);
}

/* D-pad：3×3 网格，上下左右居中，中间放原点 */
.ptz-dpad {
  display: grid;
  grid-template-columns: repeat(3, 48px);
  grid-template-rows: repeat(3, 48px);
  gap: var(--space-3);
}

.ptz-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  user-select: none;
  touch-action: none;  /* 阻止移动端滚动，让按住转向生效 */
  transition: background var(--duration-fast) var(--ease-out),
              border-color var(--duration-fast) var(--ease-out);
}

.ptz-btn:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
}

/* 按下态：用 :active 兜底，ptzMoving 期间也高亮 */
.ptz-btn:active,
.ptz-btn.pressing {
  background: var(--color-primary, #4f8cff);
  border-color: var(--color-primary, #4f8cff);
  color: #fff;
}

.ptz-up    { grid-area: 1 / 2; }
.ptz-left  { grid-area: 2 / 1; }
.ptz-right { grid-area: 2 / 3; }
.ptz-down  { grid-area: 3 / 2; }

.ptz-center {
  grid-area: 2 / 2;
  display: flex;
  align-items: center;
  justify-content: center;
}

.ptz-center-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-text-muted);
  opacity: 0.5;
}

.camera-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-4);
  padding: var(--space-12) var(--space-16);
  border-bottom: 1px solid var(--color-border);
}

.camera-stat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
}

.camera-stat .label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.camera-stat .value {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.camera-feedback {
  padding: var(--space-12) var(--space-16);
}

.camera-feedback .label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin-bottom: var(--space-4);
}

.camera-feedback .value {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: var(--leading-relaxed);
}

.camera-hint {
  padding: var(--space-8) var(--space-16);
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  background: rgba(74, 124, 112, 0.06);
  border-top: 1px solid var(--color-border);
  line-height: var(--leading-relaxed);
}

/* Modal Transition */
.modal-enter-active,
.modal-leave-active {
  transition: all 0.3s var(--ease-out);
}

.modal-enter-active .camera-modal,
.modal-leave-active .camera-modal {
  transition: all 0.3s var(--ease-out);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

.modal-enter-from .camera-modal,
.modal-leave-to .camera-modal {
  transform: scale(0.95) translateY(20px);
}
</style>
