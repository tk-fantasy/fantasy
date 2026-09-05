import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// EventCharts 内部依赖 echarts，jsdom 无 canvas 会抛错 → 打桩（组件本身的
// 行为由 EventCharts.test.js 覆盖，这里只验证页面接线）
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => ({
    setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(), on: vi.fn(),
  })),
  graphic: { LinearGradient: class {} },
}))
vi.mock('echarts/charts', () => ({ BarChart: {}, PieChart: {} }))
vi.mock('echarts/components', () => ({ GridComponent: {}, TooltipComponent: {}, LegendComponent: {} }))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

// IntersectionObserver 桩：observe 即上报可见
class IOStub {
  constructor(cb) { this.cb = cb }
  observe(el) { this.cb([{ isIntersecting: true, target: el }]) }
  disconnect() {}
  unobserve() {}
}

import FamilyReportView from '../../src/views/FamilyReportView.vue'

const STATS = {
  totals: { device_op: 10, automation: 4, task_success: 3, task_failed: 1, alert: 2, alert_resolved: 1 },
  daily: [{ day: '08-31', device_op: 10, automation: 4, task: 4, alert: 3 }],
  top_devices: [{ entity: 'light.a', name: '卧室灯', count: 6, ai: 4, manual: 2 }],
  actor: { ai: 5, manual: 5 },
}

function mockFetch({ stats = STATS, events = [] } = {}) {
  global.fetch = vi.fn((url) => {
    const u = String(url)
    if (u.startsWith('/api/events/stats')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: stats }) })
    }
    if (u.startsWith('/api/events')) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: events }) })
    }
    if (u === '/api/report/weekly') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: null }) })
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
  })
}

describe('FamilyReportView — 统计区', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('IntersectionObserver', IOStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders metric cards from stats totals', async () => {
    mockFetch()
    const wrapper = mount(FamilyReportView)
    await flushPromises()
    const cards = wrapper.findAll('.metric-card')
    expect(cards.length).toBe(4)
    const values = cards.map(c => c.find('.metric-value').text())
    expect(values).toEqual(['10', '4', '75%', '3'])
    // 任务成功率卡带成败明细
    expect(cards[2].find('.metric-detail').text()).toContain('成功 3 / 失败 1')
  })

  it('renders charts region and requests stats with days param', async () => {
    mockFetch()
    const wrapper = mount(FamilyReportView)
    await flushPromises()
    expect(wrapper.findComponent({ name: 'EventCharts' }).exists()).toBe(true)
    expect(global.fetch).toHaveBeenCalledWith('/api/events/stats?days=7', { credentials: 'include' })
  })

  it('shows em dash for task rate when no tasks ran', async () => {
    mockFetch({ stats: { totals: {}, daily: [], top_devices: [], actor: { ai: 0, manual: 0 } } })
    const wrapper = mount(FamilyReportView)
    await flushPromises()
    const cards = wrapper.findAll('.metric-card')
    expect(cards[2].find('.metric-value').text()).toBe('—')
    expect(cards[2].find('.metric-detail').text()).toContain('暂无执行')
  })

  it('overview shows weekly report and has no full timeline section', async () => {
    mockFetch({
      events: [{ id: 1, kind: 'automation', message: 'x', created_at: 0 }],
      report: null,
    })
    const wrapper = mount(FamilyReportView)
    await flushPromises()
    // 总览：周报在、全量时间线不在（时间线只通过下钻某天进入）
    expect(wrapper.find('.report-card').exists()).toBe(true)
    expect(wrapper.find('.events-card').exists()).toBe(false)
  })

  it('drill-down shows day timeline only (no report, no metric cards)', async () => {
    mockFetch()
    const wrapper = mount(FamilyReportView)
    await flushPromises()
    await wrapper.findComponent({ name: 'EventCharts' }).vm.$emit('drill', '08-31')
    await flushPromises()
    expect(wrapper.find('.day-card').exists()).toBe(true)
    // 下钻页不展示周报与数字卡
    expect(wrapper.find('.report-card').exists()).toBe(false)
    expect(wrapper.find('.metrics-grid').exists()).toBe(false)
    expect(wrapper.findComponent({ name: 'EventCharts' }).exists()).toBe(false)
  })

  it('drills into a day from chart click and fetches that date', async () => {
    const eventsByCall = []
    mockFetch({ events: [] })
    global.fetch.mockImplementation((url) => {
      const u = String(url)
      if (u.startsWith('/api/events/stats')) {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: STATS }) })
      }
      if (u.startsWith('/api/events')) {
        eventsByCall.push(u)
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
    })
    const wrapper = mount(FamilyReportView)
    await flushPromises()
    expect(wrapper.find('.day-card').exists()).toBe(false)

    // 模拟图表柱点击下钻（stats.daily 的 MM-DD → 当年完整日期）
    await wrapper.findComponent({ name: 'EventCharts' }).vm.$emit('drill', '08-31')
    await flushPromises()
    expect(wrapper.find('.day-card').exists()).toBe(true)
    expect(wrapper.find('.crumb h2').text()).toBeTruthy()
    // 下钻请求带 date= 参数（当年完整日期）
    const drillCall = eventsByCall.find(u => u.includes('date='))
    expect(drillCall).toContain(`date=${new Date().getFullYear()}-08-31`)
    // 返回总览
    await wrapper.find('.crumb-link').trigger('click')
    await flushPromises()
    expect(wrapper.find('.day-card').exists()).toBe(false)
  })
})
