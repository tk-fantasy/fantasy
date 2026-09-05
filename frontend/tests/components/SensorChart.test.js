import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

// echarts 桩：jsdom 没有 canvas，echarts.init 会抛错导致 onMounted 中断、
// loadHistory 永不执行（组件卡在 loading）。init 返回 no-op 实例即可走通数据流。
vi.mock('echarts/core', () => ({
  use: vi.fn(),
  init: vi.fn(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() })),
  graphic: { LinearGradient: class {} },
}))
vi.mock('echarts/charts', () => ({ LineChart: {} }))
vi.mock('echarts/components', () => ({
  GridComponent: {},
  TooltipComponent: {},
  DataZoomComponent: {},
  MarkLineComponent: {},
}))
vi.mock('echarts/renderers', () => ({ CanvasRenderer: {} }))

import SensorChart from '../../src/components/SensorChart.vue'

// 构造 HA history 响应：[[{state, last_updated}, ...]]
function makeHistory(states) {
  const now = Date.now()
  return [
    states.map((s, i) => ({
      state: String(s),
      last_updated: new Date(now - (states.length - 1 - i) * 3600_000).toISOString(),
    }))
  ]
}

function okJson(history) {
  return { ok: true, json: () => Promise.resolve({ data: { history } }) }
}

// IntersectionObserver 桩：保留实例引用供测试手动触发可见。
// autoVisible=true 时 observe 即上报可见（组件立即懒启动，行为与真实浏览器一致）。
let autoVisible
const ioInstances = []
class IOStub {
  constructor(cb) { this.cb = cb; ioInstances.push(this) }
  observe(el) {
    if (autoVisible) this.cb([{ isIntersecting: true, target: el }])
  }
  disconnect() {}
  unobserve() {}
}

function triggerVisible() {
  for (const io of ioInstances) io.cb([{ isIntersecting: true, target: null }])
}

describe('SensorChart', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ioInstances.length = 0
    autoVisible = true
    vi.stubGlobal('IntersectionObserver', IOStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows loading state initially', () => {
    global.fetch = vi.fn(() => new Promise(() => {})) // never resolves
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.temp', unit: '°C' } })
    expect(wrapper.find('.chart-status').text()).toContain('加载')
  })

  it('renders chart with history data', async () => {
    global.fetch = vi.fn(() => Promise.resolve(okJson(makeHistory([20, 22, 24, 23, 25]))))
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.temp', unit: '°C' } })
    await flushPromises()
    // 有数据 → loading 结束、canvas 容器存在、当前值取最后一个点
    expect(wrapper.find('.chart-status').exists()).toBe(false)
    expect(wrapper.find('.chart-canvas').exists()).toBe(true)
    expect(wrapper.find('.chart-current').text()).toContain('25')
  })

  it('shows empty state when no history', async () => {
    global.fetch = vi.fn(() => Promise.resolve(okJson([])))
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.nodata', unit: '' } })
    await flushPromises()
    expect(wrapper.find('.chart-status').text()).toContain('暂无')
  })

  it('shows error on fetch failure', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: false,
        json: () => Promise.resolve({ message: '查询失败' }),
      })
    )
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.err', unit: '' } })
    await flushPromises()
    expect(wrapper.find('.chart-error').text()).toContain('查询失败')
  })

  it('filters out non-numeric states', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve(okJson(makeHistory(['unknown', 22, 'unavailable', 24])))
    )
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.temp', unit: '°C' } })
    await flushPromises()
    // unknown/unavailable 被过滤，剩 22/24 两点（>=2 判定有数据），当前值 24
    expect(wrapper.find('.chart-status').exists()).toBe(false)
    expect(wrapper.find('.chart-current').text()).toContain('24')
  })

  it('waits for viewport before fetching, then loads when visible', async () => {
    const fetchMock = vi.fn(() => Promise.resolve(okJson(makeHistory([20, 21]))))
    global.fetch = fetchMock
    autoVisible = false
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.lazy', unit: '' } })
    await flushPromises()
    // 不可见 → 不发请求，显示占位文案
    expect(fetchMock).not.toHaveBeenCalled()
    expect(wrapper.find('.chart-status').text()).toContain('滚动到此处')

    // 进入视口 → 懒启动拉取并渲染
    triggerVisible()
    await flushPromises()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.chart-status').exists()).toBe(false)
    expect(wrapper.find('.chart-current').text()).toContain('21')
  })

  it('ignores stale response after entityId switch', async () => {
    let resolveA
    global.fetch = vi.fn((url) => {
      if (url.includes('sensor.a')) {
        return new Promise((resolve) => { resolveA = () => resolve(okJson(makeHistory([25, 26]))) })
      }
      return Promise.resolve(okJson(makeHistory([10, 11])))
    })
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.a', unit: '°C' } })
    await flushPromises() // A 的请求仍未返回
    await wrapper.setProps({ entityId: 'sensor.b' })
    await flushPromises() // B 立即返回 → 当前值 11
    expect(wrapper.find('.chart-current').text()).toContain('11')
    // A 的过期响应到达 → 被丢弃，不覆盖 B 的数据
    resolveA()
    await flushPromises()
    expect(wrapper.find('.chart-current').text()).toContain('11')
  })
})
