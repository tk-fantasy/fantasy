<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { SS_CHAT_SESSION } from '../utils/constants'
import { toolIcon, summarizeToolCall, summarizeToolResult, parseToolResult } from '../utils/toolNames'
import { useVoiceInput } from '../composables/useVoiceInput'

const router = useRouter()

// 计算 video_feed URL（认证通过 cookie 自动处理）
const videoFeedUrl = ref('')
const videoFeedKey = ref(0)  // 用于强制刷新 img src
// 流断连状态：'live' 正常 | 'reconnecting' 重连中 | 'disconnected' 放弃
// 状态来源：'device'（设备掉线，由 /api/state 的 camera_opened 驱动）
//           'network'（HTTP 流断，由 <img> @error 驱动）
const feedStatus = ref('live')
const feedStatusSource = ref('network')
let feedRetryCount = 0
let feedRetryTimer = null
const FEED_MAX_RETRIES = 10  // 设备掉线时最多重连 10 次（指数退避后约 5 分钟）
let prevCameraOpened = null  // 上一拍设备状态，用于检测 false→true 翻转

function refreshVideoFeed() {
  feedRetryCount = 0
  feedStatus.value = 'live'
  feedStatusSource.value = 'network'
  videoFeedKey.value++
  videoFeedUrl.value = `/api/video_feed?_t=${videoFeedKey.value}`
}

function onVideoFeedError() {
  // 防御：模态框已关闭则不再重连
  if (!showCamera.value) return
  if (feedRetryTimer) clearTimeout(feedRetryTimer)

  // 设备仍在线（camera_opened===true）→ 纯流/网络抖动，永不放弃，持续自愈。
  // 仅当设备也掉了（camera_opened===false）且重试耗尽才进入终态。
  if (feedRetryCount >= FEED_MAX_RETRIES && cameraState.value?.camera_opened === false) {
    feedStatus.value = 'disconnected'
    return
  }
  feedRetryCount++
  feedStatus.value = 'reconnecting'
  feedStatusSource.value = 'network'
  // 指数退避：1s, 2s, 4s, ... 封顶 30s，避免拔掉后狂刷 src
  const delay = Math.min(1000 * (2 ** (feedRetryCount - 1)), 30000)
  feedRetryTimer = setTimeout(() => {
    if (!showCamera.value) return
    videoFeedKey.value++
    videoFeedUrl.value = `/api/video_feed?_t=${videoFeedKey.value}`
  }, delay)
}

// 帧到达说明流恢复了，重置重连计数
function onVideoFeedLoad() {
  if (feedStatus.value !== 'live') {
    feedStatus.value = 'live'
    feedRetryCount = 0
  }
}

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
const pendingToolCalls = ref([])
let currentStreamingMsg = null
let ws = null
let reconnectTimer = null

const SESSION_STORAGE_KEY = SS_CHAT_SESSION
const showGreeting = ref(false)
const greetingText = ref('')
let greetingTimer = null

// Slash command autocomplete
const showSlashMenu = ref(false)
const slashIndex = ref(0)
const slashFiltered = ref([])

