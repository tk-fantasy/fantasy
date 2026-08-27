import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import CameraSwitcher from '../../src/components/CameraSwitcher.vue'

const cams = (n) => Array.from({ length: n }, (_, i) => ({ id: `cam_${i}`, name: `摄像头${i}` }))

describe('CameraSwitcher', () => {
  it('单路时不渲染切换器(无切换可言)', () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(1), modelValue: 'cam_0' } })
    expect(w.find('.camera-switcher').exists()).toBe(false)
  })

  it('2~4 路渲染标签按钮,active 高亮当前路,点击 emit change', async () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(3), modelValue: 'cam_0' } })
    expect(w.findAll('.camera-tab').length).toBe(3)
    expect(w.find('.camera-tab.active').text()).toBe('摄像头0')

    await w.findAll('.camera-tab')[2].trigger('click')
    expect(w.emitted('change')?.at(-1)).toEqual(['cam_2'])
  })

  it('点击当前已选路不 emit(切路是单例切换,同路无动作)', async () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(2), modelValue: 'cam_0' } })
    await w.findAll('.camera-tab')[0].trigger('click')
    expect(w.emitted('change')).toBeUndefined()
  })

  it('超过 4 路改用下拉列表:展示当前路 label,选中新路后 emit change 并收起', async () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(6), modelValue: 'cam_0' } })
    expect(w.findAll('.camera-tab').length).toBe(0)
    expect(w.find('.flow-select').exists()).toBe(true)
    expect(w.find('.trigger-text').text()).toBe('摄像头0')

    await w.find('.trigger').trigger('click')
    const opt = w.findAll('.dropdown .option').find(d => d.text() === '摄像头3')
    await opt.trigger('click')

    expect(w.emitted('change')?.at(-1)).toEqual(['cam_3'])
    expect(w.find('.dropdown').exists()).toBe(false)
  })

  it('下拉模式下重选当前路不 emit', async () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(5), modelValue: 'cam_0' } })
    await w.find('.trigger').trigger('click')
    const opt = w.findAll('.dropdown .option').find(d => d.text() === '摄像头0')
    await opt.trigger('click')

    expect(w.emitted('change')).toBeUndefined()
  })
})
