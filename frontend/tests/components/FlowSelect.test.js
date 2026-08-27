import { describe, it, expect, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import FlowSelect from '../../src/components/FlowSelect.vue'

const OPTIONS = [
  { value: '', label: '未分配' },
  { value: 'kt', label: '客厅' },
  { value: 'wc', label: '卫生间' },
]

describe('FlowSelect', () => {
  it('无匹配项时显示 placeholder；value 为空串的选项视为"选中未分配"', () => {
    // value='' 的选项会命中默认 modelValue=''，显示的是该选项 label 而非 placeholder
    const withEmpty = mount(FlowSelect, { props: { options: OPTIONS } })
    expect(withEmpty.find('.trigger-text').text()).toBe('未分配')

    const optsNoEmpty = [{ value: 'kt', label: '客厅' }]
    const plain = mount(FlowSelect, { props: { options: optsNoEmpty } })
    expect(plain.find('.trigger-text').text()).toBe('-- 未选择 --')
  })

  it('modelValue 命中选项时显示对应 label', () => {
    const wrapper = mount(FlowSelect, { props: { modelValue: 'kt', options: OPTIONS } })
    expect(wrapper.find('.trigger-text').text()).toBe('客厅')
  })

  it('点开下拉、点选后 emit update:modelValue 与 change 并收起', async () => {
    const wrapper = mount(FlowSelect, { props: { modelValue: '', options: OPTIONS } })

    await wrapper.find('.trigger').trigger('click')
    expect(wrapper.find('.dropdown').exists()).toBe(true)

    const opt = wrapper.findAll('.dropdown div').find(d => d.text() === '卫生间')
    await opt.trigger('click')

    expect(wrapper.emitted('update:modelValue')?.at(-1)).toEqual(['wc'])
    expect(wrapper.emitted('change')?.at(-1)).toEqual(['wc'])
    expect(wrapper.find('.dropdown').exists()).toBe(false)
  })

  it('disabled 时点击不展开', async () => {
    const wrapper = mount(FlowSelect, { props: { options: OPTIONS, disabled: true } })
    await wrapper.find('.trigger').trigger('click')
    expect(wrapper.find('.dropdown').exists()).toBe(false)
  })
})
