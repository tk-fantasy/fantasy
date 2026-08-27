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

  it('loadRules 按 camera_id 过滤；空 camera_id 匹配全局规则', async () => {
    const c = useCamera()
    const rules = [
      { id: 'r1', camera_id: 'cam1' },
      { id: 'r2', camera_id: '' },
      { id: 'r3' },
    ]
    // 每次调用各自排队一个响应，避免持久 mock 泄漏到第二次调用
    fetch.mockResolvedValueOnce(okJson(rules)).mockResolvedValueOnce(okJson(rules))

    const mine = await c.loadRules('cam1')
    expect(mine.map(r => r.id)).toEqual(['r1'])

    const all = await c.loadRules('')
    expect(all.map(r => r.id)).toEqual(['r2', 'r3'])
  })

  it('createRule 成功返回 data；失败抛后端 message', async () => {
    const c = useCamera()

    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: { id: 'r9' } }),
    })
    expect(await c.createRule('', '有人开灯')).toEqual({ id: 'r9' })
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/api/task/rule')
    expect(JSON.parse(opts.body)).toEqual({ text: '有人开灯', camera_id: '' })

    fetch.mockResolvedValueOnce({
      ok: false,
      status: 400,
      json: () => Promise.resolve({ message: '指令无法解析' }),
    })
    await expect(c.createRule('cam1', '乱写')).rejects.toThrow('指令无法解析')
  })

  it('toggleRule / deleteRule 发对应的 method 与 body', async () => {
    const c = useCamera()
    fetch.mockResolvedValue({ ok: true })

    await c.toggleRule('r1', false)
    const [u1, o1] = fetch.mock.calls[0]
    expect(u1).toBe('/api/rules/r1/enabled')
    expect(o1.method).toBe('POST')
    expect(JSON.parse(o1.body)).toEqual({ enabled: false })

    await c.deleteRule('r1')
    const [u2, o2] = fetch.mock.calls[1]
    expect(u2).toBe('/api/rules/r1')
    expect(o2.method).toBe('DELETE')
  })
})
