import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ReviseChatModal from '../../src/components/ReviseChatModal.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

const TASK = {
  id: 't1',
  name: '起床提醒',
  schedule: { kind: 'cron', expr: '0 8 * * *' },
  payload: { kind: 'reminder', intent: '提醒起床', original: '每天8点提醒起床' },
}

async function mountTask(initial = TASK) {
  const wrapper = mount(ReviseChatModal, {
    props: { kind: 'task', itemId: 't1', initial },
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

describe('ReviseChatModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    console.error = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('plan 模式：展示任务类建议问题列表', async () => {
    const wrapper = await mountTask()
    expect(wrapper.text()).toContain('这个任务是执行一次还是每天重复？')
  })

  it('点建议问题即提问：POST explain 端点并展示回答', async () => {
    fetch.mockImplementation(url =>
      String(url).endsWith('/explain') ? okData({ answer: '这个任务每天早上8点执行一次。' }) : okData({})
    )
    const wrapper = await mountTask()

    const q = wrapper.findAll('button').find(b => b.text().includes('每天重复'))
    await q.trigger('click')
    // assistant 气泡稍后由 sendInstruction 异步更新
    await flushPromises()
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/scheduled-tasks/t1/explain')).toBe(true)
    expect(wrapper.text()).toContain('每天早上8点执行一次')
  })

  it('modify 模式：发送修改指令调 revise，成功后「应用」按钮可用', async () => {
    fetch.mockImplementation(url => {
      if (String(url).endsWith('/revise')) {
        return okData({
          task: { ...TASK, schedule: { kind: 'cron', expr: '0 9 * * *' } },
          summary: '改为9点',
        })
      }
      return okData({})
    })
    const wrapper = await mountTask()

    await wrapper.findAll('.mode-btn')[1].trigger('click') // 切到 modify

    // 组件手写比较 e.key === 'Enter'（大小写敏感），测试直接点发送按钮更稳
    await wrapper.find('.revise-input').setValue('改成9点')
    await wrapper.find('.btn-send').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/scheduled-tasks/t1/revise')).toBe(true)
    expect(wrapper.text()).toContain('改为9点')

    // 应用修改 → PUT 落库 + emit applied
    const applyBtn = wrapper.findAll('button').find(b => b.text().includes('应用修改'))
    expect(applyBtn).toBeTruthy()
    fetch.mockClear()
    fetch.mockImplementation(() => okData({ ...TASK, schedule: { kind: 'cron', expr: '0 9 * * *' } }))
    await applyBtn.trigger('click')
    await flushPromises()

    expect(fetch.mock.calls[0][0]).toBe('/api/scheduled-tasks/t1')
    expect(wrapper.emitted('applied')).toBeTruthy()
  })

  it('explain 失败时把错误写进助手气泡而非崩溃', async () => {
    fetch.mockRejectedValue(new Error('LLM 超时'))
    const wrapper = await mountTask()

    const q = wrapper.findAll('button').find(b => b.text().includes('每天重复'))
    await q.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toMatch(/失败|超时|错误/)
  })
})

// ---------------------------------------------------------------------------
// pending 模式 — 聊天里刚解析出的规则草稿，确认直接落库（没有 rule_id）
// ---------------------------------------------------------------------------

// 视觉规则 + 未绑定摄像头 —— 正是聊天路径产出的、必须让用户显式选一路的状态
const DRAFT_RULE = {
  name: '有人开研发部灯',
  condition: '画面里有人',
  type: 'vision',
  camera_id: '',
  actions: [{
    mcp_tool_name: 'ha_devices___call_service',
    mcp_tool_input: { domain: 'light', service: 'turn_on', entity_id: 'light.rd' },
  }],
  action_descriptions: ['打开研发部灯'],
}

const WEATHER_DRAFT = { ...DRAFT_RULE, type: 'weather', condition: '下雨', name: '下雨关窗' }
const BOUND_DRAFT = { ...DRAFT_RULE, camera_id: 'cam_2' }

