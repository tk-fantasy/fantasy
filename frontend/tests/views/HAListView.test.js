import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import HAListView from '../../src/views/HAListView.vue'

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
})
