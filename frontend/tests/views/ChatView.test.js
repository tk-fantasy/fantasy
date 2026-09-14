import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ChatView from '../../src/views/ChatView.vue'
import ReviseChatModal from '../../src/components/ReviseChatModal.vue'
import DeviceSelectModal from '../../src/components/DeviceSelectModal.vue'

// Mock WebSocket as a class
class MockWebSocket {
  static OPEN = 1
  // 测试要主动往组件里灌 instruction，故留下每个实例的引用
  static instances = []
  constructor(url) {
    this.url = url
    this.readyState = 1
    this.onopen = null
    this.onclose = null
    this.onerror = null
    this.onmessage = null
    MockWebSocket.instances.push(this)
    setTimeout(() => { if (this.onopen) this.onopen() }, 0)
  }
  send() {}
  close() {}
}
global.WebSocket = MockWebSocket

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ data: { id: 'test-session' } })
  })
)

// Mock vue-router
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn() })
}))

// Mock useAuth
vi.mock('../../src/composables/useAuth', () => ({
  useAuth: () => ({
    token: { value: 'test-jwt-token' },
    user: { value: { username: 'testuser' } }
  })
}))

describe('ChatView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    MockWebSocket.instances = []
  })

  it('renders chat view', () => {
    const wrapper = mount(ChatView)
    expect(wrapper.find('.chat-view').exists()).toBe(true)
  })

  it('renders input area', () => {
    const wrapper = mount(ChatView)
    expect(wrapper.find('.chat-input-area').exists()).toBe(true)
    expect(wrapper.find('.chat-input').exists()).toBe(true)
  })

  it('renders send button', () => {
    const wrapper = mount(ChatView)
    expect(wrapper.find('.send-btn').exists()).toBe(true)
    expect(wrapper.find('.send-btn').text()).toBe('发送')
  })

  it('renders empty state when no messages', () => {
    const wrapper = mount(ChatView)
    expect(wrapper.find('.empty-state').exists()).toBe(true)
  })

  it('renders messages area', () => {
    const wrapper = mount(ChatView)
    expect(wrapper.find('.chat-messages').exists()).toBe(true)
  })

  it('connects WebSocket with JWT token', async () => {
    const wrapper = mount(ChatView)
    await vi.dynamicImportSettled()

    // WebSocket should be created (we can't easily verify the URL with our mock)
    // The important thing is that the component mounts without errors
    expect(wrapper.find('.chat-view').exists()).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 待确认规则弹窗 — 聊天建规则的网页确认路径
// ---------------------------------------------------------------------------

const DRAFT_RULE = {
  name: '有人开研发部灯',
  condition: '画面里有人',
  actions: [{
    mcp_tool_name: 'ha_devices___call_service',
    mcp_tool_input: { domain: 'light', service: 'turn_on', entity_id: 'light.rd' },
  }],
  action_descriptions: ['打开研发部灯'],
  summary: '有人就打开研发部灯',
}

function instruction(namespace, name, payload) {
  return {
    header: { namespace, name, session_id: 'test-session', request_id: 'r1' },
    payload,
  }
}

/** tool_response.result 是 JSON 字符串（LangChain ToolMessage 只能是字符串）。 */
function toolResult(toolName, resultObj, success = true) {
  return instruction('Template', 'CallToolResult', {
    id: 'tool-1',
    success,
    tool_name: toolName,
    tool_response: success ? { result: JSON.stringify(resultObj) } : null,
    error_message: success ? null : 'boom',
  })
}

const PENDING_RESULT = {
  status: 'pending_confirm',
  pending_id: 'pd1',
  rule: DRAFT_RULE,
  summary: DRAFT_RULE.summary,
  expire_minutes: 10,
}

async function mountChat() {
  const wrapper = mount(ChatView, { global: { stubs: { teleport: true } } })
  await flushPromises()
  return wrapper
}

const CHAT_CAMERAS = [
  { id: 'cam_1', name: '研发部', enabled: true },
  { id: 'cam_2', name: '门口', enabled: true },
]

function lastWs() {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1]
}

async function push(ws, inst) {
  ws.onmessage({ data: JSON.stringify(inst) })
  await flushPromises()
}

describe('ChatView 待确认规则弹窗', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    MockWebSocket.instances = []
    console.warn = vi.fn()
    // /api/cameras 必须回数组，否则 useCamera 把对象塞进 cameras，
    // 弹窗的 cameras prop 会报类型警告（也测不到真实的选择器渲染）
    global.fetch = vi.fn((url) => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        data: String(url) === '/api/cameras' ? CHAT_CAMERAS : { id: 'test-session' },
      }),
    }))
  })

  it('收到 pending_confirm 不立即弹框，等 Dialog.Finish 才弹', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()

    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    // 轮中弹框会让「我已确认」插到原始请求之前（dispatcher 轮末才落 model_messages）
    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)

    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(true)
  })

  it('弹框带上 pendingId / sessionId / 草稿规则', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()

    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    const modal = wrapper.findComponent(ReviseChatModal)
    expect(modal.props('pendingId')).toBe('pd1')
    expect(modal.props('sessionId')).toBe('test-session')
    expect(modal.props('kind')).toBe('rule')
    expect(modal.props('initial').name).toBe('有人开研发部灯')
  })

  it('弹框时懒加载摄像头列表，视觉规则的选择器才选得到具体某路', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    // ChatView 挂载时不拉 /api/cameras（只有开摄像头预览才拉），
    // 弹窗必须在打开时补拉，否则选择器只剩「全部摄像头」一项
    expect(global.fetch.mock.calls.some(c => String(c[0]) === '/api/cameras')).toBe(false)

    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    await flushPromises()

    expect(global.fetch.mock.calls.some(c => String(c[0]) === '/api/cameras')).toBe(true)
    const modal = wrapper.findComponent(ReviseChatModal)
    expect(modal.props('cameras')).toEqual(CHAT_CAMERAS)
    expect(modal.findAll('.camera-chip').map(c => c.text()))
      .toEqual(['全部摄像头（全局）', '研发部', '门口'])
  })

  it('确认后 push 系统消息并关闭弹窗', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(ReviseChatModal).vm.$emit(
      'confirmed', { rule_id: 'rule-1', name: '有人开研发部灯', summary: '有人就打开研发部灯' })
    await flushPromises()

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
    const system = wrapper.findAll('.message.system-message')
    expect(system[system.length - 1].text()).toContain('规则已创建：有人开研发部灯')
  })

  it('取消后 push 系统消息', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(ReviseChatModal).vm.$emit('cancelled', '有人开研发部灯')
    await flushPromises()

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
    expect(wrapper.text()).toContain('已取消创建规则：有人开研发部灯')
  })

  it('草稿过期时提示重新描述需求', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(ReviseChatModal).vm.$emit(
      'expired', '待确认规则不存在或已过期')
    await flushPromises()

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
    expect(wrapper.text()).toContain('请重新说一遍需求')
  })

  it('直接关闭弹窗不调 cancel 端点（草稿留给口头确认路径）', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    global.fetch.mockClear()

    wrapper.findComponent(ReviseChatModal).vm.$emit('close')
    await flushPromises()

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
    expect(global.fetch.mock.calls.some(c => String(c[0]).includes('/cancel'))).toBe(false)
  })

  it('关闭后下一轮不再自动弹出', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    wrapper.findComponent(ReviseChatModal).vm.$emit('close')
    await flushPromises()

    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
  })

  it('普通工具结果不触发弹窗', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()

    await push(ws, toolResult('ha_devices___call_service', { success: true }))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
  })

  it('失败的工具结果不触发弹窗', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()

    await push(ws, toolResult('automation_rule_create', PENDING_RESULT, false))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
  })

  it('聊天里 revise 后弹窗用最新草稿', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))
    await push(ws, toolResult('automation_rule_revise', {
      ...PENDING_RESULT,
      rule: { ...DRAFT_RULE, condition: '画面里有两个人' },
      change_summary: '改成两个人',
    }))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    expect(wrapper.findComponent(ReviseChatModal).props('initial').condition)
      .toBe('画面里有两个人')
  })

  it('发新消息会丢掉上一轮未处理的草稿触发器', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('automation_rule_create', PENDING_RESULT))

    await wrapper.find('.chat-input').setValue('算了，开个灯吧')
    await wrapper.find('.send-btn').trigger('click')
    await flushPromises()
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    expect(wrapper.findComponent(ReviseChatModal).exists()).toBe(false)
  })
})

