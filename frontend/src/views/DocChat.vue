<script setup>
import { ref, computed, onMounted, onUnmounted, nextTick } from 'vue'
import { useRouter } from 'vue-router'
import { useVoiceInput } from '../composables/useVoiceInput'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

const router = useRouter()

// Markdown -> safe HTML（与 NodeDetail 一致：marked 解析 + DOMPurify 消毒）
function renderMd(text) {
  if (!text) return ''
  return DOMPurify.sanitize(marked.parse(text))
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
const wsConnected = ref(false)
const statusPhase = ref('')
let currentStreamingMsg = null
let ws = null
let reconnectTimer = null

// Slash command autocomplete
const showSlashMenu = ref(false)
const slashIndex = ref(0)
const slashFiltered = ref([])

const SLASH_COMMANDS = []

const statusText = computed(() => {
  switch (statusPhase.value) {
    case 'thinking': return '正在思考...'
    default: return ''
  }
})

// ============ WebSocket ============
function connectWS() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  // WS 鉴权靠同源请求自动携带的 httpOnly cookie（aether_token），
  // JS 无法读取该 cookie，故不在 URL 拼 token。
  const wsUrl = `${protocol}//${window.location.host}/ws/doc/chat`

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
      const msg = JSON.parse(event.data)
      if (msg.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }))
        return
      }
      handleInstruction(msg)
    } catch (e) {
      console.error('Failed to parse WS message:', e)
    }
  }
}

function handleInstruction(msg) {
  switch (msg.type) {
    case 'token':
      appendToken(msg.content)
      break
    case 'error':
      messages.value.push({ role: 'system', content: `错误: ${msg.message}` })
      finalizeStreaming()
      break
    case 'done':
      finalizeStreaming()
      break
  }
}

function appendToken(token) {
  const lastMsg = messages.value[messages.value.length - 1]
  if (lastMsg && lastMsg.role === 'assistant' && lastMsg.streaming) {
    lastMsg.content += token
  } else {
    const newMsg = { role: 'assistant', content: token, streaming: true }
    messages.value.push(newMsg)
    currentStreamingMsg = newMsg
  }
  scrollToBottom()
}

function finalizeStreaming() {
  const lastMsg = messages.value[messages.value.length - 1]
  if (lastMsg && lastMsg.streaming) {
    lastMsg.streaming = false
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

  if (text.startsWith('/')) {
    const cmd = SLASH_COMMANDS.find(c => c.cmd === text)
    if (cmd) {
      executeSlashCommand(cmd)
      inputText.value = ''
      return
    }
  }

  messages.value.push({ role: 'user', content: text })
  inputText.value = ''

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ query: text }))
  } else {
    messages.value.push({ role: 'system', content: 'WebSocket 未连接，请刷新页面重试。' })
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

// ============ Lifecycle ============
onMounted(() => {
  connectWS()
})

onUnmounted(() => {
  if (ws) ws.close()
  if (reconnectTimer) clearTimeout(reconnectTimer)
})
</script>

<template>
  <div class="chat-view">
    <!-- Page Title -->
    <div class="page-title-bar">
      <h1 class="page-title-text">Aether使用助手</h1>
    </div>

    <!-- Top Left Controls -->
    <div class="top-left-controls">
      <button class="kg-btn" @click="router.push('/doc/KGraph')">语义图</button>
    </div>

    <!-- Status Bar -->
    <div class="status-bar" v-if="statusPhase">
      <div class="status-dot" :class="statusPhase"></div>
      <span class="status-text">{{ statusText }}</span>
    </div>

    <!-- Messages -->
    <div class="chat-messages">
        <div v-if="!messages.length" class="empty-state">
          <div class="empty-icon">&#128214;</div>
          <p>文档使用助手</p>
          <p class="empty-hint">输入问题，基于项目文档为你解答</p>
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
            <div class="message-text markdown-body" v-html="renderMd(msg.content)"></div>
            <span v-if="msg.streaming" class="streaming-indicator">|</span>
          </div>
        </div>

        <!-- System Message -->
        <div v-else-if="msg.role === 'system'" class="message system-message">
          {{ msg.content }}
        </div>
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

/* Page Title */
.page-title-bar {
  position: fixed;
  top: var(--space-10);
  left: 50%;
  transform: translateX(-50%);
  z-index: 100;
}

.page-title-text {
  font-size: var(--text-2xl);
  font-weight: var(--weight-bold);
  color: var(--color-text);
  margin: 0;
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
  word-break: break-word;
  line-height: var(--leading-relaxed);
}

/* Markdown 渲染样式（与 NodeDetail 一致） */
.markdown-body { font-size: var(--text-base); line-height: 1.7; }
.markdown-body :deep(h1) { font-size: var(--text-2xl); margin: 0 0 12px; border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.markdown-body :deep(h2) { font-size: var(--text-xl); margin: 20px 0 10px; }
.markdown-body :deep(h3) { font-size: var(--text-lg); margin: 16px 0 8px; }
.markdown-body :deep(p) { margin: 8px 0; }
.markdown-body :deep(code) { background: rgba(0,0,0,0.3); padding: 2px 6px; border-radius: 4px; font-size: var(--text-sm); font-family: var(--font-mono); }
.markdown-body :deep(pre) { background: rgba(0,0,0,0.3); padding: 12px; border-radius: var(--radius-sm); overflow-x: auto; border: 1px solid var(--color-border); margin: 12px 0; }
.markdown-body :deep(pre code) { background: none; padding: 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 20px; margin: 8px 0; }
.markdown-body :deep(li) { margin: 4px 0; }
.markdown-body :deep(a) { color: var(--color-primary); text-decoration: none; }
.markdown-body :deep(a:hover) { text-decoration: underline; }
.markdown-body :deep(blockquote) { border-left: 3px solid var(--color-primary); margin: 12px 0; padding: 8px 16px; background: var(--color-primary-light); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
.markdown-body :deep(table) { width: 100%; border-collapse: collapse; margin: 12px 0; }
.markdown-body :deep(th), .markdown-body :deep(td) { padding: 8px 12px; border: 1px solid var(--color-border); text-align: left; }
.markdown-body :deep(th) { background: rgba(0,0,0,0.2); font-weight: 600; }
.markdown-body :deep(hr) { border: none; border-top: 1px solid var(--color-border); margin: 16px 0; }

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

.kg-btn {
  background: none;
  border: 1px solid var(--color-border-active);
  color: var(--color-primary);
  padding: 4px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--text-sm);
}
.kg-btn:hover {
  background: var(--color-primary-light);
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
</style>
