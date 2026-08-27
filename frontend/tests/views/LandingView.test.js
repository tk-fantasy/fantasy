import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount } from '@vue/test-utils'
import LandingView from '../../src/views/LandingView.vue'

// useRouter 由视图直接调用，mock 掉 vue-router 的 useRouter
const push = vi.fn()
vi.mock('vue-router', () => ({
  useRouter: () => ({ push }),
}))

describe('LandingView', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    push.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('渲染品牌区与进入按钮，400ms 后内容淡入', async () => {
    const wrapper = mount(LandingView)

    expect(wrapper.find('.title-main').text()).toContain('重新定义')
    expect(wrapper.find('.enter-btn').exists()).toBe(true)
    expect(wrapper.find('.content').classes()).not.toContain('visible')

    await vi.advanceTimersByTimeAsync(400)
    expect(wrapper.find('.content').classes()).toContain('visible')
  })

  it('点进入：先出退场动画类，800ms 后跳 /loading', async () => {
    const wrapper = mount(LandingView)
    await vi.advanceTimersByTimeAsync(400) // 等内容可见

    await wrapper.find('.enter-btn').trigger('click')
    expect(wrapper.find('.content').classes()).toContain('exit')
    expect(push).not.toHaveBeenCalled() // 还在过渡

    await vi.advanceTimersByTimeAsync(800)
    expect(push).toHaveBeenCalledTimes(1)
    expect(push).toHaveBeenCalledWith('/loading')
  })
})
