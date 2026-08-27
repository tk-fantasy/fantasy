<template>
  <div class="test-camera-panel">
    <div class="panel-header">
      <span class="title">🎬 测试摄像头</span>
      <span class="camera-chip" v-if="status.camera_id">{{ status.camera_id }}</span>
    </div>

    <!-- 上传即播 -->
    <div class="section">
      <div class="section-title">上传视频（上传后自动循环播放，替换当前视频）</div>
      <div class="add-row">
        <input
          ref="fileInput"
          type="file"
          accept="video/*,.mkv,.flv,.ts"
          style="display: none"
          @change="onFileChosen"
        />
        <button class="btn" @click="uploadLocalFile" :disabled="uploading">
          {{ uploading ? `上传中…${uploadPct !== null ? ' ' + uploadPct + '%' : ''}` : '选择视频上传' }}
        </button>
        <button class="btn" @click="restartPlayback" :disabled="!status.current">重播</button>
      </div>
      <div v-if="uploadError" class="error-text">{{ uploadError }}</div>
      <div v-if="status.current" class="current-video">
        正在循环播放：<b>{{ status.current.name }}</b>
        <span v-if="status.current.duration_s" class="video-meta">
          （{{ status.current.duration_s }}s · 已推 {{ status.sent }} 帧<template v-if="status.dropped"> · 丢 {{ status.dropped }}</template>）
        </span>
      </div>
      <div v-else-if="!uploading" class="empty-text">尚未播放视频。上传一个即开始（上传目录中的旧视频会自动清理）。</div>
    </div>

    <!-- 真实执行开关 -->
    <div class="section">
      <div class="section-title danger-title">动作执行模式</div>
      <div class="toggle-row" @click="toggleRealExec">
        <span class="row-label">
          {{ config.real_exec ? '⚠️ 真实执行（会真的控制设备！）' : '🧪 演练模式（默认，只记录不执行）' }}
        </span>
        <span class="toggle-pill" :class="{ on: config.real_exec, danger: config.real_exec }">
          <span class="toggle-dot"></span>
        </span>
      </div>
      <div class="hint-text" v-if="config.real_exec">
        规则条件命中后将真实调用 Home Assistant 服务，请确认测试环境安全。
      </div>
    </div>

    <!-- 识别日志 -->
    <div class="section">
      <div class="section-title">识别日志（模型识别到了什么）</div>
        <div class="log-list">
        <div v-for="log in logs" :key="log.id" class="log-item" :class="log.kind">
          <span class="log-time">{{ formatTime(log.created_at) }}</span>
          <span class="log-kind" :class="log.kind">{{ kindLabel(log.kind) }}</span>
          <span class="log-content">{{ logText(log) }}</span>
        </div>
        <div v-if="!logs.length" class="empty-text">暂无识别记录。视频播放触发运动/心跳推理后在此显示。</div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiPost } from '@/utils/api'

const PLUGIN = 'test-camera'
const fileInput = ref(null)
const uploading = ref(false)
const uploadPct = ref(null)
const uploadError = ref('')
const status = ref({ camera_id: '', current: null, playing: false, sent: 0, dropped: 0 })
const config = ref({ real_exec: false, camera_name: '测试摄像头', camera_id: '' })
const logs = ref([])
let logTimer = null
let statusTimer = null

function pluginMethod(method, params = {}) {
  return apiPost(`/api/integrations/${PLUGIN}/method/${method}`, { params })
}

async function refreshStatus() {
  try {
    const data = await pluginMethod('playback.status')
    if (data) status.value = { ...status.value, ...data }
  } catch (e) {
    console.warn('load status failed:', e)
  }
}

async function refreshConfig() {
  try {
    const data = await pluginMethod('config.get')
    if (data) config.value = { ...config.value, ...data }
  } catch (e) {
    console.warn('load config failed:', e)
  }
}

async function refreshLogs() {
  const cam = config.value.camera_id || status.value.camera_id
  if (!cam) return
  try {
    const rows = await apiGet(`/api/vision-logs?camera_id=${encodeURIComponent(cam)}&limit=50`)
    logs.value = rows || []
  } catch {
    // 数据库/权限异常静默（面板仍可用）
  }
}

function uploadLocalFile() {
  fileInput.value?.click()
}

async function onFileChosen(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = ''  // 允许重复选同一文件
  if (!file) return
  uploading.value = true
  uploadError.value = ''
  uploadPct.value = 0
  try {
    // XMLHttpRequest 带 upload.onprogress；cookie 鉴权与 apiPost 一致
    const result = await new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest()
      xhr.open('POST', `/api/integrations/${PLUGIN}/files`)
      xhr.withCredentials = true
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable) uploadPct.value = Math.round((e.loaded / e.total) * 100)
      }
      xhr.onload = () => {
        try {
          const json = JSON.parse(xhr.responseText)
          // 注意：XHR 没有 fetch 的 ok 属性，用 status 判定（曾因此把成功当失败，
          // 上传 200 却 reject "HTTP 200"，后续 playback.set 永不执行）
          const okStatus = xhr.status >= 200 && xhr.status < 300
          if (okStatus && json.success !== false) resolve(json.data ?? json)
          else reject(new Error(json?.message || `HTTP ${xhr.status}`))
        } catch {
          reject(new Error(`HTTP ${xhr.status}`))
        }
      }
      xhr.onerror = () => reject(new Error('网络错误'))
      const fd = new FormData()
      fd.append('file', file, file.name)
      xhr.send(fd)
    })
    // 上传完成 → 设为当前播放（插件会清理上传目录里的旧视频并起播）
    const started = await pluginMethod('playback.set', {
      path: result.path, name: result.name || file.name,
    })
    if (started?.error) {
      uploadError.value = started.error
    }
    await refreshStatus()
  } catch (e) {
    uploadError.value = String(e?.message || e)
  } finally {
    uploading.value = false
    uploadPct.value = null
  }
}

