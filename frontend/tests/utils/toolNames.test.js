import { describe, it, expect } from 'vitest'
import { summarizeToolCall, summarizeToolResult } from '../../src/utils/toolNames'

const RESP = (data) => ({ result: JSON.stringify(data) })

describe('summarizeToolResult call_service 多实体摘要', () => {
  it('多实体执行展示全部已执行设备名，不能只显示模型传的那个', () => {
    const out = summarizeToolResult('ha___call_service', true, RESP({
      success: true,
      new_state: { state: 'on', attributes: {} },
      names: ['B灯  研发部灯 左键', 'B灯  会议室灯 右键'],
    }), null)
    expect(out).toContain('研发部灯')
    expect(out).toContain('会议室灯')
    expect(out).toContain('当前: on')
  })

  it('超过 3 个只显示数量，避免摘要过长', () => {
    const out = summarizeToolResult('ha___call_service', true, RESP({
      success: true,
      names: ['a', 'b', 'c', 'd'],
    }), null)
    expect(out).toBe('已生效：共 4 个设备')
  })

  it('单实体保持原口径「已生效，当前: x」', () => {
    const out = summarizeToolResult('ha___call_service', true, RESP({
      success: true,
      new_state: { state: 'off', attributes: {} },
      names: ['床头灯'],
    }), null)
    expect(out).toBe('已生效，当前: off')
  })

  it('无 names 字段（闸门未介入的旧路径）不受影响', () => {
    const out = summarizeToolResult('ha___call_service', true, RESP({
      success: true,
      new_state: { state: 'on', attributes: {} },
    }), null)
    expect(out).toBe('已生效，当前: on')
  })
})

describe('summarizeToolCall 基本口径', () => {
  it('call_service 标题 = 服务中文名 + 友好名', () => {
    expect(summarizeToolCall('ha___call_service', { service: 'turn_on' }, '床头灯'))
      .toBe('开启 床头灯')
  })
})
