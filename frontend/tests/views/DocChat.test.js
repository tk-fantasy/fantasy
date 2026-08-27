import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// navigator.mediaDevices 桩必须先于组件 import 安装（useVoiceInput 加载期固化）
vi.hoisted(() => {
  Object.defineProperty(global.navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn() },
    configurable: true,
  })
})

const { MockWebSocket } = vi.hoisted(() => {
  class MockWebSocket {
    static instances = []
    static OPEN = 1
    constructor(url) {
      this.url = url
      this.readyState = 0 // CONNECTING
      this.sent = []
      this.closedWith = null
      MockWebSocket.instances.push(this)
    }
    open() { this.readyState = 1; this.onopen?.({}) }
    send(data) { this.sent.push(data) }
    close(code = 1000) {
      if (this.readyState === 3) return // 与浏览器一致：重复 close 不再触发 onclose
      this.readyState = 3
      this.closedWith = code
      this.onclose?.({ code })
    }
    receive(obj) { this.onmessage?.({ data: JSON.stringify(obj) }) }
  }
  return { MockWebSocket }
})
global.WebSocket = MockWebSocket

global.fetch = vi.fn(() =>
  Promise.resolve({ ok: false, status: 401, json: () => Promise.resolve({}) })
)

const DocChat = (await import('../../src/views/DocChat.vue')).default

describe('DocChat', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    MockWebSocket.instances.length = 0
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('挂载即连接 /ws/doc/chat，页面骨架就绪', async () => {
    const wrapper = mount(DocChat)
    await flushPromises()

    expect(MockWebSocket.instances.length).toBe(1)
    expect(MockWebSocket.instances[0].url).toContain('/ws/doc/chat')
    expect(wrapper.find('.page-title-text').text()).toBe('Aether使用助手')
    expect(wrapper.find('.empty-state').exists()).toBe(true)
  })

  it('ws open 后收到 ping 回 pong；token 流式渲染并 done 收尾', async () => {
    const wrapper = mount(DocChat)
    await flushPromises()
    const ws = MockWebSocket.instances[0]

    ws.open()
    ws.receive({ type: 'ping' })
    await flushPromises()
    expect(ws.sent).toEqual([JSON.stringify({ type: 'pong' })])

    ws.receive({ type: 'token', content: '第' })
    ws.receive({ type: 'token', content: '一章' })
    ws.receive({ type: 'done' })
    await flushPromises()

    const bubble = wrapper.find('.assistant-message .message-text')
    expect(bubble.exists()).toBe(true)
    expect(bubble.text()).toContain('第一章')
    // done 后流式指示符消失
    expect(wrapper.find('.streaming-indicator').exists()).toBe(false)
  })

  it('发送消息：经 ws 发 query 并把用户气泡插进列表', async () => {
    const wrapper = mount(DocChat)
    await flushPromises()
    const ws = MockWebSocket.instances[0]
    ws.open()

    await wrapper.find('.chat-input, textarea').setValue('怎么配网关？')
    const sendBtn = wrapper.findAll('button').find(b => b.text().match(/发送|↳/))
    if (sendBtn) {
      await sendBtn.trigger('click')
    } else {
      await wrapper.find('textarea, input').trigger('keydown.enter')
    }
    await flushPromises()

    expect(ws.sent).toContain(JSON.stringify({ query: '怎么配网关？' }))
    expect(wrapper.find('.user-message .message-content').text()).toBe('怎么配网关？')
  })

  it('error 消息以系统提示插入并结束流式态', async () => {
    const wrapper = mount(DocChat)
    await flushPromises()
    const ws = MockWebSocket.instances[0]
    ws.open()
    ws.receive({ type: 'token', content: '部分回答' })
    ws.receive({ type: 'error', message: '模型超时' })
    await flushPromises()

    const sysMsgs = wrapper.findAll('.system-message')
    expect(sysMsgs.length).toBe(1)
    expect(sysMsgs[0].text()).toContain('模型超时')
  })

  it('未连接时发送：本地插入"未连接"提示而不发请求', async () => {
    const wrapper = mount(DocChat)
    await flushPromises()
    const ws = MockWebSocket.instances[0] // 保持 CONNECTING

    await wrapper.find('.chat-input, textarea').setValue('你好')
    const sendBtn = wrapper.findAll('button').find(b => b.text().match(/发送|↳/))
    if (sendBtn) {
      await sendBtn.trigger('click')
    } else {
      await wrapper.find('textarea, input').trigger('keydown.enter')
    }
    await flushPromises()

    expect(ws.sent).not.toContain(JSON.stringify({ query: '你好' }))
    expect(wrapper.text()).toContain('WebSocket 未连接')
  })

  it('异常断开（非 1008）3 秒后自动重连出第二个 WS 实例', async () => {
    mount(DocChat)
    await flushPromises()
    const first = MockWebSocket.instances[0]
    first.open()

    first.close(1006) // 网络异常（refresh 走 401 → session-expired 分支不触发重连计时）
    // close(1006) 不等于 1008 → 落入 setTimeout(connectWS, 3000)
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()

    expect(MockWebSocket.instances.length).toBe(2)
  })

  it('卸载时清掉待触发的重连定时器', async () => {
    const wrapper = mount(DocChat)
    await flushPromises()
    const ws = MockWebSocket.instances[0]
    ws.open()

    // 断开 → 排定了 3 秒后的重连
    ws.close(1006)

    // 卸载发生在重连触发前
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10000)

    expect(MockWebSocket.instances.length).toBe(1) // 定时器被清理，不再新建连接
  })
})