const SLASH_COMMANDS = [
  { cmd: '/undo', desc: '撤销上一轮对话', action: 'api', handler: doUndo },
  { cmd: '/clear', desc: '清空当前会话消息', action: 'api', handler: doClear },
  { cmd: '/compress', desc: '压缩当前上下文生成摘要', action: 'api', handler: doCompress },
  { cmd: '/new', desc: '创建新会话', action: 'fn', handler: doNewSession },
  { cmd: '/camera', desc: '打开摄像头预览', action: 'fn', handler: openCamera },
  { cmd: '/halist', desc: '查看智能家居设备', action: 'nav', url: '/halist' },
  { cmd: '/task', desc: '查看自动化规则', action: 'nav', url: '/task' },
  { cmd: '/scheduled', desc: '查看定时任务', action: 'nav', url: '/scheduled' },
  { cmd: '/models', desc: '模型配置与切换', action: 'nav', url: '/models' },
  { cmd: '/focus', desc: '设置视觉关注重点', action: 'nav', url: '/focus' },
  { cmd: '/sessions', desc: '浏览并切换历史会话', action: 'nav', url: '/sessions' },
  { cmd: '/doc', desc: '打开RAG文档助手', action: 'nav', url: '/doc' },
  { cmd: '/sg', desc: '构建与管理语义图', action: 'nav', url: '/sg' },
  { cmd: '/monitor', desc: '查看系统监控', action: 'nav', url: '/monitor' },
]

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
    // 1008 = Policy Violation (认证失败)，不重连
    if (event.code === 1008) {
      console.warn('WebSocket closed due to authentication failure, not reconnecting')
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
      finalizeStreaming()
      break
  }
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
function sendMessage() {
  const text = inputText.value.trim()
  if (!text) return

  // Check slash commands
  if (text.startsWith('/')) {
    const cmd = SLASH_COMMANDS.find(c => c.cmd === text)
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

  // Send via WebSocket
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({
      type: 'chat',
      query: text,
      session_id: sessionId.value,
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
    slashFiltered.value = SLASH_COMMANDS.filter(c =>
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
      sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId.value)
    }
    messages.value = []
    pendingToolCalls.value = []
    currentStreamingMsg = null
    messages.value.push({ role: 'system', content: '已创建新会话' })
  } catch (e) {
    messages.value.push({ role: 'system', content: `创建会话失败: ${e.message}` })
  }
}

// ============ Camera ============
const showCamera = ref(false)
const cameraState = ref(null)
let cameraPollTimer = null

function openCamera() {
  showCamera.value = true
  prevCameraOpened = null  // 重开弹窗时清空基准，避免误判设备翻转
  refreshVideoFeed()  // 打开时刷新视频流 URL
  startCameraPolling()
  fetchPtzStatus()  // 拉取 PTZ 启用状态和预置点
}

function closeCamera() {
  showCamera.value = false
  stopCameraPolling()
  if (feedRetryTimer) {
    clearTimeout(feedRetryTimer)
    feedRetryTimer = null
  }
}

async function fetchCameraState() {
  try {
    const res = await fetch('/api/state')
    const json = await res.json()
    cameraState.value = json.data || json
    syncFeedStatusWithDevice()
  } catch (e) {
    console.error('Failed to fetch camera state:', e)
  }
}

// 把后端设备状态（camera_opened）接入 feedStatus 状态机。
// 设备掉线（camera_opened===false）：显示“设备重连中”，但不动 <img> src——
//   缓存的末帧继续沿原 keepalive 连接显示，比反复重建 src 闪烁更稳。
// 设备恢复（camera_opened 由 false 翻回 true）：强制重建 src 让新帧立刻流入，
//   替代用户手动刷新；同时清掉任何“流断/断开”状态。
// 流断但设备在线（feedStatus==='disconnected' 且 camera_opened===true）：
//   自动 refreshVideoFeed() 自愈，避免卡死在断开态需整页刷新。
function syncFeedStatusWithDevice() {
  if (!showCamera.value) return
  const opened = cameraState.value?.camera_opened === true
  if (!opened) {
    // 设备掉了：进入“设备重连中”（若已是 disconnected 则保留终态，等设备回来）
    if (feedStatus.value !== 'disconnected' && feedStatus.value !== 'reconnecting') {
      feedStatus.value = 'reconnecting'
      feedStatusSource.value = 'device'
    } else if (feedStatus.value === 'reconnecting' && feedStatusSource.value === 'network') {
      // 网络重连未完时设备也掉了，统一归到“设备重连中”
      feedStatusSource.value = 'device'
    }
  } else if (prevCameraOpened === false) {
    // 设备刚恢复（false→true）：强制重建 src，新帧立刻流入
    refreshVideoFeed()
  } else if (feedStatus.value === 'disconnected') {
    // 设备一直在线，只是流断到了终态 → 自愈
    refreshVideoFeed()
  }
  prevCameraOpened = opened
}

function startCameraPolling() {
  fetchCameraState()
  cameraPollTimer = setInterval(fetchCameraState, 2000)
}

function stopCameraPolling() {
  if (cameraPollTimer) {
    clearInterval(cameraPollTimer)
    cameraPollTimer = null
  }
}

// ============ PTZ 云台控制 ============
// 点一下方向键 → POST /ptz/step：后端 ContinuousMove 一小段后自动 Stop，
// 实现"按一下动一下"。停转由后端保证（即使关页面也会停），不依赖 pointerup，
// 避免松手事件丢失导致摄像头转飞。
const ptzEnabled = ref(false)
const ptzMoving = ref(false)   // 步进冷却中，忽略连点
const ptzStepMs = ref(300)     // 单步时长(ms)，与后端 ptz.step_ms 一致
let ptzCooldownTimer = null

async function fetchPtzStatus() {
  try {
    const res = await fetch('/api/ptz/status')
    const json = await res.json()
    const data = json.data || json
    ptzEnabled.value = !!data.enabled
    if (data.step_ms) ptzStepMs.value = Number(data.step_ms) || 300
  } catch (e) {
    console.error('Failed to fetch PTZ status:', e)
  }
}

// 单击步进：发一次 move，后端到点自动 stop。冷却期内忽略新点击，
// 避免步与步的 ONVIF 指令堆叠（后端 _lock 也会串行，双保险）。
function ptzStep(direction) {
  if (!ptzEnabled.value || ptzMoving.value) return
  ptzMoving.value = true
  if (ptzCooldownTimer) { clearTimeout(ptzCooldownTimer); ptzCooldownTimer = null }
  fetch('/api/ptz/step', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ direction }),
  }).catch(e => console.error('PTZ step failed:', e))
  ptzCooldownTimer = setTimeout(() => {
    ptzCooldownTimer = null
    ptzMoving.value = false
  }, ptzStepMs.value)
}

