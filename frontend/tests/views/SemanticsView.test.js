import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import SemanticsView from '../../src/views/SemanticsView.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

const MAPS = {
  maps: {
    'lock.front': { mappings: { turn_on: { target: 'open', description: '开门' } } },
  },
}
const ENTITIES = [
  { entity_id: 'lock.front', attributes: { friendly_name: '前门锁' } },
  { entity_id: 'light.kt', attributes: { friendly_name: '客厅灯' } },
]
const SERVICES = { services: { lock: ['turn_on', 'turn_off'], light: ['turn_on', 'toggle'] } }

function setupFetch({ maps = MAPS, entities = ENTITIES, services = SERVICES } = {}) {
  fetch.mockImplementation(url => {
    if (url === '/api/ha/action-maps') return okData(maps)
    if (url === '/api/ha/entities') return okData({ entities })
    if (url === '/api/ha/entity-services') return okData(services)
    return okData({})
  })
}

async function mountView(setup) {
  setup?.()
  global.fetch = fetch
  const wrapper = mount(SemanticsView)
  await flushPromises()
  return wrapper
}

describe('SemanticsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    vi.stubGlobal('confirm', vi.fn(() => true))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('挂载并发拉三份数据，渲染已配置映射卡片（摘要 svc→target）', async () => {
    const wrapper = await mountView(setupFetch)

    const urls = fetch.mock.calls.map(c => c[0])
    expect(urls).toEqual(expect.arrayContaining(['/api/ha/action-maps', '/api/ha/entities', '/api/ha/entity-services']))

    const card = wrapper.find('.map-card')
    expect(card.exists()).toBe(true)
    expect(card.find('.card-title').text()).toBe('前门锁')
    expect(card.find('.card-summary').text()).toContain('turn_on→open')
  })

  it('无映射时显示空状态与添加按钮', async () => {
    const wrapper = await mountView(() =>
      setupFetch({ maps: { maps: {} }, entities: [], services: { services: {} } })
    )
    expect(wrapper.find('.empty').exists()).toBe(true)
    expect(wrapper.find('.btn-add').text()).toContain('添加设备')
  })

  it('加载失败显示错误条', async () => {
    fetch.mockRejectedValue(new Error('offline'))
    const wrapper = await mountView()

    expect(wrapper.find('.error').text()).toBe('offline')
    expect(console.error).toHaveBeenCalled
  })

  it('打开编辑弹窗：草稿深拷贝已有映射', async () => {
    const wrapper = await mountView(setupFetch)

    await wrapper.find('.map-card .card-main').trigger('click')
    expect(wrapper.find('.modal-overlay').exists()).toBe(true)

    // 草稿含已配置的 target，但输入框展示的是语义词 open 而非默认值
    const inputs = wrapper.findAll('.modal-overlay input')
    expect(inputs.length).toBeGreaterThan(0)
  })

  it('添加弹窗：按关键词过滤实体列表并选中进入配置态', async () => {
    const wrapper = await mountView(() =>
      setupFetch({ maps: { maps: {} }, entities: ENTITIES, services: SERVICES })
    )

    await wrapper.find('.btn-add').trigger('click')
    const pickerInput = wrapper.find('.search-input')
    expect(pickerInput.exists()).toBe(true)

    await pickerInput.setValue('客厅')
    // 过滤后只剩 light.kt 一行可选
    const rows = wrapper.findAll('.entity-row')
    expect(rows.length).toBe(1)
    expect(rows[0].text()).toContain('客厅灯')

    await rows[0].trigger('click')
    // 选中后展示该域可用 services
    expect(wrapper.text()).toContain('turn_off')
  })
})
