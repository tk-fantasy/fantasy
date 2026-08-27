import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import CameraSettingsView from '../../src/views/CameraSettingsView.vue'

// 摄像头管理页：走 useCamera → global.fetch，按 URL 分流返回
function makeFetch({ cameras = [], areas = [] } = {}) {
  return vi.fn(url => {
    if (url === '/api/cameras') return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: cameras }) })
    if (url === '/api/ha/areas') return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: areas }) })
    if (String(url).includes('/focuses')) return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
  })
}

const CAMERAS = [
  { id: 'a', name: '前门', source_type: 'rtsp', rtsp_url: 'rtsp://1.1.1.1/stream', enabled: 1, display_enabled: 0, area: '玄关' },
  { id: 'b', name: '测试插件', source_type: 'test', enabled: 1, display_enabled: 0 },
]

async function mountView() {
  const wrapper = mount(CameraSettingsView, {
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

describe('CameraSettingsView', () => {
  beforeEach(() => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.stubGlobal('alert', vi.fn())
    vi.stubGlobal('prompt', vi.fn(() => null))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('挂载时并发拉取摄像头与区域，渲染页面骨架', async () => {
    const f = makeFetch({ cameras: CAMERAS, areas: [{ name: '玄关' }] })
    global.fetch = f

    const wrapper = mount(CameraSettingsView, { global: { stubs: { teleport: true } } })
    await flushPromises()

    expect(wrapper.find('h1').text()).toBe('摄像头管理')
    const urls = f.mock.calls.map(c => c[0])
    expect(urls).toContain('/api/cameras')
    expect(urls).toContain('/api/ha/areas')
  })

  it('副标题显示摄像头路数；卡片显示名称与 RTSP 地址', async () => {
    global.fetch = makeFetch({ cameras: CAMERAS, areas: [] })
    const wrapper = await mountView()

    expect(wrapper.find('.page-sub').text()).toContain('2 路摄像头')
    const cards = wrapper.findAll('.cam-card')
    expect(cards.length).toBe(2)
    expect(cards[0].text()).toContain('前门')
    expect(cards[0].text()).toContain('rtsp://1.1.1.1/stream')
  })

  it('插件虚拟摄像头卡有徽标且不提供配置/删除按钮', async () => {
    global.fetch = makeFetch({ cameras: CAMERAS, areas: [] })
    const wrapper = await mountView()

    const testCard = wrapper.findAll('.cam-card')[1]
    expect(testCard.find('.test-badge').text()).toBe('插件虚拟摄像头')
    expect(testCard.find('.btn-config').exists()).toBe(false)
    expect(testCard.find('.btn-del').exists()).toBe(false)
    // 但普通卡有配置/删除
    const rtspCard = wrapper.findAll('.cam-card')[0]
    expect(rtspCard.find('.btn-config').exists()).toBe(true)
    expect(rtspCard.find('.btn-del').exists()).toBe(true)
  })

  it('空列表显示空状态引导', async () => {
    global.fetch = makeFetch({ cameras: [] })
    const wrapper = await mountView()
    expect(wrapper.find('.empty-state').exists()).toBe(true)
  })

  it('点添加打开新建弹窗，关闭后回到列表态', async () => {
    global.fetch = makeFetch({ cameras: CAMERAS, areas: [] })
    const wrapper = await mountView()

    expect(wrapper.find('.cam-modal-overlay').exists()).toBe(false)
    await wrapper.find('.btn-add').trigger('click')
    expect(wrapper.find('.cam-modal-overlay').exists()).toBe(true)

    await wrapper.find('.modal-close').trigger('click')
    expect(wrapper.find('.cam-modal-overlay').exists()).toBe(false)
  })

  it('点配置进入编辑态并加载该摄像头的关注项', async () => {
    const f = makeFetch({ cameras: CAMERAS, areas: [] })
    global.fetch = f
    const wrapper = await mountView()

    await wrapper.findAll('.cam-card')[0].find('.btn-config').trigger('click')
    expect(wrapper.find('.cam-modal-overlay').exists()).toBe(true)
    expect(f.mock.calls.some(c => String(c[0]) === '/api/cameras/a/focuses')).toBe(true)
    // 编辑态提供分区保存按钮（基础信息等）
    expect(wrapper.findAll('.btn-section-save').length).toBeGreaterThan(0)
  })

  it('启停开关乐观切换并 PUT enabled，失败回滚', async () => {
    let cameras = CAMERAS.map(c => ({ ...c }))
    const f = vi.fn(url => {
      if (url === '/api/cameras') {
        // 每次 PUT 后会重拉列表
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: cameras }) })
      }
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
    })
    global.fetch = f
    const wrapper = await mountView()

    // 第一张卡的第一个开关是"启用"toggle
    await wrapper.findAll('.cam-card')[0].findAll('.base-toggle')[0].trigger('click')
    await flushPromises()

    const putCall = f.mock.calls.find(c => c[0] === '/api/cameras/a' && c[1]?.method === 'PUT')
    expect(putCall).toBeTruthy()
    expect(JSON.parse(putCall[1].body)).toEqual({ enabled: 0 })
  })

  it('AI 预览单例开关走 display 端点', async () => {
    const f = makeFetch({ cameras: CAMERAS, areas: [] })
    global.fetch = f
    const wrapper = await mountView()

    // 第二个 toggle 是 display_enabled（当前 0 → 点击后 enable）
    await wrapper.findAll('.cam-card')[0].findAll('.base-toggle')[1].trigger('click')
    await flushPromises()

    expect(f.mock.calls.some(c => c[0] === '/api/cameras/a/display/enable')).toBe(true)
  })

  it('删除需 confirm 确认后调 DELETE', async () => {
    const f = makeFetch({ cameras: [CAMERAS[0]], areas: [] })
    global.fetch = f
    const wrapper = await mountView()

    await wrapper.find('.btn-del').trigger('click')

    expect(confirm).toHaveBeenCalledWith(expect.stringContaining('删除该摄像头'))
    const del = f.mock.calls.find(c => c[1]?.method === 'DELETE')
    expect(del[0]).toBe('/api/cameras/a')
  })

  it('confirm 取消时不发删除请求', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false))
    const f = makeFetch({ cameras: [CAMERAS[0]], areas: [] })
    global.fetch = f
    const wrapper = await mountView()

    await wrapper.find('.btn-del').trigger('click')
    expect(f.mock.calls.some(c => c[1]?.method === 'DELETE')).toBe(false)
  })

  it('新建弹窗底部提供创建按钮：整卡字段 POST /api/cameras 后关弹窗', async () => {
    const f = makeFetch({ cameras: [], areas: [] })
    global.fetch = f
    const wrapper = await mountView()

    await wrapper.find('.btn-add').trigger('click')
    // 分区头不再有创建入口(分区保存仅编辑态显示)
    expect(wrapper.findAll('.btn-section-save').length).toBe(0)
    // 底部主按钮:文案"创建"
    const btn = wrapper.find('.btn-modal-create')
    expect(btn.exists()).toBe(true)
    expect(btn.text()).toBe('创建')

    await wrapper.find('input[placeholder="如:客厅、门口"]').setValue('car01')
    await wrapper.find('input[placeholder^="rtsp://"]').setValue('http://192.168.4.48:8080/live/car01stream.flv')
    await btn.trigger('click')
    await flushPromises()

    const post = f.mock.calls.find(c => c[0] === '/api/cameras' && c[1]?.method === 'POST')
    expect(post).toBeTruthy()
    const body = JSON.parse(post[1].body)
    expect(body.name).toBe('car01')
    expect(body.source_type).toBe('rtsp')
    expect(body.rtsp_url).toBe('http://192.168.4.48:8080/live/car01stream.flv')
    // 创建成功后关闭弹窗回到列表态
    expect(wrapper.find('.cam-modal-overlay').exists()).toBe(false)
  })

  it('新建空名称点底部创建被拦截且不发请求', async () => {
    const f = makeFetch({ cameras: [], areas: [] })
    global.fetch = f
    const wrapper = await mountView()

    await wrapper.find('.btn-add').trigger('click')
    await wrapper.find('.btn-modal-create').trigger('click')

    expect(alert).toHaveBeenCalledWith(expect.stringContaining('请填写摄像头名称'))
    expect(f.mock.calls.some(c => c[1]?.method === 'POST')).toBe(false)
  })

  it('编辑态试连按钮调 test-stream 并展示结果', async () => {
    const f = vi.fn(url => {
      if (url === '/api/cameras') return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [CAMERAS[0]] }) })
      if (url === '/api/ha/areas') return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
      if (String(url) === '/api/cameras/a/test-stream') {
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: { ok: true } }) })
      }
      if (String(url).includes('/focuses')) return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: {} }) })
    })
    global.fetch = f
    const wrapper = await mountView()

    await wrapper.findAll('.cam-card')[0].find('.btn-config').trigger('click')
    await wrapper.find('.btn-test').trigger('click')
    await flushPromises()

    expect(f.mock.calls.some(c => c[0] === '/api/cameras/a/test-stream')).toBe(true)
    expect(wrapper.find('.test-ok').exists()).toBe(true)
  })
})
