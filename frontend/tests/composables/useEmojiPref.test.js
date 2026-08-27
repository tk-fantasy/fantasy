import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useEmojiPref } from '../../src/composables/useEmojiPref'

global.fetch = vi.fn()

describe('useEmojiPref', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.error = vi.fn()
  })

  it('loadEmojiPrefs 把 API 数组转成 scope:key 字典', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({
        data: [
          { scope: 'weather', key: '100', emoji_char: '☀️' },
          { scope: 'entity', key: 'light.kt', emoji_char: '💡' },
        ],
      }),
    })
    const p = useEmojiPref()
    await p.loadEmojiPrefs()
    expect(p.emojiPrefs.value).toEqual({
      'weather:100': '☀️',
      'entity:light.kt': '💡',
    })
  })

  it('data 非数组时兜底空字典；请求失败不抛出', async () => {
    fetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: null }) })
    const p1 = useEmojiPref()
    await p1.loadEmojiPrefs()
    expect(p1.emojiPrefs.value).toEqual({})

    fetch.mockRejectedValueOnce(new Error('offline'))
    await useEmojiPref().loadEmojiPrefs() // 不应抛
  })

  it('openEmojiPicker 记录目标并打开选择器', () => {
    const p = useEmojiPref()
    p.openEmojiPicker('weather', '100')
    expect(p.currentEmojiTarget.value).toEqual({ scope: 'weather', key: '100' })
    expect(p.showEmojiPicker.value).toBe(true)
  })

  it('onEmojiSelect PUT 到后端并回写本地字典', async () => {
    fetch.mockResolvedValueOnce({ ok: true })
    const p = useEmojiPref()
    p.openEmojiPicker('weather', '100')

    await p.onEmojiSelect({ char: '🌧️' })
    const [url, opts] = fetch.mock.calls[0]
    expect(url).toBe('/api/emoji/preferences')
    expect(opts.method).toBe('PUT')
    expect(JSON.parse(opts.body)).toEqual({
      scope: 'weather', key: '100', emoji_char: '🌧️',
    })
    expect(p.emojiPrefs.value['weather:100']).toBe('🌧️')
  })

  it('未 open 过就 select 时静默忽略', async () => {
    await useEmojiPref().onEmojiSelect({ char: 'x' })
    expect(fetch).not.toHaveBeenCalled()
  })

  it('getEmoji：已配置返回偏好，未配置回退默认值', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: [{ scope: 'w', key: 'k', emoji_char: '⭐' }] }),
    })
    const p = useEmojiPref()
    await p.loadEmojiPrefs()

    expect(p.getEmoji('w', 'k')).toBe('⭐')
    expect(p.getEmoji('w', 'other', ' fallback').trim()).toBe('fallback')
  })

  it('PUT 网络失败只打日志，本地字典不动', async () => {
    fetch.mockRejectedValueOnce(new Error('offline'))
    const p = useEmojiPref()
    p.openEmojiPicker('a', 'b')

    await p.onEmojiSelect({ char: '😀' })
    expect(p.emojiPrefs.value['a:b']).toBeUndefined()
  })
})
