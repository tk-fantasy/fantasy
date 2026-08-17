import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TaskView from '../../src/views/TaskView.vue'

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
