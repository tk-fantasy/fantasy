import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import HAListView from '../../src/views/HAListView.vue'

// SensorChart 依赖 echarts，jsdom 无 canvas 会在 init 时抛错 → 打桩
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

// IntersectionObserver 桩：observe 即上报可见（SensorChart 懒启动直接触发）
class IOStub {
  constructor(cb) { this.cb = cb }
  observe(el) { this.cb([{ isIntersecting: true, target: el }]) }
  disconnect() {}
  unobserve() {}
}

// Mock fetch — 返回 entities + devices + services
// 后端 /api/ha/entities 实际返回 { entities, devices, count }，
// 前端 HAListView 读 devices（设备分组），测试 mock 需同步结构。
function mockFetch(entities, services = {}) {
  global.fetch = vi.fn((url) => {
    if (url === '/api/ha/entities') {
      // 把扁平 entities 包成 devices 格式（每实体一个设备，含 entities 子数组）
      const devices = entities.map(e => ({
        area_name: '未分组',
        name: e.name || e.entity_id,
        entities: [e],
      }))
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: { entities, devices, count: entities.length } }),
      })
    }
    if (url === '/api/ha/services') {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: services }),
      })
    }
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
  })
}

describe('HAListView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('IntersectionObserver', IOStub)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders HA list page', () => {
    mockFetch([])
    const wrapper = mount(HAListView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('loads entities on mount', async () => {
    mockFetch([])
    mount(HAListView)
    await flushPromises()
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/entities', { credentials: 'include' })
  })

  it('renders search input', () => {
    mockFetch([])
    const wrapper = mount(HAListView)
    expect(wrapper.find('input').exists()).toBe(true)
  })

  it('renders area filter', () => {
    mockFetch([])
    const wrapper = mount(HAListView)
    expect(wrapper.text()).toContain('全部')
  })

  it('sensor card is clickable even without services', async () => {
    // sensor 域无任何服务，但应仍可点击查看数值/历史
    mockFetch(
      [{ entity_id: 'sensor.temp', name: '温度', state: '22', domain: 'sensor', attributes: { unit_of_measurement: '°C' } }],
      {}  // 无任何服务定义
    )
    const wrapper = mount(HAListView)
    await flushPromises()
    const card = wrapper.find('.device-card')
    expect(card.classes()).toContain('clickable')
    // 传感器无可控服务 → card-spec 显示 "0 可控"（非独立 .ctrl-badge，模板已改）
    expect(wrapper.find('.card-spec').text()).toContain('0 可控')
  })

  it('controllable device shows clickable + 可控 badge', async () => {
    mockFetch(
      [{ entity_id: 'light.lamp', name: '灯', state: 'on', domain: 'light', attributes: {} }],
      { light: { turn_on: { fields: ['entity_id'] } } }
    )
    const wrapper = mount(HAListView)
    await flushPromises()
    const card = wrapper.find('.device-card')
    expect(card.classes()).toContain('clickable')
    // 模板用 .card-spec 显示 "X 可控 · Y 属性"（非独立 .ctrl-badge）
    expect(wrapper.find('.card-spec').text()).toContain('可控')
  })

  // 多传感设备：打开详情后每个带历史的实体各一张趋势图（平铺），不再共用选中图位
  function mockMultiSensorDevice(entityIds) {
    const entities = entityIds.map((id, i) => ({
      entity_id: id,
      name: `传感器${i}`,
      state: '22',
      domain: 'sensor',
      attributes: { unit_of_measurement: '°C' },
    }))
    global.fetch = vi.fn((url) => {
      if (url === '/api/ha/entities') {
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve({
            data: {
              entities,
              devices: [{ area_name: '客厅', name: '温湿度计', entities }],
              count: entities.length,
            },
          }),
        })
      }
      if (url === '/api/ha/services') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
    })
  }

  it('tiles one trend chart per sensor entity in device modal', async () => {
    mockMultiSensorDevice(['sensor.temp', 'sensor.hum', 'sensor.bat'])
    const wrapper = mount(HAListView, { global: { stubs: { teleport: true } } })
    await flushPromises()
    await wrapper.find('.device-card').trigger('click')
    await flushPromises()
    const cards = wrapper.findAll('.history-card')
    expect(cards.length).toBe(3)
    expect(wrapper.find('.history-card .history-card-name').text()).toContain('传感器0')
    // 每张卡各挂一个 SensorChart（含 canvas 容器）
    expect(wrapper.findAll('.history-card .chart-canvas').length).toBe(3)
  })

  it('collapses to 4 charts with expand button when device has many sensors', async () => {
    mockMultiSensorDevice([
      'sensor.a', 'sensor.b', 'sensor.c', 'sensor.d', 'sensor.e', 'sensor.f',
    ])
    const wrapper = mount(HAListView, { global: { stubs: { teleport: true } } })
    await flushPromises()
    await wrapper.find('.device-card').trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.history-card').length).toBe(4)
    const moreBtn = wrapper.find('.history-more')
    expect(moreBtn.text()).toContain('展开其余 2 个')
    await moreBtn.trigger('click')
    await flushPromises()
    expect(wrapper.findAll('.history-card').length).toBe(6)
    expect(wrapper.find('.history-more').exists()).toBe(false)
  })

  it('hides history section for device without history entities', async () => {
    mockFetch(
      [{ entity_id: 'light.lamp', name: '灯', state: 'on', domain: 'light', attributes: {} }],
      { light: { turn_on: { fields: ['entity_id'] } } }
    )
    const wrapper = mount(HAListView, { global: { stubs: { teleport: true } } })
    await flushPromises()
    await wrapper.find('.device-card').trigger('click')
    await flushPromises()
    expect(wrapper.find('.history-section').exists()).toBe(false)
  })
})
