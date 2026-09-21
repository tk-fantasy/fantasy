import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { defineComponent, h, ref, KeepAlive, nextTick } from 'vue'
import ChatView from '../../src/views/ChatView.vue'

// 切页不打断主对话（App.vue 的 <keep-alive include="ChatView">）回归测试：
// 离开 /chat 时组件只是停用 —— WS 不关闭、不重连，回到 /chat 对话现场原样保留。

class MockWebSocket {
  static OPEN = 1
  static instances = []
  constructor(url) {
    this.url = url
    this.readyState = 1
    this.onopen = null
    this.onclose = null
    this.onerror = null
    this.onmessage = null
    this.closed = false
    MockWebSocket.instances.push(this)
    setTimeout(() => { if (this.onopen) this.onopen() }, 0)
  }
  send() {}
  close() { this.closed = true }
}
global.WebSocket = MockWebSocket

global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ data: { id: 'test-session' } })
  })
)

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() }),
  useRoute: () => ({ query: {} })
}))

vi.mock('../../src/composables/useAuth', () => ({
  useAuth: () => ({
    token: { value: 'test-jwt-token' },
    user: { value: { username: 'testuser' } }
  })
}))

// 模拟 App.vue：<keep-alive include="ChatView"> 内切换 ChatView ↔ 其他页面
const DummyView = defineComponent({
  name: 'DummyView',
  render() { return h('div', { class: 'dummy-view' }, 'other page') }
})

const Host = defineComponent({
  name: 'KeepAliveHost',
  setup() {
    const showChat = ref(true)
    return { showChat }
  },
  render() {
    return h(KeepAlive, { include: ['ChatView'] }, {
      default: () => h(this.showChat ? ChatView : DummyView)
    })
  }
})

describe('ChatView KeepAlive（切页不断开主对话）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    MockWebSocket.instances = []
  })

  it('切走再切回：WS 不关闭不重连，消息现场保留', async () => {
    const wrapper = mount(Host)
    await flushPromises()
    expect(wrapper.find('.chat-view').exists()).toBe(true)
    expect(MockWebSocket.instances).toHaveLength(1)

    // 构造对话现场：发一条用户消息
    await wrapper.find('.chat-input').setValue('在吗')
    await wrapper.find('.send-btn').trigger('click')
    expect(wrapper.text()).toContain('在吗')

    // 切到别的页面（离开 /chat）
    wrapper.vm.showChat = false
    await nextTick()
    await flushPromises()
    expect(wrapper.find('.dummy-view').exists()).toBe(true)
    // 连接仍在（未被 close），也没有偷偷新建第二条
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(MockWebSocket.instances[0].closed).toBe(false)

    // 切回 /chat：复用原连接，消息不被清空
    wrapper.vm.showChat = true
    await nextTick()
    await flushPromises()
    expect(wrapper.find('.chat-view').exists()).toBe(true)
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(wrapper.text()).toContain('在吗')
  })
})
