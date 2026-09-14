import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useCamera } from '../../src/composables/useCamera'

global.fetch = vi.fn()

// useCamera 走 utils/api 的 apiGet/apiPost/apiPut（底层都是 global.fetch），
// DELETE / 规则创建走原生 fetch —— 统一在 fetch 层按 URL 分流。
function okJson(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

describe('useCamera', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('loadCameras 拉列表并复位 loading', async () => {
    const c = useCamera()
    fetch.mockResolvedValueOnce(okJson([{ id: 'a' }, { id: 'b' }]))

    await c.loadCameras()
    expect(c.cameras.value).toEqual([{ id: 'a' }, { id: 'b' }])
    expect(c.loading.value).toBe(false)
  })

  it('loadCameras 出错时 loading 也必须复位（finally）', async () => {
    const c = useCamera()
    fetch.mockRejectedValueOnce(new Error('boom'))

    await expect(c.loadCameras()).rejects.toThrow('boom')
    expect(c.loading.value).toBe(false)
  })

  it('createCamera 提交后刷新列表并返回新建结果', async () => {
    const c = useCamera()
    fetch
      .mockResolvedValueOnce(okJson({ id: 'new1' })) // POST
      .mockResolvedValueOnce(okJson([{ id: 'new1' }])) // 随后的 loadCameras

    const created = await c.createCamera({ name: '前门' })
    expect(created).toEqual({ id: 'new1' })
    expect(fetch.mock.calls[0][0]).toBe('/api/cameras')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ name: '前门' })
    expect(c.cameras.value).toEqual([{ id: 'new1' }])
  })

  it('updateCamera PUT 到对应 id 并刷新', async () => {
    const c = useCamera()
    fetch
      .mockResolvedValueOnce(okJson({ id: 'a', enabled: false }))
      .mockResolvedValueOnce(okJson([]))

    await c.updateCamera('a', { enabled: false })
    expect(fetch.mock.calls[0][0]).toBe('/api/cameras/a')
    expect(fetch.mock.calls[0][1].method).toBe('PUT')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ enabled: false })
  })

  it('deleteCamera 成功后刷新；失败抛带状态码的错误', async () => {
    const c = useCamera()

    fetch.mockResolvedValueOnce({ ok: true }).mockResolvedValueOnce(okJson([]))
    await c.deleteCamera('a')
    expect(fetch.mock.calls[0]).toEqual([
      '/api/cameras/a',
      { method: 'DELETE', credentials: 'include' },
    ])

    fetch.mockResolvedValueOnce({ ok: false, status: 409 })
    await expect(c.deleteCamera('a')).rejects.toThrow('HTTP 409')
  })

  it('deleteFocus 失败时抛错（成功路径不抛）', async () => {
    const c = useCamera()

    fetch.mockResolvedValueOnce({ ok: true })
    await c.deleteFocus('cam1', 'f1')

    fetch.mockResolvedValueOnce({ ok: false, status: 500 })
    await expect(c.deleteFocus('cam1', 'f2')).rejects.toThrow('删除关注项失败')
  })

  it('enableDisplay / disableDisplay POST 单例开关端点', async () => {
    const c = useCamera()
    fetch.mockResolvedValue(okJson({}))

    await c.enableDisplay('cam1')
    expect(fetch.mock.calls[0][0]).toBe('/api/cameras/cam1/display/enable')

    await c.disableDisplay('cam2')
    expect(fetch.mock.calls[1][0]).toBe('/api/cameras/cam2/display/disable')
  })

})
