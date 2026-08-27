import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useLlmStatus, ROLE_LABELS } from '../../src/composables/useLlmStatus'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

describe('useLlmStatus', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.error = vi.fn()
  })

  it('ROLE_LABELS 覆盖状态条展示的 4 个角色', () => {
    expect(ROLE_LABELS).toEqual({ chat: '对话', summary: '摘要', vision: '视觉', embed: '向量' })
  })

  it('onStatusHover 懒加载：首次请求，缓存后不再请求', async () => {
    fetch.mockReturnValueOnce(okData({ roles: { chat: { ok: true } } }))
    const s = useLlmStatus()

    await s.onStatusHover()
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(s.llmStatus.value.roles.chat.ok).toBe(true)
    expect(s.llmStatusLoading.value).toBe(false)

    await s.onStatusHover() // 命中缓存
    await s.onStatusHover()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it('进行中的 hover 不叠加并发请求', async () => {
    let resolveFirst
    fetch.mockReturnValueOnce(new Promise(r => { resolveFirst = r }))
    const s = useLlmStatus()

    const p1 = s.onStatusHover() // 发起中：llmStatusLoading=true
    const p2 = s.onStatusHover() // 应直接跳过
    resolveFirst({ ok: true, json: () => Promise.resolve({ data: { roles: {} } }) })
    await Promise.all([p1, p2])
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(s.llmStatusLoading.value).toBe(false)
  })

  it('首次 hover 失败不置缓存，下次可重试成功', async () => {
    const broken = useLlmStatus()
    fetch.mockRejectedValueOnce(new Error('offline'))
    await broken.onStatusHover()
    expect(console.error).toHaveBeenCalled()
    expect(broken.llmStatus.value).toBeNull()

    fetch.mockReturnValueOnce(okData({ roles: { chat: {} } }))
    await broken.onStatusHover() // 未置 loaded → 允许重试
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(broken.llmStatus.value.roles.chat).toBeDefined()
  })

  it('loadChatModelName：按 key_id 匹配 llm_keys 取模型名', async () => {
    fetch.mockImplementation(url =>
      url === '/api/llm/settings'
        ? okData({ current: { chat: { key_id: 'k2' } } })
        : okData([{ id: 'k1', model: 'glm-a' }, { id: 'k2', model: 'glm-4-flash' }])
    )
    const s = useLlmStatus()
    await s.loadChatModelName()
    expect(s.chatModelName.value).toBe('glm-4-flash')
  })

  it('非全局聊天配置但没绑 key 时模型名为空字符串', async () => {
    fetch.mockReturnValueOnce(okData({ current: { chat: {} } }))
    const s = useLlmStatus()
    await s.loadChatModelName()
    expect(s.chatModelName.value).toBe('')
  })

  it('走全局共享时不覆盖模型名（可能从别处已设置）', async () => {
    fetch.mockReturnValueOnce(okData({ current: { chat: { use_global: true } } }))
    const s = useLlmStatus()
    s.chatModelName.value = 'global-model'
    await s.loadChatModelName()
    expect(s.chatModelName.value).toBe('global-model')
  })

  it('settings 接口失败只打日志不抛出', async () => {
    fetch.mockRejectedValueOnce(new Error('offline'))
    const s = useLlmStatus()
    await s.loadChatModelName()
    expect(console.error).toHaveBeenCalled()
    expect(s.chatModelName.value).toBe('')
  })
})
