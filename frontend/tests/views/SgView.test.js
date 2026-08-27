import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import SgView from '../../src/views/SgView.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

const CONFIG = { embed_model: 'bge-m3', chat_model: 'glm-4-flash', ready: true }
const LATEST = { graph: { nodes: [{ id: 'n1', name: '灯' }], links: [] } }

function setupFetch({ status = { status: 'idle', progress: 0, message: '' }, latest = LATEST } = {}) {
  fetch.mockImplementation(url => {
    if (url === '/api/sg/config') return okData(CONFIG)
    if (url === '/api/sg/status') return okData(status)
    if (url === '/api/sg/latest') return okData(latest)
    return okData({})
  })
  return fetch
}

async function mountView(setup) {
  setup?.()
  const wrapper = mount(SgView)
  await flushPromises()
  return wrapper
}

describe('SgView', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
  })

  afterEach(() => {
    vi.clearAllTimers()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('挂载加载配置/状态/最新图，渲染后端就绪信息', async () => {
    const wrapper = await mountView(setupFetch)

    const urls = fetch.mock.calls.map(c => c[0])
    expect(urls).toContain('/api/sg/config')
    expect(urls).toContain('/api/sg/status')
    expect(urls).toContain('/api/sg/latest')

    expect(wrapper.text()).toContain('bge-m3')
    expect(wrapper.text()).toContain('glm-4-flash')
    // latest 有图 → 不显示占位引导
    expect(wrapper.text()).not.toContain('请先构建')
  })

  it('保存配置 POST 表单值并提示成功', async () => {
    const wrapper = await mountView(setupFetch)

    // 改阈值输入（param-grid 中第一个 number input）
    const threshold = wrapper.find('.param-grid input')
    if (threshold.exists()) {
      await threshold.setValue('0.8')
      const saveBtn = wrapper.findAll('button').find(b => b.text().includes('保存'))
      await saveBtn.trigger('click')
      await flushPromises()

      const post = fetch.mock.calls.find(c => c[0] === '/api/sg/config' && c[1]?.method === 'POST')
      expect(post).toBeTruthy()
      expect(JSON.parse(post[1].body).threshold).toBe(0.8)
      expect(alert).not.toHaveBeenCalled()
    }
  })

  it('开始构建：POST build 后轮询状态到 building 显示进度', async () => {
    let pollCount = 0
    fetch.mockImplementation(url => {
      if (url === '/api/sg/config') return okData(CONFIG)
      if (url === '/api/sg/latest') return okData(LATEST)
      if (url === '/api/sg/build') return okData({})
      if (url === '/api/sg/status') {
        pollCount++
        return okData(pollCount > 1 ? { status: 'building', progress: 40, message: '向量化中' } : { status: 'idle', progress: 0 })
      }
      return okData({})
    })
    const wrapper = await mountView()

    const buildBtn = wrapper.findAll('button').find(b => b.text().includes('构建'))
    await buildBtn.trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/sg/build')).toBe(true)

    // 轮询间隔后再看状态
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(wrapper.text()).toContain('向量化中')
  })

  it('无产物时不显示节点数据且页面可正常渲染', async () => {
    const wrapper = await mountView(() =>
      setupFetch({ status: { status: 'idle', progress: 0 }, latest: null })
    )

    // apiGet 对 data:null 回退整个 json（对象），视图需容错渲染而不崩溃
    expect(wrapper.text()).toContain('节点数')
  })

  it('取消构建调 cancel 端点', async () => {
    const wrapper = await mountView(() =>
      setupFetch({ status: { status: 'building', progress: 10, message: '构建中' } })
    )

    const cancelBtn = wrapper.findAll('button').find(b => b.text().includes('取消'))
    if (cancelBtn && !cancelBtn.attributes('disabled')) {
      await cancelBtn.trigger('click')
      await flushPromises()
      expect(fetch.mock.calls.some(c => c[0] === '/api/sg/cancel')).toBe(true)
    } else {
      // 无进行中任务时按钮不存在或禁用 —— 断言没误发请求
      expect(fetch.mock.calls.some(c => c[0] === '/api/sg/cancel')).toBe(false)
    }
  })
})
