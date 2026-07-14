import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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

describe('SensorChart', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('shows loading state initially', () => {
    global.fetch = vi.fn(() => new Promise(() => {})) // never resolves
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.temp', unit: '°C' } })
    expect(wrapper.find('.chart-status').text()).toContain('加载')
  })

  it('renders chart with history data', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: { history: makeHistory([20, 22, 24, 23, 25]) } }),
      })
    )
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.temp', unit: '°C' } })
    await flushPromises()
    expect(wrapper.find('.chart-svg').exists()).toBe(true)
    expect(wrapper.find('.chart-line').exists()).toBe(true)
    // 当前值取最后一个点
    expect(wrapper.find('.chart-current').text()).toContain('25')
  })

  it('shows empty state when no history', async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: { history: [] } }),
      })
    )
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
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          data: { history: makeHistory(['unknown', 22, 'unavailable', 24]) }
        }),
      })
    )
    const wrapper = mount(SensorChart, { props: { entityId: 'sensor.temp', unit: '°C' } })
    await flushPromises()
    // 只有 2 个有效点 → 不够画线（需 >=2），刚好 2 个能画
    expect(wrapper.find('.chart-svg').exists()).toBe(true)
  })
})
