/**
 * 语音输入 composable — 浏览器 MediaRecorder 录音 + 后端 STT 转文字。
 *
 * ChatView / DocChat 共用。点击按钮才开始 getUserMedia 申请麦克风权限
 * （最小权限，不主动弹窗）。录音用点击开始/再点击停止的交互；停止后音频
 * 以 multipart 上传到 /api/stt/transcribe，识别文本经 onResult 回调返回。
 *
 * 用原生 fetch + FormData（不能用 apiPost：它强设 JSON content-type 会破坏
 * multipart boundary）。auth cookie 由 main.js 全局 fetch 拦截器自动带上。
 */
import { ref } from 'vue'

/**
 * 麦克风/录音 API 是否可用。
 * 浏览器只在「安全上下文」暴露 navigator.mediaDevices：
 *   - https:// 任意主机
 *   - http://localhost / 127.0.0.1
 *   - file://
 * 用 http://<局域网IP> 访问时（如部署场景）navigator.mediaDevices 是 undefined，
 * 直接调 getUserMedia 会抛 "Cannot read properties of undefined (reading 'getUserMedia')"，
 * 这种底层 TypeError 对用户毫无意义，所以在 toggle 里提前拦截并给中文提示。
 */
const voiceAvailable = ref(
  typeof navigator !== 'undefined'
    && !!navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === 'function'
)

export function useVoiceInput({ onResult, onError } = {}) {
  const recording = ref(false)
  const transcribing = ref(false)
  // 暴露可用性，调用方可用它把麦克风按钮置灰/加 tooltip
  const available = voiceAvailable

  let mediaRecorder = null
  let stream = null
  let chunks = []

  async function toggle() {
    if (recording.value) {
      stop()
      return
    }
    // 非安全上下文（HTTP 非 localhost）：navigator.mediaDevices 不存在
    if (!available.value) {
      const scheme = typeof window !== 'undefined' && window.location
        ? window.location.protocol
        : 'unknown'
      onError?.(new Error(
        window.isSecureContext === false
          ? `麦克风不可用：当前为 ${scheme} 非安全上下文，浏览器只允许 HTTPS 或 localhost 使用麦克风`
          : '当前浏览器不支持麦克风录音（navigator.mediaDevices 不可用）'
      ))
      return
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e) {
      onError?.(e)
      return
    }
    chunks = []
    // 优先 webm；Safari 不支持时回落到浏览器默认 mime
    const mime = MediaRecorder.isTypeSupported('audio/webm')
      ? 'audio/webm'
      : ''
    mediaRecorder = mime
      ? new MediaRecorder(stream, { mimeType: mime })
      : new MediaRecorder(stream)
    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size > 0) chunks.push(e.data)
    }
    mediaRecorder.onstop = upload
    mediaRecorder.start()
    recording.value = true
  }

  function stop() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.stop()
    }
    if (stream) {
      stream.getTracks().forEach((t) => t.stop())
      stream = null
    }
    recording.value = false
  }

  async function upload() {
    if (chunks.length === 0) return
    transcribing.value = true
    try {
      const blob = new Blob(chunks, { type: mediaRecorder?.mimeType || 'audio/webm' })
      const fd = new FormData()
      fd.append('audio', blob, 'voice.webm')
      const res = await fetch('/api/stt/transcribe', { method: 'POST', body: fd })
      const json = await res.json()
      if (!res.ok) {
        throw new Error(json.message || json.detail || '语音识别失败')
      }
      const text = json.data?.text || ''
      if (text) onResult?.(text)
    } catch (e) {
      onError?.(e)
    } finally {
      transcribing.value = false
      chunks = []
    }
  }

  return { recording, transcribing, toggle, stop, available }
}
