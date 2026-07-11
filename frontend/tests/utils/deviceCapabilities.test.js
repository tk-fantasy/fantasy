import { describe, it, expect } from 'vitest'
import { adaptControls, formatSliderValue, toActualValue } from '../../src/utils/deviceCapabilities'

describe('adaptControls', () => {
  it('returns empty array for null/undefined controls', () => {
    expect(adaptControls(null, {})).toEqual([])
    expect(adaptControls(undefined, {})).toEqual([])
    expect(adaptControls({}, {})).toEqual([])
  })

  it('adapts enum control from backend _controls dict', () => {
    // 后端 resolve_controls 返回的 dict，key 是控制名
    const controls = {
      fan_mode: { type: 'enum', service: 'set_fan_mode', param: 'fan_mode', options: ['auto', 'low', 'high'], current: 'auto' },
    }
    const entity = { entity_id: 'climate.hvac', attributes: { fan_mode: 'auto', fan_modes: ['auto', 'low', 'high'] } }
    const result = adaptControls(controls, entity)

    expect(result.length).toBe(1)
    const cap = result[0]
    expect(cap.type).toBe('enum')
    expect(cap.key).toBe('fan_mode')
    expect(cap.options).toEqual(['auto', 'low', 'high'])
    expect(cap.current).toBe('auto')
    expect(cap.service).toBe('climate')
    expect(cap.action).toBe('set_fan_mode')
    expect(cap.param).toBe('fan_mode')
    expect(cap.currentAttr).toBe('fan_mode')
    expect(cap.label).toBe('Fan Mode')
  })

  it('adapts slider control with pctMatch detection', () => {
    const controls = {
      brightness_pct: { type: 'slider', service: 'turn_on', param: 'brightness_pct', min: 0, max: 100, step: 1, current: 50, unit: '%' },
    }
    const entity = { entity_id: 'light.lamp', attributes: { brightness_pct: 50 } }
    const result = adaptControls(controls, entity)

    expect(result.length).toBe(1)
    const cap = result[0]
    expect(cap.type).toBe('slider')
    expect(cap.key).toBe('brightness_pct')
    expect(cap.min).toBe(0)
    expect(cap.max).toBe(100)
    expect(cap.step).toBe(1)
    expect(cap.current).toBe(50)
    expect(cap.unit).toBe('%')
    expect(cap.service).toBe('light')
    expect(cap.action).toBe('turn_on')
    expect(cap.pctMatch).toBe(true)
  })

  it('adapts slider without pctMatch for non-_pct param', () => {
    const controls = {
      temperature: { type: 'slider', service: 'set_temperature', param: 'temperature', min: 16, max: 30, step: 1, current: 22, unit: '°C' },
    }
    const entity = { entity_id: 'climate.hvac', attributes: { temperature: 22 } }
    const result = adaptControls(controls, entity)

    const cap = result[0]
    expect(cap.pctMatch).toBe(false)
    expect(cap.label).toBe('Temperature')
  })

  it('aggregates action controls into single _actions entry', () => {
    const controls = {
      open_cover: { type: 'action', service: 'open_cover', param: null },
      close_cover: { type: 'action', service: 'close_cover', param: null },
      stop_cover: { type: 'action', service: 'stop_cover', param: null },
    }
    const entity = { entity_id: 'cover.curtain', attributes: {} }
    const result = adaptControls(controls, entity)

    // 三个 action 应聚合为单个 _actions 条目
    expect(result.length).toBe(1)
    const cap = result[0]
    expect(cap.type).toBe('action')
    expect(cap.key).toBe('_actions')
    expect(cap.actions.length).toBe(3)
    expect(cap.actions[0].service).toBe('cover')
    expect(cap.actions[0].action).toBe('open_cover')
    expect(cap.actions[0].label).toBe('Open')
  })

  it('handles mixed enum + slider + action controls', () => {
    const controls = {
      fan_mode: { type: 'enum', service: 'set_fan_mode', param: 'fan_mode', options: ['auto', 'low'], current: 'auto' },
      temperature: { type: 'slider', service: 'set_temperature', param: 'temperature', min: 16, max: 30, step: 1, current: 22, unit: '°C' },
      turn_on: { type: 'action', service: 'turn_on', param: null },
    }
    const entity = { entity_id: 'climate.hvac', attributes: { temperature: 22, fan_mode: 'auto' } }
    const result = adaptControls(controls, entity)

    // enum + slider + 1 个聚合的 action 条目
    expect(result.length).toBe(3)
    expect(result.filter(c => c.type === 'enum').length).toBe(1)
    expect(result.filter(c => c.type === 'slider').length).toBe(1)
    expect(result.filter(c => c.type === 'action').length).toBe(1)
  })

  it('skips entries without type field', () => {
    const controls = {
      fan_mode: { type: 'enum', service: 'set_fan_mode', param: 'fan_mode', options: ['auto'], current: 'auto' },
      broken: { service: 'something', param: 'x' },  // 无 type，应跳过
    }
    const entity = { entity_id: 'climate.hvac', attributes: {} }
    const result = adaptControls(controls, entity)
    expect(result.length).toBe(1)
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