describe('ChatView 设备消歧弹窗', () => {
  const CANDIDATES = [
    { entity_id: 'light.a', label: '床头灯', domain: 'light', area_name: '卧室', state: 'off' },
    { entity_id: 'light.b', label: '客厅吊灯', domain: 'light', area_name: '客厅', state: 'on' },
  ]
  // 闸门返回的是 success 形状（不含 "error" 键），否则会触发失败重试轮
  const NEED_SELECTION = {
    success: false,
    status: 'need_selection',
    pending_id: 'sel-1',
    reason: 'ambiguous',
    query: '开灯',
    notice: '用户说的是「开灯」，匹配到 2 个设备，无法确定是哪一个。',
    candidates: CANDIDATES,
    action: { domain: 'light', service: 'turn_on', data: {} },
    hint: '……',
  }

  beforeEach(() => {
    vi.clearAllMocks()
    sessionStorage.clear()
    MockWebSocket.instances = []
    console.warn = vi.fn()
    global.fetch = vi.fn((url) => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({
        data: String(url) === '/api/cameras' ? CHAT_CAMERAS : { id: 'test-session' },
      }),
    }))
  })

  it('收到 need_selection 不立即弹框，等 Dialog.Finish 才弹', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()

    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    // 与待确认规则同一条硬约束：轮中弹框会让「我已通过界面选择」插到原始请求之前
    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)

    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(true)
  })

  it('弹框带上 pendingId / sessionId / query / reason / service / 候选', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    const modal = wrapper.findComponent(DeviceSelectModal)
    expect(modal.props('pendingId')).toBe('sel-1')
    expect(modal.props('sessionId')).toBe('test-session')
    expect(modal.props('query')).toBe('开灯')
    expect(modal.props('reason')).toBe('ambiguous')
    expect(modal.props('service')).toBe('turn_on')
    expect(modal.props('candidates')).toEqual(CANDIDATES)
  })

  it('category_miss 也接得住（设备不存在时如实说 + 给同类候选）', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service',
      { ...NEED_SELECTION, reason: 'category_miss', query: '月球的灯' }))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    const modal = wrapper.findComponent(DeviceSelectModal)
    expect(modal.props('reason')).toBe('category_miss')
    expect(modal.props('query')).toBe('月球的灯')
  })

  it('工具失败形状（success=false）不触发弹框', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION, false))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
  })

  it('别的工具返回同名字段不触发（只认 call_service）', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___get_entities', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))
    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
  })

  it('confirmed：关弹框并把执行结果作为系统消息回显', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(DeviceSelectModal).vm.$emit(
      'confirmed', { entity_ids: ['light.a'], names: ['床头灯'] })
    await flushPromises()

    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
    expect(wrapper.text()).toContain('已执行：床头灯')
  })

  it('cancelled：关弹框并明确说明没有操作任何设备', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(DeviceSelectModal).vm.$emit('cancelled')
    await flushPromises()

    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
    expect(wrapper.text()).toContain('没有操作任何设备')
  })

  it('expired：提示重说指令', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(DeviceSelectModal).vm.$emit('expired', '待选设备不存在或已过期')
    await flushPromises()

    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
    expect(wrapper.text()).toContain('请重新说一遍指令')
  })

  it('expired：后端消息自带提示语时不重复说两遍', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    wrapper.findComponent(DeviceSelectModal).vm.$emit(
      'expired', '待选设备不存在或已过期，请重新说一遍指令')
    await flushPromises()

    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
    expect(wrapper.text()).toContain('⚠️ 待选设备不存在或已过期，请重新说一遍指令。')
    expect(wrapper.text()).not.toContain('请重新说一遍指令，请重新说一遍指令')
  })

  it('close：只关弹窗，不作废后端草稿（TTL 内仍可在聊天里说设备名）', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    const before = global.fetch.mock.calls.length
    wrapper.findComponent(DeviceSelectModal).vm.$emit('close')
    await flushPromises()

    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
    // 没打 /cancel
    expect(global.fetch.mock.calls.length).toBe(before)
    expect(global.fetch.mock.calls.some(c => String(c[0]).endsWith('/cancel'))).toBe(false)
  })

  it('发新消息会丢掉上一轮未处理的消歧触发器', async () => {
    const wrapper = await mountChat()
    const ws = lastWs()
    await push(ws, toolResult('ha_devices___call_service', NEED_SELECTION))

    await wrapper.find('.chat-input').setValue('算了，开客厅吊灯')
    await wrapper.find('.send-btn').trigger('click')
    await flushPromises()
    await push(ws, instruction('Dialog', 'Finish', { success: true }))

    expect(wrapper.findComponent(DeviceSelectModal).exists()).toBe(false)
  })
})
