import { describe, it, expect } from 'vitest'
import { resolveCapabilities, formatSliderValue, toActualValue } from '../../src/utils/deviceCapabilities'

describe('resolveCapabilities', () => {
  const lightServices = {
    light: {
      turn_on: { fields: ['entity_id', 'brightness_pct', 'color_temp'], required: ['entity_id'] },
      turn_off: { fields: ['entity_id'], required: ['entity_id'] },
      toggle: { fields: ['entity_id'], required: ['entity_id'] },
    },
  }

  const climateServices = {
    climate: {
      set_temperature: { fields: ['entity_id', 'temperature', 'hvac_mode'], required: ['entity_id', 'temperature'] },
      set_fan_mode: { fields: ['entity_id', 'fan_mode'], required: ['entity_id'] },
      set_swing_mode: { fields: ['entity_id', 'swing_mode'], required: ['entity_id'] },
      turn_on: { fields: ['entity_id'], required: ['entity_id'] },
      turn_off: { fields: ['entity_id'], required: ['entity_id'] },
    },
  }

  const coverServicesWithPos = {
    cover: {
      open_cover: { fields: ['entity_id'], required: ['entity_id'] },
      close_cover: { fields: ['entity_id'], required: ['entity_id'] },
      set_cover_position: { fields: ['entity_id', 'position'], required: ['entity_id'] },
    },
  }

  it('returns empty array for entity with no matching services', () => {
    const entity = { entity_id: 'sensor.temp', attributes: { unit_of_measurement: '°C' }, state: '22' }
    const result = resolveCapabilities(entity, {})
    expect(result).toEqual([])
  })

  it('derives enum control from array attribute and matching service field', () => {
    const entity = {
      entity_id: 'climate.hvac',
      attributes: {
        fan_modes: ['auto', 'low', 'medium', 'high'],
        fan_mode: 'auto',
      },
      state: 'off',
    }
    const result = resolveCapabilities(entity, climateServices)
    const enumCaps = result.filter(c => c.type === 'enum')
    expect(enumCaps.length).toBeGreaterThanOrEqual(1)
    const fanMode = enumCaps.find(c => c.key === 'fan_modes')
    expect(fanMode).toBeTruthy()
    expect(fanMode.options).toEqual(['auto', 'low', 'medium', 'high'])
    expect(fanMode.current).toBe('auto')
  })

  it('derives slider control from numeric attribute', () => {
    const entity = {
      entity_id: 'light.bed_light',
      attributes: {
        brightness: 128,
        min_color_temp_kelvin: 2000,
        max_color_temp_kelvin: 6500,
      },
      state: 'on',
    }
    const result = resolveCapabilities(entity, lightServices)
    const sliders = result.filter(c => c.type === 'slider')
    expect(sliders.length).toBeGreaterThanOrEqual(1)
    const brightness = sliders.find(c => c.param === 'brightness_pct')
    expect(brightness).toBeTruthy()
    expect(brightness.action).toBe('turn_on')
  })

  it('derives slider for cover position', () => {
    const entity = {
      entity_id: 'cover.curtain',
      attributes: {
        current_position: 50,
      },
      state: 'open',
    }
    const result = resolveCapabilities(entity, coverServicesWithPos)
    const sliders = result.filter(c => c.type === 'slider')
    expect(sliders.length).toBeGreaterThanOrEqual(1)
    const position = sliders.find(c => c.param === 'position')
    expect(position).toBeTruthy()
    expect(position.action).toBe('set_cover_position')
  })

  it('derives slider with unit from unit_of_measurement', () => {
    const entity = {
      entity_id: 'climate.hvac',
      attributes: {
        temperature: 22,
        min_temp: 16,
        max_temp: 30,
        target_temp_step: 1,
        unit_of_measurement: '°C',
      },
      state: 'off',
    }
    const result = resolveCapabilities(entity, climateServices)
    const sliders = result.filter(c => c.type === 'slider')
    expect(sliders.length).toBeGreaterThanOrEqual(1)
    const tempSlider = sliders.find(c => c.param === 'temperature')
    expect(tempSlider).toBeTruthy()
    expect(tempSlider.unit).toBe('°C')
    expect(tempSlider.min).toBe(16)
    expect(tempSlider.max).toBe(30)
  })

  it('skips enum for supported_* attributes', () => {
    const entity = {
      entity_id: 'climate.hvac',
      attributes: {
        supported_features: [1, 2, 3],
      },
      state: 'off',
    }
    const result = resolveCapabilities(entity, climateServices)
    const supportedCaps = result.filter(c => c.key === 'supported_features')
    expect(supportedCaps.length).toBe(0)
  })
})

describe('formatSliderValue', () => {
  it('returns — for null', () => {
    expect(formatSliderValue({ current: null, unit: '' })).toBe('—')
  })

  it('returns number with unit', () => {
    expect(formatSliderValue({ current: 22, unit: '°C' })).toBe('22°C')
  })

  it('rounds float values', () => {
    expect(formatSliderValue({ current: 50.7, unit: '%' })).toBe('51%')
  })
})

describe('toActualValue', () => {
  it('returns input value without inputScale', () => {
    expect(toActualValue({}, 100)).toBe(100)
  })

  it('applies inputScale when present', () => {
    const result = toActualValue({ inputScale: 2.55 }, 100)
    expect(result).toBeCloseTo(255, 0)
  })
})
