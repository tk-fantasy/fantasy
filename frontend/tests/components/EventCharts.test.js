import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// echarts 桩：jsdom 无 canvas，init 会抛错。记录 setOption 调用供断言，
// 并保留 on() 让点击监听可注册、可模拟触发。
const chartInstances = []
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => {
    const handlers = {}
    const inst = {
      setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn(),
      on: vi.fn((event, cb) => { handlers[event] = cb }),
      __trigger: (event, params) => handlers[event]?.(params),
    }
    chartInstances.push(inst)
    return inst
  }),
  graphic: { LinearGradient: class {} },
}))
vi.mock('echarts/charts', () => ({ BarChart: {}, PieChart: {} }))
vi.mock('echarts/components', () => ({
  GridComponent: {},
  TooltipComponent: {},
  LegendComponent: {},
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

// IntersectionObserver 桩：observe 即上报可见（组件立即懒启动）
class IOStub {
  constructor(cb) { this.cb = cb }
  observe(el) { this.cb([{ isIntersecting: true, target: el }]) }
  disconnect() {}
  unobserve() {}
}

import EventCharts from '../../src/components/EventCharts.vue'

const STATS = {
  totals: { device_op: 10, automation: 4, task_success: 3, task_failed: 1, alert: 1 },
  daily: [
    { day: '08-30', device_op: 3, automation: 1, task: 1, alert: 0 },
    { day: '08-31', device_op: 7, automation: 3, task: 3, alert: 1 },
  ],
  top_devices: [
    { entity: 'light.a', name: '卧室灯', count: 6, ai: 4, manual: 2 },
    { entity: 'switch.b', name: '插座', count: 4, ai: 1, manual: 3 },
  ],
  actor: { ai: 5, manual: 5 },
}

describe('EventCharts', () => {
  beforeEach(() => {
    chartInstances.length = 0
    vi.stubGlobal('IntersectionObserver', IOStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders trend and top charts with data', async () => {
    const wrapper = mount(EventCharts, { props: { stats: STATS } })
    await flushPromises()
    // 两张图各 init 一个实例，且都收到 option
    expect(chartInstances.length).toBe(2)
    expect(wrapper.find('.chart-canvas').exists()).toBe(true)
    expect(wrapper.find('.charts-empty').exists()).toBe(false)
    // actor 占比条：AI 5/10 = 50%
    expect(wrapper.find('.actor-fill').attributes('style')).toContain('50%')
    expect(wrapper.find('.actor-legend').text()).toContain('AI 5 次（50%）')
  })

  it('shows empty state when no chartable data', async () => {
    const empty = {
      totals: { weekly_report: 1 },
      daily: [{ day: '08-31', device_op: 0, automation: 0, task: 0, alert: 0 }],
      top_devices: [],
      actor: { ai: 0, manual: 0 },
    }
    const wrapper = mount(EventCharts, { props: { stats: empty } })
    await flushPromises()
    expect(wrapper.find('.charts-empty').exists()).toBe(true)
    expect(chartInstances.length).toBe(0)
  })

  it('re-renders on stats change', async () => {
    const wrapper = mount(EventCharts, { props: { stats: STATS } })
    await flushPromises()
    const callsBefore = chartInstances[0].setOption.mock.calls.length
    await wrapper.setProps({ stats: { ...STATS, daily: [...STATS.daily, { day: '09-01', device_op: 2, automation: 0, task: 0, alert: 0 }] } })
    await flushPromises()
    expect(chartInstances[0].setOption.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('emits drill with day on trend bar click', async () => {
    const wrapper = mount(EventCharts, { props: { stats: STATS } })
    await flushPromises()
    // 趋势图是第一个实例；模拟 ECharts click 事件（系列点击携带天名）
    chartInstances[0].__trigger('click', { componentType: 'series', name: '08-30' })
    expect(wrapper.emitted('drill')).toEqual([['08-30']])
    // 非系列点击（如图例/空白）不下钻
    chartInstances[0].__trigger('click', { componentType: 'legend', name: '自动化' })
    expect(wrapper.emitted('drill').length).toBe(1)
  })
})
