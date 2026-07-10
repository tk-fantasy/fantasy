import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import WeatherWidget from '../../src/components/WeatherWidget.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      code: 'ok',
      data: {
        temperature: '25',
        weather: '晴',
        location: '上海',
        icon: '100',
        feels_like: '26',
        humidity: '60',
        wind_dir: '东南',
        wind_scale: '3',
        wind_speed: '15',
        visibility: '10',
        uv_index: '5',
        location_id: '101020100',
        obs_time: '2024-01-01T12:00:00',
        indices: []
      }
    })
  })
)

describe('WeatherWidget', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders weather widget', async () => {
    const wrapper = mount(WeatherWidget)
    await vi.dynamicImportSettled()
    expect(wrapper.find('.weather-widget').exists()).toBe(true)
  })

  it('displays temperature', async () => {
    const wrapper = mount(WeatherWidget)
    await vi.dynamicImportSettled()
    expect(wrapper.find('.weather-temp').text()).toBe('25°')
  })

  it('displays weather description', async () => {
    const wrapper = mount(WeatherWidget)
    await vi.dynamicImportSettled()
    expect(wrapper.find('.weather-desc').text()).toBe('晴')
  })

  it('displays location', async () => {
    const wrapper = mount(WeatherWidget)
    await vi.dynamicImportSettled()
    expect(wrapper.find('.weather-location').text()).toBe('上海')
  })

  it('toggles expand on click', async () => {
    const wrapper = mount(WeatherWidget)
    await vi.dynamicImportSettled()
    
    expect(wrapper.find('.weather-dropdown').exists()).toBe(false)
    
    await wrapper.find('.weather-widget').trigger('click')
    expect(wrapper.find('.weather-dropdown').exists()).toBe(true)
    
    await wrapper.find('.weather-widget').trigger('click')
    expect(wrapper.find('.weather-dropdown').exists()).toBe(false)
  })

  it('shows expand icon', async () => {
    const wrapper = mount(WeatherWidget)
    await vi.dynamicImportSettled()
    expect(wrapper.find('.expand-icon').text()).toBe('▼')
    
    await wrapper.find('.weather-widget').trigger('click')
    expect(wrapper.find('.expand-icon').text()).toBe('▲')
  })

  it('fetches weather on mount', async () => {
    mount(WeatherWidget)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/weather')
  })
})
