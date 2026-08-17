import { describe, it, expect } from 'vitest'
import { getRuleMismatch } from '../../src/utils/ruleMismatch'

describe('getRuleMismatch', () => {
  // —— 红级：全局视觉规则 ——
  it('vision + 无 camera_id → red', () => {
    expect(getRuleMismatch({ type: 'vision', camera_id: '' })).toBe('red')
  })
  it('type 缺失按 vision 兜底 + 无 camera_id → red', () => {
    expect(getRuleMismatch({ camera_id: '' })).toBe('red')
    expect(getRuleMismatch({ type: null, camera_id: '' })).toBe('red')
  })
  it('type 非法值按 vision 兜底 + 无 camera_id → red', () => {
    expect(getRuleMismatch({ type: 'unknown', camera_id: '' })).toBe('red')
  })
  it('type 大小写归一化：VISION + 无 camera_id → red', () => {
    expect(getRuleMismatch({ type: 'VISION', camera_id: '' })).toBe('red')
  })

  // —— 橙级：绑定摄像头的定时/天气规则 ——
  it('time + camera_id → orange', () => {
    expect(getRuleMismatch({ type: 'time', camera_id: 'cam1' })).toBe('orange')
  })
  it('weather + camera_id → orange', () => {
    expect(getRuleMismatch({ type: 'weather', camera_id: 'cam1' })).toBe('orange')
  })
  it('camera_id 缺失按空串处理：time → 无标记', () => {
    expect(getRuleMismatch({ type: 'time' })).toBe('')
  })

  // —— 正常规则：无标记 ——
  it('vision + camera_id → 无标记', () => {
    expect(getRuleMismatch({ type: 'vision', camera_id: 'cam1' })).toBe('')
  })
  it('time + 无 camera_id → 无标记', () => {
    expect(getRuleMismatch({ type: 'time', camera_id: '' })).toBe('')
  })
  it('weather + 无 camera_id → 无标记', () => {
    expect(getRuleMismatch({ type: 'weather', camera_id: '' })).toBe('')
  })
})
