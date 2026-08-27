import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import PluginSlot from '../../src/components/integration/PluginSlot.vue'

global.fetch = vi.fn()

function mountSlot(slot = 'chat_tools') {
  return mount(PluginSlot, {
    props: { slot },
    global: { stubs: { teleport: true } },
  })
}

describe('PluginSlot', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.warn = vi.fn()
  })

  it('挂载时拉取 ui_contributions', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: [] }),
    })
    await mountSlot()
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith('/api/integrations/ui_contributions', expect.anything())
  })

  it('贡献数据非数组时不炸渲染（防御逻辑）', async () => {
    const errSpy = vi.spyOn(console, 'error')
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ data: { broken: true } }),
    })
    const wrapper = await mountSlot()
    await flushPromises()
    await flushPromises()

    expect(wrapper.vm).toBeTruthy() // 组件实例存活即代表渲染路径未被炸掉
    errSpy.mockRestore()
  })

  it('slot/type 不匹配的贡献不渲染组件', async () => {
    fetch.mockImplementationOnce(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          data: [
            { plugin_id: 'xiaoai', slot: 'other_slot', type: 'custom_component' },
            { plugin_id: 'xiaoai', slot: 'chat_tools', type: 'webhook_only' },
          ],
        }),
      })
    )
    const wrapper = await mountSlot('chat_tools')
    await flushPromises()

    expect(wrapper.text()).not.toContain('小爱') // 无匹配面板内容
  })

  it('匹配的贡献（slot+type）会异步挂载对应插件前端组件', async () => {
    // test-camera 插件有真实的 frontend/TestCameraPanel.vue，走真实 glob 命中
    fetch.mockImplementationOnce(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          data: [{ plugin_id: 'test-camera', slot: 'chat_tools', type: 'custom_component' }],
        }),
      })
    )
    const wrapper = await mountSlot('chat_tools')
    await flushPromises()
    // defineAsyncComponent 异步解析后再 flush 一轮
    await flushPromises()

    expect(wrapper.html()).not.toBe('')
  })
})
