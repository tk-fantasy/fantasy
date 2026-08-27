import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useGreeting } from '../../src/composables/useGreeting'

global.fetch = vi.fn()

function hourAt(h) {
  // 固定系统时间到当天 h 点，供时段问候分段判断
  vi.useFakeTimers()
  vi.setSystemTime(new Date(2026, 7, 27, h, 0, 0))
}

// getGreeting 未导出，经由 showGreetingMessage 的产出（greetingText）验证分段
async function greetingAt(hour, ownerName = '') {
  hourAt(hour)
  fetch.mockResolvedValueOnce({
    json: () => Promise.resolve({ data: { owner_name: ownerName } }),
  })
  const g = useGreeting()
  await g.showGreetingMessage()
  return g.greetingText.value
}

describe('useGreeting', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.error = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('按小时分段：早上好/中午好/下午好/晚上好', async () => {
    expect(await greetingAt(6)).toBe('早上好')
    expect(await greetingAt(13)).toBe('中午好')
    expect(await greetingAt(15)).toBe('下午好')
    expect(await greetingAt(22)).toBe('晚上好')
    // 边界：5 点起算早上；12 点整算中午；14/18 各归下一段
    expect(await greetingAt(5)).toBe('早上好')
    expect(await greetingAt(12)).toBe('中午好')
    expect(await greetingAt(14)).toBe('下午好')
    expect(await greetingAt(18)).toBe('晚上好')
  })

  it('有房主名时拼进问候语', async () => {
    expect(await greetingAt(9, '老王')).toBe('早上好，老王')
  })

  it('showGreetingMessage 拉房主名并显示，1.5 秒后隐藏', async () => {
    hourAt(10)
    fetch.mockResolvedValueOnce({
      json: () => Promise.resolve({ data: { owner_name: '张三' } }),
    })
    const g = useGreeting()

    await g.showGreetingMessage()
    expect(fetch).toHaveBeenCalledWith('/api/home/info')
    expect(g.greetingText.value).toBe('早上好，张三')
    expect(g.showGreeting.value).toBe(true)

    await vi.advanceTimersByTimeAsync(1500)
    expect(g.showGreeting.value).toBe(false)
  })

  it('home 接口缺 owner_name 时只用时段问候', async () => {
    hourAt(20)
    fetch.mockResolvedValueOnce({
      json: () => Promise.resolve({ data: {} }),
    })
    const g = useGreeting()
    await g.showGreetingMessage()
    expect(g.greetingText.value).toBe('晚上好')
  })
})
