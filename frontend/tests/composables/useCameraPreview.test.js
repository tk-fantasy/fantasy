import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref } from 'vue'
import { useCameraPreview } from '../../src/composables/useCameraPreview'

global.fetch = vi.fn()

function okJson(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

/** 建一个受控的 composable：注入 cameras/loading 桩 + 可手动触发 onVideoFeedError */
function makePreview(camerasList = []) {
  const activeCameraId = ref(null)
  const cameras = ref(camerasList)
  const loadCameras = vi.fn(async () => {})
  const api = useCameraPreview(activeCameraId, cameras, loadCameras)
  return { ...api, activeCameraId, cameras, loadCameras }
}

describe('useCameraPreview', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })

  afterEach(() => {
    // 清掉轮询 interval / 重连 setTimeout，防止跨用例泄漏
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('refreshVideoFeed：多路走 per-camera URL，key 自增强制刷新', () => {
    const p = makePreview()
    p.activeCameraId.value = 'cam1'

    p.refreshVideoFeed()
    expect(p.videoFeedUrl.value).toBe('/api/cameras/cam1/video_feed?_t=1')

    p.refreshVideoFeed() // 再刷一次 key++，img src 必然变化
    expect(p.videoFeedUrl.value).toBe('/api/cameras/cam1/video_feed?_t=2')
    expect(p.feedStatus.value).toBe('live')
    expect(p.feedRetryCount.value).toBe(0)
  })

  it('refreshVideoFeed：无选中摄像头不构造 URL（单摄兼容端点已删）', () => {
    const p = makePreview()
    p.refreshVideoFeed()
    expect(p.videoFeedUrl.value).toBe('')
  })

  it('fetchCameraState：无选中摄像头不发请求', async () => {
    fetch.mockResolvedValue(okJson({}))
    const p = makePreview()
    await p.fetchCameraState()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('onVideoFeedError 进入 reconnecting 并按指数退避重连', async () => {
    const p = makePreview()
    p.showCamera.value = true
    p.activeCameraId.value = 'cam1'
    p.refreshVideoFeed()

    p.onVideoFeedError()
    expect(p.feedStatus.value).toBe('reconnecting')
    expect(p.feedRetryCount.value).toBe(1)

    // 1s 后重建 src（第二次 2s …）
    await vi.advanceTimersByTimeAsync(1000)
    expect(p.videoFeedUrl.value).toContain('/video_feed?_t=')

    // 连续失败两次 → 重试计数累计
    p.onVideoFeedError()
    p.onVideoFeedError()
    expect(p.feedRetryCount.value).toBe(3)
  })

  it('设备在线时永不放弃重连；设备离线且重试耗尽才 disconnected', async () => {
    const p = makePreview()
    p.showCamera.value = true
    p.activeCameraId.value = 'cam1'
    p.cameraState.value = { camera_opened: true } // 设备在线
    p.refreshVideoFeed()

    for (let i = 0; i < 12; i++) {
      p.onVideoFeedError()
      await vi.advanceTimersByTimeAsync(30000) // 封顶退避
    }
    expect(p.feedStatus.value).not.toBe('disconnected') // 纯流抖动自愈到底

    // 设备也掉了：耗尽后进入终态
    p.cameraState.value = { camera_opened: false }
    for (let i = 0; i < 11; i++) {
      p.onVideoFeedError()
      await vi.advanceTimersByTimeAsync(30000)
    }
    p.onVideoFeedError()
    expect(p.feedStatus.value).toBe('disconnected')
  })

  it('模态框关闭后 img error 不再安排重连', async () => {
    const p = makePreview()
    p.showCamera.value = false

    p.onVideoFeedError()
    await vi.advanceTimersByTimeAsync(5000)
    expect(p.feedStatus.value).toBe('live') // 未进入 reconnecting
  })

  it('onVideoFeedLoad 帧到达后复位 live 与重试计数', () => {
    const p = makePreview()
    p.showCamera.value = true
    p.onVideoFeedError() // → reconnecting
    expect(p.feedStatus.value).toBe('reconnecting')

    p.onVideoFeedLoad()
    expect(p.feedStatus.value).toBe('live')
    expect(p.feedRetryCount.value).toBe(0)

    // 已是 live 时 load 不重复置位逻辑（幂等）
    p.onVideoFeedLoad()
    expect(p.feedStatus.value).toBe('live')
  })

  it('openCamera 默认选第一路 enabled 摄像头并开始状态轮询', async () => {
    fetch.mockResolvedValue(okJson({ camera_opened: true }))
    const p = makePreview([{ id: 'a', enabled: false }, { id: 'b', enabled: true }])

    await p.openCamera()
    expect(p.loadCameras).toHaveBeenCalledTimes(1)
    expect(p.activeCameraId.value).toBe('b')
    expect(p.videoFeedUrl.value).toContain('/api/cameras/b/video_feed')
    expect(p.showCamera.value).toBe(true)

    const callsAfterOpen = fetch.mock.calls.length
    await vi.advanceTimersByTimeAsync(2000) // 轮询周期到
    expect(fetch.mock.calls.length).toBeGreaterThan(callsAfterOpen)
  })

  it('closeCamera 停止轮询并清掉挂起的重连定时器', async () => {
    fetch.mockResolvedValue(okJson({}))
    const p = makePreview([])
    await p.openCamera()

    p.closeCamera()
    expect(p.showCamera.value).toBe(false)

    const callsNow = fetch.mock.calls.length
    await vi.advanceTimersByTimeAsync(10000)
    expect(fetch.mock.calls.length).toBe(callsNow) // 无新轮询请求
  })

  it('switchCamera 旧路 disable、新路 enable，并换 feed URL', async () => {
    fetch.mockResolvedValue(okJson({}))
    const p = makePreview()
    p.activeCameraId.value = 'old'
    p.showCamera.value = true
    p.refreshVideoFeed()

    await p.switchCamera('new')
    const posts = fetch.mock.calls.map(c => c[0])
    expect(posts).toContain('/api/cameras/old/display/disable')
    expect(posts).toContain('/api/cameras/new/display/enable')
    expect(p.videoFeedUrl.value).toContain('/api/cameras/new/video_feed')
  })

  it('switchCamera 同路不动作', async () => {
    const p = makePreview()
    p.activeCameraId.value = 'same'
    await p.switchCamera('same')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('fetchCameraState 读 per-camera state 并接入设备状态机', async () => {
    fetch.mockResolvedValueOnce(okJson({ camera_opened: false }))
    const p = makePreview()
    p.activeCameraId.value = 'cam9'
    p.showCamera.value = true
    p.refreshVideoFeed()

    await p.fetchCameraState()
    expect(fetch.mock.calls[0][0]).toBe('/api/cameras/cam9/state')
    expect(p.cameraState.value).toEqual({ camera_opened: false })
    // 设备掉了 → 设备源的重连中（不动 img src）
    expect(p.feedStatus.value).toBe('reconnecting')
    expect(p.feedStatusSource.value).toBe('device')
  })

  it('设备恢复 false→true 时强制重建 src；流终态但设备在线则自愈', async () => {
    const p = makePreview()
    p.activeCameraId.value = 'c'
    p.showCamera.value = true
    p.refreshVideoFeed() // key=1

    // 第一次：设备在线（false→true 不成立，prev 初始 null）
    fetch.mockResolvedValueOnce(okJson({ camera_opened: true }))
    await p.fetchCameraState()

    // 流掉到终态 + 设备在线 → 自动 refreshVideoFeed 自愈（key 变 2）
    p.feedStatus.value = 'disconnected'
    fetch.mockResolvedValueOnce(okJson({ camera_opened: true }))
    await p.fetchCameraState()
    expect(p.videoFeedUrl.value).toContain('_t=2')
    expect(p.feedStatus.value).toBe('live')

    // 设备先掉再恢复 → 强制刷新 src（key 变 3）
    p.refreshVideoFeed() // key=3
    fetch.mockResolvedValueOnce(okJson({ camera_opened: false }))
    await p.fetchCameraState() // prev=true → opened=false 掉线态
    p.feedStatus.value = 'reconnecting'
    fetch.mockResolvedValueOnce(okJson({ camera_opened: true }))
    await p.fetchCameraState() // false→true 翻转
    expect(p.videoFeedUrl.value).toContain('_t=4')
  })
})
