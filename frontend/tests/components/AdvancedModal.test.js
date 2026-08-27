import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import AdvancedModal from '../../src/components/AdvancedModal.vue'

describe('AdvancedModal', () => {
  function mountModal() {
    // 组件内容在 <Teleport to="body"> 里，stub 后内联可查
    return mount(AdvancedModal, {
      props: { title: '高级设置' },
      slots: { default: '<p class="inner">正文内容</p>' },
      global: { stubs: { teleport: true } },
    })
  }

  it('渲染标题与默认插槽内容', () => {
    const wrapper = mountModal()
    expect(wrapper.text()).toContain('高级设置')
    expect(wrapper.find('.inner').text()).toBe('正文内容')
  })

  it('点关闭按钮 emit close', async () => {
    const wrapper = mountModal()
    await wrapper.find('.modal-close').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('点击 overlay 自身 emit close（点击内部容器不关）', async () => {
    const wrapper = mountModal()

    // 在容器上派发事件，target 为容器本身 → .self 拦截不关闭
    await wrapper.find('.modal-container').trigger('click')
    expect(wrapper.emitted('close')).toBeFalsy()

    // 直接点 overlay：target 为 overlay 本身 → 关闭
    await wrapper.find('.modal-overlay').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })
})
