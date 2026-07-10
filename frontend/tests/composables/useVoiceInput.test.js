import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useVoiceInput } from '../../src/composables/useVoiceInput'

// MediaRecorder 恒桩：jsdom 没有真实实现，业务逻辑只依赖 state/ondataavailable/onstop
class MockMediaRecorder {
  constructor(stream, opts) {
    this.stream = stream
    this.mimeType = opts?.mimeType || 'audio/webm'
    this.state = 'inactive'
  }
  start() { this.state = 'recording' }
  stop() {
    this.state = 'inactive'
    this.ondataavailable?.({ data: new Blob(['x'], { type: this.mimeType }) })
    this.onstop?.()
  }
}
global.MediaRecorder = MockMediaRecorder
MockMediaRecorder.isTypeSupported = () => true

// getUserMedia 恒桩
const getTracks = () => [{ stop: vi.fn() }]
let mockStream = { getTracks }
Object.defineProperty(global.navigator, 'mediaDevices', {
  value: { getUserMedia: vi.fn(() => Promise.resolve(mockStream)) },
  configurable: true,
})

global.fetch = vi.fn()

describe('useVoiceInput', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockStream = { getTracks }
    navigator.mediaDevices.getUserMedia = vi.fn(() => Promise.resolve(mockStream))
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('初始状态：未录音、未转写，暴露 toggle/stop', () => {
    const v = useVoiceInput()
    expect(v.recording.value).toBe(false)
    expect(v.transcribing.value).toBe(false)
    expect(typeof v.toggle).toBe('function')
    expect(typeof v.stop).toBe('function')
  })

  it('toggle 开始录音后 recording 变 true', async () => {
    const v = useVoiceInput()
    await v.toggle()
    expect(v.recording.value).toBe(true)
    expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({ audio: true })
  })

  it('再次 toggle 停止录音，上传成功后回调 onResult', async () => {
    const onResult = vi.fn()
    const v = useVoiceInput({ onResult })
    await v.toggle()
    expect(v.recording.value).toBe(true)

    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: { text: '你好世界' } }),
    })

    await v.toggle() // 停止 → 触发 onstop → upload
    expect(v.recording.value).toBe(false)
    // upload 是 async，等一个微任务
    await Promise.resolve()
    expect(fetch).toHaveBeenCalledWith('/api/stt/transcribe', expect.objectContaining({ method: 'POST' }))
    expect(onResult).toHaveBeenCalledWith('你好世界')
  })

  it('麦克风权限拒绝时回调 onError 且不录音', async () => {
    const onError = vi.fn()
    navigator.mediaDevices.getUserMedia = vi.fn(() =>
      Promise.reject(Object.assign(new Error('denied'), { name: 'NotAllowedError' }))
    )
    const v = useVoiceInput({ onError })

    await v.toggle()
    expect(v.recording.value).toBe(false)
    expect(onError).toHaveBeenCalled()
    expect(onError.mock.calls[0][0].name).toBe('NotAllowedError')
  })

  it('上游返回非 ok 时回调 onError', async () => {
    const onError = vi.fn()
    const v = useVoiceInput({ onError })
    await v.toggle()

    fetch.mockResolvedValueOnce({
      ok: false,
      json: () => Promise.resolve({ message: '语音识别失败' }),
    })

    await v.toggle()
    await Promise.resolve()
    expect(onError).toHaveBeenCalled()
  })
})
