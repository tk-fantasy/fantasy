import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import AdvancedView from '../../src/views/AdvancedView.vue'

// Mock fetch：所有接口返回空 data，页面应正常渲染默认值
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ data: {} }),
  })
)

describe('AdvancedView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  // 回归：6eacab9 删 PTZ 时误删了相邻的 saveAutomation 块，
  // 模板仍引用未定义变量，渲染即 ReferenceError 白屏
  it('加载完成后渲染全部 7 张配置卡片且无渲染错误', async () => {
    const errors = []
    const wrapper = mount(AdvancedView, {
      global: { config: { errorHandler: err => errors.push(err) } },
    })
    await flushPromises()

    const titles = wrapper.findAll('.config-title').map(n => n.text())
    expect(errors).toEqual([])
    expect(titles).toEqual([
      '天气 API',
      '网页搜索（Exa）',
      '摄像头参数',
      'Home Assistant',
      '助手角色',
      'API Keys',
      '自动化',
    ])
  })
})
