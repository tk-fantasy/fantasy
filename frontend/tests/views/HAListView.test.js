import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import HAListView from '../../src/views/HAListView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: [
        { entity_id: 'light.bedroom', name: '卧室灯', state: 'on', domain: 'light', attributes: { brightness: 200 } },
        { entity_id: 'switch.kitchen', name: '厨房开关', state: 'off', domain: 'switch', attributes: {} }
      ]
    })
  })
)

describe('HAListView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders HA list page', () => {
    const wrapper = mount(HAListView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('loads entities on mount', async () => {
    mount(HAListView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/entities', { credentials: 'include' })
  })

  it('renders search input', () => {
    const wrapper = mount(HAListView)
    expect(wrapper.find('input').exists()).toBe(true)
  })

  it('renders area filter', () => {
    const wrapper = mount(HAListView)
    expect(wrapper.text()).toContain('全部')
  })
})
