import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TaskView from '../../src/views/TaskView.vue'
import CameraBindModal from '../../src/components/CameraBindModal.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: [
        { id: '1', name: '人来开灯', condition: '检测到人', enabled: true, actions: [] },
        { id: '2', name: '人走关灯', condition: '无人', enabled: false, actions: [] }
      ]
    })
  })
)

describe('TaskView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders task page', () => {
    const wrapper = mount(TaskView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(TaskView)
    expect(wrapper.find('.page-header h1').text()).toBe('自动化规则')
  })

  it('loads rules on mount', async () => {
    mount(TaskView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/rules', { credentials: 'include' })
  })

  it('renders create form toggle', () => {
    const wrapper = mount(TaskView)
    expect(wrapper.text()).toContain('新建规则')
  })
})

// TaskView onMounted 拉三类数据，全走 fetch：
// /api/rules（规则列表）、emoji prefs、摄像头列表（useCamera）
function mockFetch(rules = []) {
  global.fetch = vi.fn((url) => {
    if (url === '/api/rules') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: rules }) })
    }
    // useCamera / emoji prefs 等其余请求兜底空数据
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
  })
}

describe('TaskView 规则错配徽标', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('全局视觉规则：卡片标红 + 红徽标', async () => {
    mockFetch([
      { id: 'r1', name: '有人比耶关研发部灯', type: 'vision', camera_id: '', enabled: true, condition: '检测到有人比个耶', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    const card = wrapper.find('.rule-card')
    expect(card.classes()).toContain('rule-card--red')
    const badge = wrapper.find('.rule-mismatch-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.classes()).toContain('rule-mismatch-badge--red')
    expect(badge.text()).toBe('⚠️ 视觉规则未绑定摄像头')
  })

  it('绑定摄像头的定时规则：卡片标橙 + 橙徽标', async () => {
    mockFetch([
      { id: 'r2', name: '早八点开灯', type: 'time', camera_id: 'cam1', enabled: true, condition: '每天 8 点', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    // cam1 规则按 camera_id 过滤，仅在对应摄像头视图显示（默认"全局"视图不显示）；
    // 切换顶部摄像头过滤器到 cam1（计划疏漏修正：不改断言，仅补视图切换）
    wrapper.vm.selectedCameraId = 'cam1'
    await flushPromises()
    const card = wrapper.find('.rule-card')
    expect(card.classes()).toContain('rule-card--orange')
    const badge = wrapper.find('.rule-mismatch-badge')
    expect(badge.classes()).toContain('rule-mismatch-badge--orange')
    expect(badge.text()).toBe('💡 定时/天气规则不依赖摄像头')
  })

  it('正常视觉规则（已绑定摄像头）：无徽标无配色', async () => {
    mockFetch([
      { id: 'r3', name: '比耶关灯', type: 'vision', camera_id: 'cam1', enabled: true, condition: '检测到有人比个耶', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    // 同上：切到 cam1 摄像头视图后该规则卡片才渲染
    wrapper.vm.selectedCameraId = 'cam1'
    await flushPromises()
    expect(wrapper.find('.rule-card').classes()).not.toContain('rule-card--red')
    expect(wrapper.find('.rule-card').classes()).not.toContain('rule-card--orange')
    expect(wrapper.find('.rule-mismatch-badge').exists()).toBe(false)
  })

  it('正常定时规则（全局）：无徽标无配色', async () => {
    mockFetch([
      { id: 'r4', name: '日落开客厅灯', type: 'time', camera_id: '', enabled: true, condition: '日落时', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    expect(wrapper.find('.rule-mismatch-badge').exists()).toBe(false)
    expect(wrapper.find('.rule-card').classes()).not.toContain('rule-card--orange')
  })
})

// ---------------------------------------------------------------------------
// 两段式创建：preview 只解析 → 视觉规则弹摄像头选择框 → 选完才落库
//
// header 的「当前作用范围」是打字**之前**定的，而规则是不是视觉类型要等 LLM 解析
// 完才知道。不拦这一道，在「全局」范围下输入「有人就开灯」就会落库成
// ruleMismatch 标红的危险规则（automation_service 对未绑定规则每一路都评估）。
// ---------------------------------------------------------------------------

const CAMERAS = [
  { id: 'cam_1', name: '研发部', enabled: true },
  { id: 'cam_2', name: '门口', enabled: true },
]

const VISION_PREVIEW = {
  rule: {
    name: '有人开研发部灯', condition: '画面里有人', type: 'vision', camera_id: '',
    actions: [{ mcp_tool_input: { entity_id: 'light.rd' } }],
    action_descriptions: ['打开研发部灯'], summary: '有人就打开研发部灯',
  },
  needs_camera: true,
}

const TIME_PREVIEW = {
  rule: {
    name: '日落开灯', condition: '日落时', type: 'time', camera_id: '',
    actions: [], action_descriptions: [], summary: '日落时开灯',
  },
  needs_camera: false,
}

/** 按 URL+method 路由的 fetch 桩，记录 POST body 供断言。 */
function mockCreateFlow(preview, rules = []) {
  const posts = []
  global.fetch = vi.fn((url, opts = {}) => {
    const method = (opts.method || 'GET').toUpperCase()
    if (method === 'POST' && String(url).endsWith('/api/rules/preview')) {
      posts.push({ url: String(url), body: JSON.parse(opts.body) })
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: preview }) })
    }
    if (method === 'POST' && String(url).endsWith('/api/rules')) {
      posts.push({ url: String(url), body: JSON.parse(opts.body) })
      const saved = { id: 'r-new', enabled: true, ...JSON.parse(opts.body) }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: saved }) })
    }
    if (String(url) === '/api/cameras') {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: CAMERAS }) })
    }
    if (String(url) === '/api/rules') {
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: rules }) })
    }
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ data: [] }) })
  })
  return posts
}

