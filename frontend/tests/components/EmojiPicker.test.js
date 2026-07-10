import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import EmojiPicker from '../../src/components/EmojiPicker.vue'

describe('EmojiPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    document.body.innerHTML = ''
  })

  function mountPicker(props = {}) {
    return mount(EmojiPicker, {
      props: { visible: true, ...props },
      attachTo: document.body,
    })
  }

  it('does not render when invisible', () => {
    const wrapper = mount(EmojiPicker, {
      props: { visible: false },
      attachTo: document.body,
    })
    expect(document.querySelector('.emoji-picker-overlay')).toBeNull()
  })

  it('renders when visible', () => {
    mountPicker()
    expect(document.querySelector('.emoji-picker-overlay')).not.toBeNull()
  })

  it('renders title and close button', () => {
    mountPicker()
    expect(document.querySelector('.emoji-picker-title').textContent).toBe('选择 Emoji')
    expect(document.querySelector('.emoji-picker-close')).not.toBeNull()
  })

  it('renders search input', () => {
    mountPicker()
    const input = document.querySelector('input')
    expect(input).not.toBeNull()
    expect(input.placeholder).toContain('搜索 emoji')
  })

  it('shows hint when no search query', () => {
    mountPicker()
    expect(document.querySelector('.emoji-picker-hint')).not.toBeNull()
    expect(document.querySelector('.emoji-picker-hint').textContent).toContain('输入关键词搜索 emoji')
  })

  it('emits close when close button clicked', async () => {
    const wrapper = mountPicker()
    document.querySelector('.emoji-picker-close').click()
    await nextTick()
    expect(wrapper.emitted('update:visible')).toBeTruthy()
    expect(wrapper.emitted('update:visible')[0]).toEqual([false])
  })

  it('closes on overlay click', async () => {
    const wrapper = mountPicker()
    document.querySelector('.emoji-picker-overlay').click()
    await nextTick()
    expect(wrapper.emitted('update:visible')).toBeTruthy()
  })

  it('shows search results when API returns data', async () => {
    vi.useFakeTimers()
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          data: {
            status: 'ok',
            results: [
              { char: '🌧️', name: 'rain', score: 0.95 },
              { char: '⛈️', name: 'thunderstorm', score: 0.88 },
            ],
          },
        }),
      })
    )

    mountPicker()
    const input = document.querySelector('input')
    input.value = '下雨'
    input.dispatchEvent(new Event('input'))
    vi.advanceTimersByTime(400)
    await nextTick()
    // resolve fetch
    await vi.runAllTimersAsync()
    await nextTick()

    expect(document.querySelectorAll('.emoji-item').length).toBe(2)
    expect(document.querySelector('.emoji-grid').textContent).toContain('🌧️')
    expect(document.querySelector('.emoji-grid').textContent).toContain('rain')
    vi.useRealTimers()
  })

  it('emits select when clicking an emoji item', async () => {
    vi.useFakeTimers()
    global.fetch = vi.fn(() =>
      Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          data: {
            status: 'ok',
            results: [
              { char: '☀️', name: 'sun', score: 0.99 },
            ],
          },
        }),
      })
    )

    const wrapper = mountPicker()
    const input = document.querySelector('input')
    input.value = '太阳'
    input.dispatchEvent(new Event('input'))
    vi.advanceTimersByTime(400)
    await nextTick()
    await vi.runAllTimersAsync()
    await nextTick()

    document.querySelector('.emoji-item').click()
    await nextTick()
    expect(wrapper.emitted('select')).toBeTruthy()
    expect(wrapper.emitted('select')[0][0]).toEqual({ char: '☀️', name: 'sun', score: 0.99 })
    vi.useRealTimers()
  })
})
