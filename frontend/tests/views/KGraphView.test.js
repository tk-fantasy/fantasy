import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import KGraphView from '../../src/views/KGraphView.vue'

// Graph3D 依赖 WebGL（jsdom 无 GL 上下文），以带 focusNode 的桩替换
const Graph3DStub = { template: '<div class="graph3d-stub" />', methods: { focusNode() {} } }

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

function mountGraph(nodes = [], links = []) {
  fetch.mockImplementation(url =>
    url === '/api/sg/latest' ? okData({ graph: { nodes, links } }) : okData([])
  )
  return mount(KGraphView, {
    global: {
      stubs: {
        Graph3D: Graph3DStub,
        SearchPanel: true,
        NodeDetail: true,
      },
      mocks: { $router: { back: vi.fn() } },
    },
  })
}

describe('KGraphView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
  })

  it('加载后状态栏显示节点与关联数，loading 遮罩消失', async () => {
    const wrapper = mountGraph([{ id: 'a' }, { id: 'b' }], [{ source: 'a', target: 'b' }])
    await flushPromises()

    expect(wrapper.find('.status-bar').text()).toContain('2 节点')
    expect(wrapper.find('.status-bar').text()).toContain('1 关联')
    expect(wrapper.find('.loading-overlay').exists()).toBe(false)
  })

  it('连线数上限从 localStorage 读入输入框', async () => {
    localStorage.setItem('sg_max_links', '80')
    const wrapper = mountGraph()
    await flushPromises()

    const input = wrapper.find('.link-input')
    expect(Number(input.element.value)).toBe(80)
  })

  it('改连线数并 change：合法值持久化，非法(<10)不写', async () => {
    localStorage.setItem('sg_max_links', '150')
    const wrapper = mountGraph()
    await flushPromises()

    const input = wrapper.find('.link-input')
    input.setValue('300')
    await input.trigger('change')
    expect(localStorage.getItem('sg_max_links')).toBe('300')

    input.setValue('5')
    await input.trigger('change')
    expect(localStorage.getItem('sg_max_links')).toBe('300') // <10 不落盘
  })

  it('点重载重新拉图（fetch /api/sg/latest 再次调用）', async () => {
    const wrapper = mountGraph()
    await flushPromises()
    const callsBefore = fetch.mock.calls.filter(c => c[0] === '/api/sg/latest').length

    await wrapper.find('.link-reload').trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.filter(c => c[0] === '/api/sg/latest').length).toBe(callsBefore + 1)
  })

  it('返回按钮触发 $router.back()', async () => {
    const back = vi.fn()
    const wrapper = mount(KGraphView, {
      global: {
        stubs: { Graph3D: Graph3DStub, SearchPanel: true, NodeDetail: true },
        mocks: { $router: { back } },
      },
    })
    await flushPromises()

    await wrapper.find('.back-btn').trigger('click')
    expect(back).toHaveBeenCalledTimes(1)
  })
})
