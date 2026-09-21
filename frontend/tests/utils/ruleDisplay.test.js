import { describe, it, expect } from 'vitest'
import { formatCondition, formatActions, formatSingleAction } from '../../src/utils/ruleDisplay'

describe('formatCondition', () => {
  it('空值渲染 —（统一 modal 行为；原 TaskView 会渲染字面 "null"）', () => {
    expect(formatCondition(null)).toBe('—')
    expect(formatCondition(undefined)).toBe('—')
    expect(formatCondition('')).toBe('—')
  })

  it('字符串直通', () => {
    expect(formatCondition('有人出现')).toBe('有人出现')
  })

  it('识别 visual/time/weather（原 modal 缺失的三分支）', () => {
    expect(formatCondition({ visual: '有人出现' })).toBe('视觉: 有人出现')
    expect(formatCondition({ time: '08:00' })).toBe('时间: 08:00')
    expect(formatCondition({ weather: '雨' })).toBe('天气: 雨')
  })

  it('description/type 优先于具体字段', () => {
    expect(formatCondition({ description: 'd', type: 'vision' })).toBe('d')
    expect(formatCondition({ type: 'vision', visual: 'x' })).toBe('vision')
  })
})

describe('formatSingleAction', () => {
  it('MCP 格式走 6 项动词表（含 set_temperature）', () => {
    expect(formatSingleAction({
      mcp_tool_name: 'ha_devices___call_service',
      mcp_tool_input: { service: 'set_temperature', entity_id: 'climate.chuang_tou_deng' },
    })).toBe('chuang tou deng 设置温度')
  })

  it('直连格式同样吃到 6 项动词表（原 TaskView 直连表只有 4 项，set_temperature 原样漏出）', () => {
    expect(formatSingleAction({ service: 'set_temperature', entity_id: 'climate.chuang_tou_deng' }))
      .toBe('chuang tou deng 设置温度')
    expect(formatSingleAction({ service: 'turn_on', entity_id: 'light.chuang_tou_deng' }))
      .toBe('chuang tou deng 打开')
  })

  it('未知 service 原样回显', () => {
    expect(formatSingleAction({ service: 'weird_service', entity_id: 'light.x' })).toBe('x weird_service')
  })

  it('字符串直通、空值返回空串', () => {
    expect(formatSingleAction('关灯')).toBe('关灯')
    expect(formatSingleAction(null)).toBe('')
  })
})

describe('formatActions', () => {
  const mcp = { mcp_tool_name: 't', mcp_tool_input: { service: 'turn_on', entity_id: 'light.a' } }

  it('descs 优先于逐项解析', () => {
    expect(formatActions([mcp], ['打开灯'])).toEqual(['打开灯'])
  })

  it('descs 缺位时逐项回退解析', () => {
    expect(formatActions([mcp, mcp], ['打开灯'])).toEqual(['打开灯', 'a 打开'])
  })

  it('actions 为 JSON 字符串也能解析（TaskView 防御路径）', () => {
    expect(formatActions(JSON.stringify([mcp]), [])).toEqual(['a 打开'])
  })

  it('actions 为单对象时包成数组', () => {
    expect(formatActions(mcp, [])).toEqual(['a 打开'])
  })

  it('空 actions 但有 descs → 显示 descs（modal 行为）', () => {
    expect(formatActions([], ['打开灯'])).toEqual(['打开灯'])
  })

  it('什么都没有 → 空数组', () => {
    expect(formatActions(null, [])).toEqual([])
  })
})