// ============ Session History ============
async function loadSessionHistory(sid) {
  if (!sid) return
  try {
    const res = await fetch(`/api/sessions/${sid}`)
    if (res.status === 404) {
      // Session no longer exists (e.g. DB reset) — clear stale ID, create fresh
      sessionStorage.removeItem(SESSION_STORAGE_KEY)
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

// ============ Greeting ============
function getGreeting(ownerName) {
  const hour = new Date().getHours()
  let timeGreeting = ''
  
  if (hour >= 5 && hour < 12) {
    timeGreeting = '早上好'
  } else if (hour >= 12 && hour < 14) {
    timeGreeting = '中午好'
  } else if (hour >= 14 && hour < 18) {
    timeGreeting = '下午好'
  } else {
    timeGreeting = '晚上好'
  }
  
  return ownerName ? `${timeGreeting}，${ownerName}` : timeGreeting
}

async function showGreetingMessage() {
  try {
    const res = await fetch('/api/home/info')
    const json = await res.json()
    const ownerName = json.data?.owner_name || ''
    
    greetingText.value = getGreeting(ownerName)
    showGreeting.value = true
    
    // 1.5秒后开始淡化
    greetingTimer = setTimeout(() => {
      showGreeting.value = false
    }, 1500)
  } catch (e) {
    console.error('Failed to load home info for greeting:', e)
  }
}

// ============ Lifecycle ============
onMounted(async () => {
  connectWS()
  
  // 只有从 Landing 进入时才显示问候
  const shouldShowGreeting = sessionStorage.getItem('aether-show-greeting')
  if (shouldShowGreeting) {
    sessionStorage.removeItem('aether-show-greeting')
    showGreetingMessage()
  }

  // 1. 先检查 URL 参数
  const urlParams = new URLSearchParams(window.location.search)
  const urlSessionId = urlParams.get('session')

  // 2. 再检查 sessionStorage
  const savedSessionId = sessionStorage.getItem(SESSION_STORAGE_KEY)

  // 3. 优先使用 URL 参数，其次 sessionStorage，最后创建新的
  if (urlSessionId) {
    sessionId.value = urlSessionId
    sessionStorage.setItem(SESSION_STORAGE_KEY, urlSessionId)
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
        sessionStorage.setItem(SESSION_STORAGE_KEY, sessionId.value)
      }
    } catch (e) {
      console.error('Failed to create session:', e)
    }
  }
})

onUnmounted(() => {
  if (ws) ws.close()
  if (reconnectTimer) clearTimeout(reconnectTimer)
  if (greetingTimer) clearTimeout(greetingTimer)
  if (feedRetryTimer) clearTimeout(feedRetryTimer)
  if (ptzCooldownTimer) clearTimeout(ptzCooldownTimer)
  stopCameraPolling()
  // 保持 sessionId 在 sessionStorage 中，下次进入可恢复
})
</script>

<template>
  <div class="chat-view">
    <!-- Top Left Controls -->
    <div class="top-left-controls">
    </div>

    <!-- Status Bar -->
    <div class="status-bar" v-if="statusPhase">
      <div class="status-dot" :class="statusPhase"></div>
      <span class="status-text">{{ statusText }}</span>
    </div>

    <!-- Messages -->
    <div class="chat-messages">
      <!-- Greeting Overlay -->
      <Transition name="greeting-fade">
        <div v-if="showGreeting" class="greeting-overlay">
          <div class="greeting-text">{{ greetingText }}</div>
        </div>
      </Transition>

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
        <button @click="sendMessage" class="send-btn">发送</button>
      </div>
      <div class="voice-error" v-if="voiceError">{{ voiceError }}</div>
      <div class="connection-status">
        <span class="ws-dot" :class="{ connected: wsConnected }"></span>
        <span>{{ wsConnected ? '已连接' : '未连接' }}</span>
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
          </div>
        </div>
      </Transition>
    </Teleport>
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

/* Greeting Overlay */
.greeting-overlay {
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.3);
  backdrop-filter: blur(20px);
  -webkit-backdrop-filter: blur(20px);
  pointer-events: none;
}

.greeting-text {
  font-size: 48px;
  font-weight: 600;
  color: #fff;
  text-shadow: 0 4px 16px rgba(0, 0, 0, 0.5);
  white-space: nowrap;
  animation: greetingPulse 1.5s ease-in-out;
}

@keyframes greetingPulse {
  0% { transform: scale(0.8); opacity: 0; }
  50% { transform: scale(1.05); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
}

.greeting-fade-enter-active {
  transition: opacity 0.4s ease-out;
}

.greeting-fade-leave-active {
  transition: opacity 0.8s ease-out;
}

.greeting-fade-enter-from,
.greeting-fade-leave-to {
  opacity: 0;
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
  background: rgba(0, 0, 0, 0.15);
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
  background: var(--color-danger);
}

.ws-dot.connected {
  background: var(--color-success);
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
  background: rgba(0, 0, 0, 0.7);
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
  padding: var(--space-12);
  background: #000;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 300px;
}

.camera-feed {
  max-width: 100%;
  max-height: 400px;
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
