import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import MonitorView from '../../src/views/MonitorView.vue'

global.fetch = vi.fn()

const METRICS = {
  http: { total: 128, errors: 3, avg_latency_s: 0.25, p95_latency_s: 0.8, latency_samples: 100 },
  tools: { calls: { weather: 5, vision_chat: 12 }, errors: { vision_chat: 2 } },
  llm: { calls: 42, errors: 1 },
  automation: { evals: 7 },
}

describe('MonitorView', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    console.error = vi.fn()
  })

  afterEach(() => {
    // 卸载组件清掉 5s 轮询后再恢复真实计时器
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('加载中显示 loading，数据到达后渲染指标卡', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ code: 'ok', data: METRICS }),
    })
    const wrapper = mount(MonitorView)
    expect(wrapper.find('.loading-state').exists()).toBe(true)

    await flushPromises()

    const values = wrapper.findAll('.card-value')
    expect(wrapper.text()).toContain('HTTP 请求')
    expect(values.map(v => v.text())).toContain('128')
    expect(wrapper.find('.loading-state').exists()).toBe(false)
  })

  it('formatLatency 分段：0 → -；<1ms → μs；<1s → ms；≥1s → s', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ code: 'ok', data: METRICS }),
    })
    const wrapper = mount(MonitorView)
    await flushPromises()

    // avg=0.25s、p95=0.8s
    expect(wrapper.findAll('.card-value').map(v => v.text())).toContain('250.0ms')
    expect(wrapper.findAll('.card-value').map(v => v.text())).toContain('800.0ms')
  })

  it('每 5 秒轮询一次 /api/metrics', async () => {
    fetch.mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ code: 'ok', data: METRICS }),
    })
    mount(MonitorView)
    await flushPromises()
    const initial = fetch.mock.calls.length

    await vi.advanceTimersByTimeAsync(5000)
    expect(fetch.mock.calls.length).toBe(initial + 1)
    await vi.advanceTimersByTimeAsync(5000)
    expect(fetch.mock.calls.length).toBe(initial + 2)
  })

  it('接口失败时静默降级（不抛出、仍退出 loading）', async () => {
    fetch.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(MonitorView)
    await flushPromises()

    expect(console.error).toHaveBeenCalled()
    expect(wrapper.find('.loading-state').exists()).toBe(false)
  })

  it('非 code=ok 的响应不覆盖默认指标', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ code: 'error', message: 'x' }),
    })
    const wrapper = mount(MonitorView)
    await flushPromises()

    expect(wrapper.findAll('.card-value').map(v => v.text())).toContain('0')
  })
})