async function openCreateFormAndType(wrapper, text) {
  await wrapper.find('.btn-add').trigger('click')
  await flushPromises()
  await wrapper.find('.create-input').setValue(text)
  await wrapper.find('.btn-create').trigger('click')
  await flushPromises()
  await flushPromises()
}

describe('TaskView 两段式创建', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    console.error = vi.fn()
    console.warn = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('视觉规则：preview 后弹选择框，且此时还没落库', async () => {
    const posts = mockCreateFlow(VISION_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()

    await openCreateFormAndType(wrapper, '有人就打开研发部灯')

    expect(wrapper.findComponent(CameraBindModal).exists()).toBe(true)
    expect(posts.map(p => p.url)).toEqual(['/api/rules/preview'])
    // preview 刻意不带 camera_id，否则 needs_camera 永远为假、拦不住
    expect(posts[0].body).toEqual({ text: '有人就打开研发部灯' })
  })

  it('选择框确认 → POST /api/rules 带上选中的 camera_id', async () => {
    const posts = mockCreateFlow(VISION_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()
    await openCreateFormAndType(wrapper, '有人就打开研发部灯')

    wrapper.findComponent(CameraBindModal).vm.$emit('confirm', 'cam_2')
    await flushPromises()

    const save = posts.find(p => p.url.endsWith('/api/rules'))
    expect(save.body.camera_id).toBe('cam_2')
    expect(save.body.name).toBe('有人开研发部灯')
    expect(save.body.type).toBe('vision')
    expect(wrapper.findComponent(CameraBindModal).exists()).toBe(false)
  })

  it('选择框可以显式选全局（camera_id 空串）', async () => {
    const posts = mockCreateFlow(VISION_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()
    await openCreateFormAndType(wrapper, '有人就开灯')

    wrapper.findComponent(CameraBindModal).vm.$emit('confirm', '')
    await flushPromises()

    const save = posts.find(p => p.url.endsWith('/api/rules'))
    expect(save.body.camera_id).toBe('')
  })

  it('选择框取消 → 不落库，输入框内容留着让用户改措辞', async () => {
    const posts = mockCreateFlow(VISION_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()
    await openCreateFormAndType(wrapper, '有人就打开研发部灯')

    wrapper.findComponent(CameraBindModal).vm.$emit('close')
    await flushPromises()

    expect(posts.map(p => p.url)).toEqual(['/api/rules/preview'])
    expect(wrapper.findComponent(CameraBindModal).exists()).toBe(false)
    expect(wrapper.find('.create-input').element.value).toBe('有人就打开研发部灯')
  })

  it('非视觉规则：不弹框，直接落库并沿用 header 的作用范围', async () => {
    const posts = mockCreateFlow(TIME_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()
    await openCreateFormAndType(wrapper, '日落时打开客厅灯')

    expect(wrapper.findComponent(CameraBindModal).exists()).toBe(false)
    const save = posts.find(p => p.url.endsWith('/api/rules'))
    expect(save).toBeTruthy()
    // header 默认「全局(定时/天气)」→ camera_id 空串，保持原有按房间归类的用法
    expect(save.body.camera_id).toBe('')
  })

  it('header 作用范围指着某路摄像头时，非视觉规则沿用该绑定', async () => {
    const posts = mockCreateFlow(TIME_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()
    wrapper.vm.selectedCameraId = 'cam_1'
    await flushPromises()

    await openCreateFormAndType(wrapper, '日落时打开客厅灯')

    expect(posts.find(p => p.url.endsWith('/api/rules')).body.camera_id).toBe('cam_1')
  })

  it('作用范围是全局但解析出视觉规则 → 选择框收到 scopeCameraId="" 以便警告', async () => {
    mockCreateFlow(VISION_PREVIEW)
    const wrapper = mount(TaskView)
    await flushPromises()
    await openCreateFormAndType(wrapper, '有人就开灯')

    expect(wrapper.findComponent(CameraBindModal).props('scopeCameraId')).toBe('')
    expect(wrapper.findComponent(CameraBindModal).props('cameras')).toEqual(CAMERAS)
  })

  it('preview 失败时 alert 出后端 message，不弹选择框', async () => {
    global.fetch = vi.fn((url, opts = {}) => {
      if (String(url).endsWith('/api/rules/preview')) {
        return Promise.resolve({
          ok: false, status: 400,
          json: () => Promise.resolve({ message: '无法从输入中解析出有效的视觉条件' }),
        })
      }
      if (String(url) === '/api/cameras') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: CAMERAS }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
    })
    const wrapper = mount(TaskView)
    await flushPromises()

    await openCreateFormAndType(wrapper, '乱写')

    expect(alert).toHaveBeenCalledWith(expect.stringContaining('无法从输入中解析出'))
    expect(wrapper.findComponent(CameraBindModal).exists()).toBe(false)
  })
})
