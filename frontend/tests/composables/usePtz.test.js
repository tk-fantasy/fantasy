import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ref } from 'vue'
import { usePtz } from '../../src/composables/usePtz'

global.fetch = vi.fn()

function makePtz(camerasList = [], activeId = null) {
  const activeCameraId = ref(activeId)
  const cameras = ref(camerasList)
  const api = usePtz(activeCameraId, cameras)
  return { ...api, activeCameraId, cameras }
}

describe('usePtz', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('fetchPtzStatus 读到该路摄像头的 PTZ 配置', async () => {
    const p = makePtz(
      [{ id: 'cam1', ptz_enabled: true, ptz_step_ms: 450 }],
      'cam1'
    )

    await p.fetchPtzStatus()
    expect(p.ptzEnabled.value).toBe(true)
    expect(p.ptzStepMs.value).toBe(450)
  })

  it('无选中或找不到配置行时 PTZ 不启用', async () => {
    const p1 = makePtz([{ id: 'cam1', ptz_enabled: true }], null)
    await p1.fetchPtzStatus()
    expect(p1.ptzEnabled.value).toBe(false)

    const p2 = makePtz([], 'ghost')
    await p2.fetchPtzStatus()
    expect(p2.ptzEnabled.value).toBe(false)
  })

  it('ptz_step_ms 缺省回退 300', async () => {
    const p = makePtz([{ id: 'c', ptz_enabled: true }], 'c')
    await p.fetchPtzStatus()
    expect(p.ptzStepMs.value).toBe(300)
  })

  it('未启用时不发步进请求', () => {
    const p = makePtz([{ id: 'c', ptz_enabled: false }], 'c')
    p.fetchPtzStatus()
    p.ptzStep('left')
    expect(fetch).not.toHaveBeenCalled()
  })

  it('启用后单击发一次 POST；冷却期内连点被忽略', async () => {
    fetch.mockResolvedValue({})
    const p = makePtz([{ id: 'c', ptz_enabled: true }], 'c')
    await p.fetchPtzStatus()

    p.ptzStep('left')
    expect(fetch).toHaveBeenCalledTimes(1)
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/api/cameras/c/ptz/step')
    expect(opts.method).toBe('POST')
    expect(JSON.parse(opts.body)).toEqual({ direction: 'left' })
    expect(p.ptzMoving.value).toBe(true) // 冷却中

    p.ptzStep('right') // 冷却期内的第二次点击
    expect(fetch).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(300) // 冷却结束（按摄像头 step_ms）
    expect(p.ptzMoving.value).toBe(false)
    p.ptzStep('right') // 恢复后可再发
    expect(fetch).toHaveBeenCalledTimes(2)
  })

  it('网络异常不吞事件循环：catch 后冷却照常释放', async () => {
    fetch.mockRejectedValue(new Error('offline'))
    const p = makePtz([{ id: 'c', ptz_enabled: true, ptz_step_ms: 100 }], 'c')
    await p.fetchPtzStatus()

    p.ptzStep('up')
    await vi.advanceTimersByTimeAsync(100)
    expect(p.ptzMoving.value).toBe(false)
  })
})