const CAMERAS = [
  { id: 'cam_1', name: '研发部', enabled: true },
  { id: 'cam_2', name: '门口', enabled: true },
  { id: 'cam_3', name: '已停用', enabled: false },
]

function okJson(data) {
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data }) })
}

function errJson(status, message) {
  return Promise.resolve({ ok: false, status, json: () => Promise.resolve({ message }) })
}

async function mountPending(props = {}) {
  const wrapper = mount(ReviseChatModal, {
    props: {
      kind: 'rule',
      initial: DRAFT_RULE,
      pendingId: 'pd1',
      sessionId: 's1',
      cameras: CAMERAS,
      ...props,
    },
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

function findBtn(wrapper, text) {
  return wrapper.findAll('button').find(b => b.text().includes(text))
}

/** 点摄像头 chip（按显示名找）。 */
async function pickCamera(wrapper, name) {
  const chip = wrapper.findAll('.camera-chip').find(c => c.text() === name)
  expect(chip, `找不到摄像头选项「${name}」`).toBeTruthy()
  await chip.trigger('click')
  await flushPromises()
}

function confirmBtn(wrapper) {
  return findBtn(wrapper, '确认创建')
}

/** 切到 modify 模式并发一条指令。 */
async function sendModify(wrapper, text) {
  await wrapper.findAll('.mode-btn')[1].trigger('click')
  await wrapper.find('.revise-input').setValue(text)
  await wrapper.find('.btn-send').trigger('click')
  await flushPromises()
  await flushPromises()
}

describe('ReviseChatModal pending 模式', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    console.error = vi.fn()
    console.warn = vi.fn()
    fetch.mockImplementation(() => okJson({}))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('标题改成确认创建，并提示还没落库 + 草稿有效期', async () => {
    const wrapper = await mountPending()

    expect(wrapper.find('h2').text()).toBe('确认创建规则')
    expect(wrapper.find('.pending-notice').text()).toContain('还没有创建')
    expect(wrapper.find('.pending-notice').text()).toContain('10 分钟')
  })

  it('不传 itemId 也能挂载（草稿没有 rule_id）', async () => {
    const wrapper = await mountPending()

    // Vue 对缺失的 required prop 走 console.warn，不是 error
    expect(console.warn).not.toHaveBeenCalled()
    expect(wrapper.find('.revise-container').exists()).toBe(true)
  })

  it('底部是确认创建/取消创建，且不显示「应用修改」（会 PUT 到空 rule_id）', async () => {
    const wrapper = await mountPending()

    expect(findBtn(wrapper, '确认创建')).toBeTruthy()
    expect(findBtn(wrapper, '取消创建')).toBeTruthy()
    expect(findBtn(wrapper, '应用修改')).toBeUndefined()

    await sendModify(wrapper, '改成两个人')
    expect(findBtn(wrapper, '应用修改')).toBeUndefined()
    expect(findBtn(wrapper, '确认创建')).toBeTruthy()
  })

  it('非 pending 模式保持原样：仍是规则详情 + 应用修改', async () => {
    const wrapper = await mountTaskPlain()

    expect(wrapper.find('h2').text()).toBe('任务详情')
    expect(findBtn(wrapper, '确认创建')).toBeUndefined()
    expect(wrapper.find('.pending-notice').exists()).toBe(false)
  })

  it('plan 提问走 pending explain 端点，body 带 session_id 不带 current', async () => {
    fetch.mockImplementation(() => okJson({ answer: '画面里有人就开灯' }))
    const wrapper = await mountPending()

    const q = findBtn(wrapper, '什么时候会触发')
    await q.trigger('click')
    await flushPromises()
    await flushPromises()

    const call = fetch.mock.calls.find(c => String(c[0]).includes('/rules/pending/'))
    expect(call[0]).toBe('/api/rules/pending/pd1/explain')
    const body = JSON.parse(call[1].body)
    expect(body).toEqual({ session_id: 's1', question: '这条规则什么时候会触发？' })
    expect(wrapper.text()).toContain('画面里有人就开灯')
  })

  it('modify 走 pending revise 端点，并把改动同步进摘要', async () => {
    fetch.mockImplementation(url => String(url).endsWith('/revise')
      ? okJson({ rule: { ...DRAFT_RULE, condition: '画面里有两个人' }, summary: '改成两个人' })
      : okJson({}))
    const wrapper = await mountPending()

    await sendModify(wrapper, '要两个人才开')

    const call = fetch.mock.calls.find(c => String(c[0]).includes('/rules/pending/'))
    expect(call[0]).toBe('/api/rules/pending/pd1/revise')
    expect(JSON.parse(call[1].body)).toEqual({ session_id: 's1', instruction: '要两个人才开' })
    expect(wrapper.text()).toContain('画面里有两个人')
  })

  it('点确认创建 → POST confirm → emit confirmed', async () => {
    fetch.mockImplementation(() => okJson({
      rule_id: 'rule-1', name: '有人开研发部灯', summary: '有人就打开研发部灯',
    }))
    const wrapper = await mountPending()
    await pickCamera(wrapper, '研发部')

    await confirmBtn(wrapper).trigger('click')
    await flushPromises()

    expect(fetch.mock.calls[0][0]).toBe('/api/rules/pending/pd1/confirm')
    expect(JSON.parse(fetch.mock.calls[0][1].body))
      .toEqual({ session_id: 's1', camera_id: 'cam_1' })
    expect(wrapper.emitted('confirmed')[0][0]).toMatchObject({ rule_id: 'rule-1' })
  })

  it('确认按钮不依赖 hasRevision：plan 模式下选完摄像头就能直接点', async () => {
    fetch.mockImplementation(() => okJson({ rule_id: 'rule-1', name: 'x' }))
    const wrapper = await mountPending()

    await pickCamera(wrapper, '门口')
    const btn = confirmBtn(wrapper)
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('confirmed')).toBeTruthy()
  })

  it('点取消创建 → POST cancel → emit cancelled（带规则名）', async () => {
    fetch.mockImplementation(() => okJson({ cancelled: true, name: '有人开研发部灯' }))
    const wrapper = await mountPending()

    await findBtn(wrapper, '取消创建').trigger('click')
    await flushPromises()

    expect(fetch.mock.calls[0][0]).toBe('/api/rules/pending/pd1/cancel')
    expect(wrapper.emitted('cancelled')[0][0]).toBe('有人开研发部灯')
  })

  it('草稿过期（404）→ emit expired 让上层提示重说需求', async () => {
    fetch.mockImplementation(() => errJson(404, '待确认规则不存在或已过期，请重新描述需求'))
    const wrapper = await mountPending()
    await pickCamera(wrapper, '研发部')

    await confirmBtn(wrapper).trigger('click')
    await flushPromises()

    expect(wrapper.emitted('expired')[0][0]).toContain('已过期')
    expect(wrapper.emitted('confirmed')).toBeUndefined()
  })

  it('非 404 失败（如设备已不存在）留在弹窗内展示，用户还能改', async () => {
    fetch.mockImplementation(() => errJson(400, '规则动作引用的设备已不存在: light.rd'))
    const wrapper = await mountPending()
    await pickCamera(wrapper, '研发部')

    await confirmBtn(wrapper).trigger('click')
    await flushPromises()

    expect(wrapper.emitted('expired')).toBeUndefined()
    expect(wrapper.emitted('confirmed')).toBeUndefined()
    expect(wrapper.find('.action-error').text()).toContain('light.rd')
    expect(alert).not.toHaveBeenCalled()
  })

  it('X 关闭只 emit close，不发任何请求（草稿保留给口头确认）', async () => {
    const wrapper = await mountPending()
    fetch.mockClear()

    await wrapper.find('.btn-close').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('close')).toBeTruthy()
    expect(wrapper.emitted('cancelled')).toBeUndefined()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('遮罩点击同样只 emit close', async () => {
    const wrapper = await mountPending()
    fetch.mockClear()

    await wrapper.find('.revise-overlay').trigger('click')
    await flushPromises()

    expect(wrapper.emitted('close')).toBeTruthy()
    expect(fetch).not.toHaveBeenCalled()
  })
})

/** 非 pending 模式的对照挂载（任务弹窗原行为不能被动到）。 */
async function mountTaskPlain() {
  const wrapper = mount(ReviseChatModal, {
    props: { kind: 'task', itemId: 't1', initial: TASK },
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

// ---------------------------------------------------------------------------
// 摄像头绑定选择器 — 视觉规则必须显式选一路（或显式选全局）
// ---------------------------------------------------------------------------

describe('ReviseChatModal 摄像头绑定', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    console.error = vi.fn()
    console.warn = vi.fn()
    fetch.mockImplementation(() => okJson({}))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('未绑定的视觉规则：显示选择器 + 识别标签，确认按钮被禁用', async () => {
    const wrapper = await mountPending()

    expect(wrapper.find('.camera-picker').exists()).toBe(true)
    expect(wrapper.find('.vision-tag').text()).toContain('视觉规则')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.find('.camera-hint.required').text()).toContain('必须选一路')
  })

  it('选项 = 启用的摄像头 + 全部摄像头，停用的那路不出现', async () => {
    const wrapper = await mountPending()

    const labels = wrapper.findAll('.camera-chip').map(c => c.text())
    expect(labels).toEqual(['全部摄像头（全局）', '研发部', '门口'])
  })

  it('没选摄像头 → 确认按钮 disabled，点了也不发请求', async () => {
    const wrapper = await mountPending()
    fetch.mockClear()

    const btn = confirmBtn(wrapper)
    expect(btn.attributes('disabled')).toBeDefined()
    await btn.trigger('click')
    await flushPromises()

    expect(fetch).not.toHaveBeenCalled()
    expect(wrapper.emitted('confirmed')).toBeUndefined()
    // 为什么点不动，界面上要说清楚
    expect(wrapper.find('.camera-hint.required').text()).toContain('必须选一路')
    expect(wrapper.find('.action-row .hint').text()).toContain('还需选择')
  })

  it('选具体一路 → 确认 body 带该 camera_id，警示消失', async () => {
    fetch.mockImplementation(() => okJson({ rule_id: 'rule-1', name: 'x' }))
    const wrapper = await mountPending()

    await pickCamera(wrapper, '门口')
    expect(wrapper.find('.camera-hint').text()).toContain('只有这一路')
    await confirmBtn(wrapper).trigger('click')
    await flushPromises()

    expect(JSON.parse(fetch.mock.calls[0][1].body).camera_id).toBe('cam_2')
  })

  it('选「全部摄像头」→ 出红字警告，确认后 camera_id 为空串（显式全局）', async () => {
    fetch.mockImplementation(() => okJson({ rule_id: 'rule-1', name: 'x' }))
    const wrapper = await mountPending()

    await pickCamera(wrapper, '全部摄像头（全局）')
    expect(wrapper.find('.camera-hint.danger').text()).toContain('每一路')
    // 显式全局是合法选择，必须放开确认按钮
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()

    await confirmBtn(wrapper).trigger('click')
    await flushPromises()
    const body = JSON.parse(fetch.mock.calls[0][1].body)
    expect(body.camera_id).toBe('')
    expect('camera_id' in body).toBe(true)
  })

  it('草稿已绑定摄像头 → 预选那一路，可直接确认', async () => {
    fetch.mockImplementation(() => okJson({ rule_id: 'rule-1', name: 'x' }))
    const wrapper = await mountPending({ initial: BOUND_DRAFT })

    const active = wrapper.findAll('.camera-chip').filter(c => c.classes('active'))
    expect(active.map(c => c.text())).toEqual(['门口'])
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()

    await confirmBtn(wrapper).trigger('click')
    await flushPromises()
    expect(JSON.parse(fetch.mock.calls[0][1].body).camera_id).toBe('cam_2')
  })

  it('非视觉规则：不显示选择器，确认不带 camera_id', async () => {
    fetch.mockImplementation(() => okJson({ rule_id: 'rule-1', name: 'x' }))
    const wrapper = await mountPending({ initial: WEATHER_DRAFT })

    expect(wrapper.find('.camera-picker').exists()).toBe(false)
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()

    await confirmBtn(wrapper).trigger('click')
    await flushPromises()
    const body = JSON.parse(fetch.mock.calls[0][1].body)
    expect(body).toEqual({ session_id: 's1' })
  })

  it('type 缺失时按视觉处理（与后端 is_vision_rule 同口径）', async () => {
    const { type: _type, ...noType } = DRAFT_RULE
    const wrapper = await mountPending({ initial: noType })

    expect(wrapper.find('.camera-picker').exists()).toBe(true)
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('revise 把规则改成天气后选择器消失（从 pendingJson 现算，不等 prop）', async () => {
    fetch.mockImplementation(url => String(url).endsWith('/revise')
      ? okJson({ rule: { ...WEATHER_DRAFT }, summary: '改成下雨触发' })
      : okJson({ rule_id: 'rule-1', name: 'x' }))
    const wrapper = await mountPending()
    expect(wrapper.find('.camera-picker').exists()).toBe(true)

    await sendModify(wrapper, '改成下雨天触发')

    expect(wrapper.find('.camera-picker').exists()).toBe(false)
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('没有可用摄像头时只剩「全部摄像头」一项', async () => {
    const wrapper = await mountPending({ cameras: [] })

    expect(wrapper.findAll('.camera-chip').map(c => c.text()))
      .toEqual(['全部摄像头（全局）'])
  })

  it('非 pending 模式（已落库规则详情）不显示选择器', async () => {
    const wrapper = await mountTaskPlain()

    expect(wrapper.find('.camera-picker').exists()).toBe(false)
  })
})

// ---------------------------------------------------------------------------

describe('ReviseChatModal 自动匹配横幅', () => {
  it('initial 带 auto_corrections 时渲染替换明细，提示用户核对', async () => {
    const wrapper = await mountPending({
      initial: {
        ...DRAFT_RULE,
        auto_corrections: [{
          action_index: 0,
          from: 'cover.front_door',
          to: 'light.rd',
          to_name: '研发部灯',
          query: '打开大门',
        }],
      },
    })

    const banner = wrapper.find('.auto-fix-banner')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('cover.front_door')
    expect(banner.text()).toContain('研发部灯')
    expect(banner.text()).toContain('核对')
  })

  it('无 auto_corrections 时不渲染横幅', async () => {
    const wrapper = await mountPending()
    expect(wrapper.find('.auto-fix-banner').exists()).toBe(false)
  })

  it('revise 带回新规则的 auto_corrections 时横幅随之刷新', async () => {
    const wrapper = await mountPending()
    expect(wrapper.find('.auto-fix-banner').exists()).toBe(false)

    global.fetch = vi.fn().mockImplementation((url) => {
      if (String(url).includes('/revise')) {
        return okJson({
          rule: {
            ...DRAFT_RULE,
            auto_corrections: [{ action_index: 0, from: 'light.rd', to: 'light.kt', to_name: '客厅灯' }],
          },
          summary: '换成客厅灯',
        })
      }
      return okJson({})
    })

    await sendModify(wrapper, '换成客厅灯')
    await flushPromises()

    const banner = wrapper.find('.auto-fix-banner')
    expect(banner.exists()).toBe(true)
    expect(banner.text()).toContain('客厅灯')
  })
})