async function restartPlayback() {
  try {
    await pluginMethod('playback.restart')
  } catch (e) {
    console.warn('restart failed:', e)
  }
}

async function toggleRealExec() {
  const next = !config.value.real_exec
  try {
    await pluginMethod('config.set', { real_exec: next })
    config.value.real_exec = next
  } catch (e) {
    console.warn('set real_exec failed:', e)
  }
}

function kindLabel(kind) {
  if (kind === 'preview') return '识别'
  if (kind === 'rule_eval') return '规则'
  if (kind === 'action') return '动作'
  return kind
}

function logText(log) {
  const c = log.content || {}
  if (log.kind === 'preview') return c.feedback || c.event || ''
  if (log.kind === 'rule_eval') {
    return `${c.result === 1 || c.result === '1' ? '✓ 满足' : '· 不满足'}「${c.condition || ''}」`
  }
  if (log.kind === 'action') {
    const head = c.dry_run ? '[演练] 将执行' : (c.error ? '执行失败' : '已执行')
    const inp = c.input || {}
    return `${head} ${c.tool || ''} ${inp.entity_id || ''}`.trim()
  }
  return JSON.stringify(c)
}

function formatTime(ms) {
  if (!ms) return ''
  const d = new Date(ms)
  const p = n => String(n).padStart(2, '0')
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

onMounted(async () => {
  await Promise.all([refreshConfig(), refreshStatus()])
  await refreshLogs()
  startPolling()
  // 页面切后台时停掉轮询：面板常驻聊天页，状态/留痕列表不需要
  // 后台刷新，白吃全局限流额度（120 次/分钟/IP）。
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  stopPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})

function startPolling() {
  if (logTimer || statusTimer) return
  // 10s 档：状态计数/识别留痕都不需要秒级新鲜度，轮询过密会挤占
  // 全局限流额度，挤压 video_feed 触发"重连中"风暴
  logTimer = setInterval(refreshLogs, 10000)
  statusTimer = setInterval(refreshStatus, 10000)
}

function stopPolling() {
  if (logTimer) clearInterval(logTimer)
  if (statusTimer) clearInterval(statusTimer)
  logTimer = null
  statusTimer = null
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') {
    refreshStatus()
    refreshLogs()
    startPolling()
  } else {
    stopPolling()
  }
}
</script>

<style scoped>
.test-camera-panel {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  background: rgba(255, 255, 255, 0.03);
  font-size: var(--text-sm);
  text-align: left;
}
.panel-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.title { font-weight: var(--weight-medium); color: var(--color-text); }
.camera-chip {
  padding: 1px 8px;
  border-radius: 999px;
  background: rgba(74, 124, 112, 0.18);
  color: var(--color-text-secondary);
  font-size: var(--text-xs, 12px);
}
.section { margin-bottom: 14px; }
.section-title {
  color: var(--color-text-tertiary);
  font-size: var(--text-xs, 12px);
  margin-bottom: 6px;
}
.danger-title { color: #d9822b; }
.add-row { display: flex; gap: 8px; }
.btn {
  padding: 6px 14px;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  background: rgba(74, 124, 112, 0.25);
  color: var(--color-text);
  cursor: pointer;
  font-family: inherit;
  font-size: var(--text-sm);
}
.btn:hover { border-color: var(--color-border-active); }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.current-video {
  margin-top: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: rgba(74, 124, 112, 0.15);
  color: var(--color-text);
}
.video-meta { color: var(--color-text-tertiary); font-size: var(--text-xs, 12px); }
.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px;
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.03);
  cursor: pointer;
}
.toggle-pill {
  width: 36px; height: 20px; border-radius: 10px;
  background: rgba(255, 255, 255, 0.12);
  display: flex; align-items: center; padding: 2px; flex-shrink: 0;
  transition: background var(--duration-fast) var(--ease-out);
}
.toggle-pill.on { background: rgba(74, 124, 112, 0.6); }
.toggle-pill.on.danger { background: rgba(190, 80, 60, 0.75); }
.toggle-dot { width: 16px; height: 16px; border-radius: 50%; background: #fff; transition: transform var(--duration-fast) var(--ease-out); }
.toggle-pill.on .toggle-dot { transform: translateX(16px); }
.hint-text { margin-top: 6px; color: #d9822b; font-size: var(--text-xs, 12px); }
.error-text { margin-top: 4px; color: #d96060; font-size: var(--text-xs, 12px); }
.empty-text { color: var(--color-text-tertiary); font-size: var(--text-xs, 12px); padding: 6px 2px; }
.log-list { display: flex; flex-direction: column; gap: 3px; max-height: 180px; overflow-y: auto; }
.log-item { display: flex; gap: 8px; align-items: baseline; font-size: var(--text-xs, 12px); }
.log-time { color: var(--color-text-tertiary); flex-shrink: 0; }
.log-kind {
  flex-shrink: 0;
  padding: 0 6px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.08);
  color: var(--color-text-secondary);
}
.log-kind.rule_eval { background: rgba(90, 130, 200, 0.22); }
.log-kind.action { background: rgba(190, 130, 60, 0.25); }
.log-content { color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
