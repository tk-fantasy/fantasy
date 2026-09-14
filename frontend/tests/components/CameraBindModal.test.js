import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import CameraBindModal from '../../src/components/CameraBindModal.vue'

const CAMERAS = [
  { id: 'cam_1', name: '研发部', enabled: true },
  { id: 'cam_2', name: '门口', enabled: true },
  { id: 'cam_3', name: '已停用', enabled: false },
]

function mountBind(props = {}) {
  return mount(CameraBindModal, {
    props: {
      cameras: CAMERAS,
      ruleName: '有人开研发部灯',
      condition: '画面里有人',
      actionText: '打开研发部灯',
      scopeCameraId: '',
      ...props,
    },
    global: { stubs: { teleport: true } },
  })
}

function chips(wrapper) {
  return wrapper.findAll('.bind-chip')
}

function confirmBtn(wrapper) {
  return wrapper.findAll('button').find(b => b.text().includes('创建规则'))
}

async function pick(wrapper, name) {
  const chip = chips(wrapper).find(c => c.text() === name)
  expect(chip, `找不到选项「${name}」`).toBeTruthy()
  await chip.trigger('click')
  await flushPromises()
}

describe('CameraBindModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.error = vi.fn()
    console.warn = vi.fn()
  })

  it('列出启用的摄像头 + 全部摄像头，停用的那路不出现', () => {
    const wrapper = mountBind()

    expect(chips(wrapper).map(c => c.text()))
      .toEqual(['研发部', '门口', '全部摄像头（全局）'])
  })

  it('展示待绑定的规则要点', () => {
    const wrapper = mountBind()

    expect(wrapper.find('.rule-name').text()).toBe('有人开研发部灯')
    expect(wrapper.text()).toContain('画面里有人')
    expect(wrapper.text()).toContain('打开研发部灯')
  })

  it('默认不选，确认按钮禁用 —— 必须显式做一次选择', () => {
    const wrapper = mountBind()

    expect(chips(wrapper).filter(c => c.classes('active'))).toEqual([])
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.find('.bind-hint').text()).toContain('选一路摄像头')
  })

  it('作用范围是「全局」但规则是视觉类型 → 出错配警告', () => {
    const wrapper = mountBind({ scopeCameraId: '' })

    expect(wrapper.find('.bind-warn').exists()).toBe(true)
    expect(wrapper.find('.bind-warn').text()).toContain('每一路')
  })

  it('作用范围本来就指着某路摄像头 → 无警告且预选那一路', () => {
    const wrapper = mountBind({ scopeCameraId: 'cam_2' })

    expect(wrapper.find('.bind-warn').exists()).toBe(false)
    expect(chips(wrapper).filter(c => c.classes('active')).map(c => c.text()))
      .toEqual(['门口'])
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()
  })

  it('作用范围指向已停用/不存在的摄像头 → 不预选', () => {
    const wrapper = mountBind({ scopeCameraId: 'cam_3' })

    expect(chips(wrapper).filter(c => c.classes('active'))).toEqual([])
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('选一路 → confirm 事件带该 camera_id', async () => {
    const wrapper = mountBind()

    await pick(wrapper, '研发部')
    await confirmBtn(wrapper).trigger('click')

    expect(wrapper.emitted('confirm')[0][0]).toBe('cam_1')
    expect(wrapper.find('.bind-hint').text()).toContain('只有这一路')
  })

  it('选「全部摄像头」→ 红字警告，confirm 带空串（显式全局）', async () => {
    const wrapper = mountBind()

    await pick(wrapper, '全部摄像头（全局）')
    expect(wrapper.find('.bind-hint.danger').text()).toContain('每一路')
    expect(confirmBtn(wrapper).attributes('disabled')).toBeUndefined()

    await confirmBtn(wrapper).trigger('click')
    expect(wrapper.emitted('confirm')[0][0]).toBe('')
  })

  it('改主意：从全局切回具体一路，警告消失', async () => {
    const wrapper = mountBind()

    await pick(wrapper, '全部摄像头（全局）')
    expect(wrapper.find('.bind-hint.danger').exists()).toBe(true)

    await pick(wrapper, '门口')
    expect(wrapper.find('.bind-hint.danger').exists()).toBe(false)

    await confirmBtn(wrapper).trigger('click')
    expect(wrapper.emitted('confirm')[0][0]).toBe('cam_2')
  })

  it('一路摄像头都没有 → 直接说明规则不会触发，确认按钮禁用', () => {
    const wrapper = mountBind({ cameras: [] })

    expect(wrapper.find('.bind-blocked').text()).toContain('没有可用摄像头')
    expect(chips(wrapper)).toEqual([])
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('全部摄像头都停用 → 同样视为没有可用摄像头', () => {
    const wrapper = mountBind({
      cameras: [{ id: 'cam_9', name: '停用的', enabled: false }],
    })

    expect(wrapper.find('.bind-blocked').exists()).toBe(true)
    expect(confirmBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('取消 → 只 emit close，不 emit confirm', async () => {
    const wrapper = mountBind()

    await wrapper.findAll('button').find(b => b.text() === '取消').trigger('click')

    expect(wrapper.emitted('close')).toBeTruthy()
    expect(wrapper.emitted('confirm')).toBeUndefined()
  })

  it('点遮罩关闭 → emit close', async () => {
    const wrapper = mountBind()

    await wrapper.find('.bind-overlay').trigger('click')

    expect(wrapper.emitted('close')).toBeTruthy()
  })

  it('X 按钮关闭 → emit close', async () => {
    const wrapper = mountBind()

    await wrapper.find('.btn-close').trigger('click')

    expect(wrapper.emitted('close')).toBeTruthy()
  })
})
