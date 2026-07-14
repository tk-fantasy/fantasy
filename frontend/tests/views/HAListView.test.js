import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import HAListView from '../../src/views/HAListView.vue'

// Mock fetch — 返回 entities + services
function mockFetch(entities, services = {}) {
  global.fetch = vi.fn((url) => {
    if (url === '/api/ha/entities') {
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ data: { entities, count: entities.length } }),
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
    // 不应显示"可控" badge（传感器不是可控设备）
    expect(wrapper.find('.ctrl-badge').exists()).toBe(false)
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
    expect(wrapper.find('.ctrl-badge').exists()).toBe(true)
  })
})
