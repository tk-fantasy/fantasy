import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import BaseToggle from '../../src/components/BaseToggle.vue'

describe('BaseToggle', () => {
  it('renders correctly', () => {
    const wrapper = mount(BaseToggle, {
      props: { modelValue: false }
    })
    expect(wrapper.find('.base-toggle').exists()).toBe(true)
    expect(wrapper.find('.base-toggle-knob').exists()).toBe(true)
  })

  it('applies "on" class when modelValue is true', () => {
    const wrapper = mount(BaseToggle, {
      props: { modelValue: true }
    })
    expect(wrapper.find('.base-toggle').classes()).toContain('on')
  })

  it('does not have "on" class when modelValue is false', () => {
    const wrapper = mount(BaseToggle, {
      props: { modelValue: false }
    })
    expect(wrapper.find('.base-toggle').classes()).not.toContain('on')
  })

  it('emits update:modelValue when clicked', async () => {
    const wrapper = mount(BaseToggle, {
      props: { modelValue: false }
    })
    await wrapper.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeTruthy()
    expect(wrapper.emitted('update:modelValue')[0]).toEqual([true])
  })

  it('emits false when toggled off', async () => {
    const wrapper = mount(BaseToggle, {
      props: { modelValue: true }
    })
    await wrapper.trigger('click')
    expect(wrapper.emitted('update:modelValue')[0]).toEqual([false])
  })

  it('has correct aria attributes', () => {
    const wrapper = mount(BaseToggle, {
      props: { modelValue: true }
    })
    expect(wrapper.find('.base-toggle').attributes('role')).toBe('switch')
    expect(wrapper.find('.base-toggle').attributes('aria-checked')).toBe('true')
  })
})
