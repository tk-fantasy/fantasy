import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import DeviceSelectModal from '../../src/components/DeviceSelectModal.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

function errRes(status, message) {
  return Promise.resolve({ ok: false, status, json: () => Promise.resolve({ message }) })
}

const CANDIDATES = [
  { entity_id: 'light.a', label: '床头灯', domain: 'light', area_name: '卧室', state: 'off' },
  { entity_id: 'light.b', label: '客厅吊灯', domain: 'light', area_name: '客厅', state: 'on' },
  { entity_id: 'light.c', label: '厨房灯', domain: 'light', area_name: '厨房', state: 'off' },
]

function mountModal(props = {}) {
  return mount(DeviceSelectModal, {
    props: {
      pendingId: 'sel-1', sessionId: 's1', query: '开灯',
      service: 'turn_on', candidates: CANDIDATES, ...props,
    },
    global: { stubs: { teleport: true } },
  })
}

function checkboxes(wrapper) {
  return wrapper.findAll('input[type="checkbox"]')
}

function checkedIds(wrapper) {
  return checkboxes(wrapper)
    .filter((b) => b.element.checked)
    .map((b) => b.attributes('data-eid'))
}

function buttonByText(wrapper, text) {
  return wrapper.findAll('button').find((b) => b.text().includes(text))
}

describe('DeviceSelectModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    fetch.mockImplementation(() => okData({ entity_ids: [], names: [] }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染全部候选：展示名 + 区域 + 当前状态', () => {
    const w = mountModal()
    const text = w.text()
    expect(text).toContain('床头灯')
    expect(text).toContain('客厅吊灯')
    expect(text).toContain('厨房灯')
    expect(text).toContain('卧室')
    expect(checkboxes(w).length).toBe(3)
    expect(w.findAll('.cand-state').map((n) => n.text())).toEqual(['off', 'on', 'off'])
  })

  it('歧义文案带上用户原话与候选数', () => {
    const w = mountModal()
    expect(w.text()).toContain('你要操作哪个设备？')
    expect(w.find('.notice').text()).toContain('「开灯」匹配到 3 个设备')
  })

  it('category_miss：标题必须如实说没找到，不能假装设备存在', () => {
    const w = mountModal({ reason: 'category_miss', query: '月球的灯' })
    expect(w.text()).toContain('没找到「月球的灯」')
    expect(w.find('.notice').text()).toContain('没有这个名字的设备')
  })

  it('turn_on 预勾当前关着的灯（已亮的不必再开一遍）', () => {
    const w = mountModal({ service: 'turn_on' })
    expect(checkedIds(w)).toEqual(['light.a', 'light.c'])
    expect(w.text()).toContain('已选 2 / 3')
  })

  it('turn_off 预勾当前亮着的灯', () => {
    const w = mountModal({ service: 'turn_off' })
    expect(checkedIds(w)).toEqual(['light.b'])
  })

  it('无法预判的服务（调光等）默认全勾', () => {
    const w = mountModal({ service: 'brightness_set' })
    expect(checkedIds(w)).toEqual(['light.a', 'light.b', 'light.c'])
  })

  it('状态不可信（unavailable）时按「会变」处理，预勾', () => {
    const w = mountModal({
      service: 'turn_on',
      candidates: [{ entity_id: 'light.x', label: '走廊灯', state: 'unavailable' }],
    })
    expect(checkedIds(w)).toEqual(['light.x'])
  })

  it('全选 / 清空来回切换', async () => {
    const w = mountModal({ service: 'turn_on' })
    const toggle = buttonByText(w, '全选')
    await toggle.trigger('click')
    expect(checkedIds(w).length).toBe(3)
    await buttonByText(w, '清空').trigger('click')
    expect(checkedIds(w)).toEqual([])
  })

  it('确认只提交勾选项，打到 select 端点并 emit confirmed', async () => {
    fetch.mockImplementation(() => okData({ entity_ids: ['light.a'], names: ['床头灯'] }))
    const w = mountModal({ service: 'turn_on' })
    await buttonByText(w, '执行').trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.length).toBe(1)
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/ha/pending/sel-1/select')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ session_id: 's1', entity_ids: ['light.a', 'light.c'] })
    expect(w.emitted('confirmed')[0][0]).toEqual({ entity_ids: ['light.a'], names: ['床头灯'] })
  })

  it('一个都没勾时确认按钮禁用，不发请求', async () => {
    const w = mountModal({ service: 'turn_on' })
    await buttonByText(w, '全选').trigger('click')   // 先全选
    await buttonByText(w, '清空').trigger('click')   // 再清空
    const confirm = buttonByText(w, '执行')
    expect(confirm.attributes('disabled')).toBeDefined()
    await confirm.trigger('click')
    await flushPromises()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('草稿过期（404）→ emit expired，不在弹窗内报错', async () => {
    fetch.mockImplementation(() => errRes(404, '待选设备不存在或已过期，请重新说一遍指令'))
    const w = mountModal()
    await buttonByText(w, '执行').trigger('click')
    await flushPromises()
    expect(w.emitted('expired')[0][0]).toContain('已过期')
    expect(w.find('.action-error').exists()).toBe(false)
  })

  it('非 404 失败留在弹窗内（用户还能改选），不 emit', async () => {
    fetch.mockImplementation(() => errRes(403, '设备「厨房灯」被用户设为禁止 AI 操作'))
    const w = mountModal()
    await buttonByText(w, '执行').trigger('click')
    await flushPromises()
    expect(w.emitted('confirmed')).toBeUndefined()
    expect(w.emitted('expired')).toBeUndefined()
    expect(w.find('.action-error').text()).toContain('禁止 AI 操作')
  })

  it('取消打 cancel 端点并 emit cancelled', async () => {
    const w = mountModal()
    await buttonByText(w, '取消').trigger('click')
    await flushPromises()
    const [url, init] = fetch.mock.calls[0]
    expect(url).toBe('/api/ha/pending/sel-1/cancel')
    expect(JSON.parse(init.body)).toEqual({ session_id: 's1' })
    expect(w.emitted('cancelled')).toBeTruthy()
  })

  it('点关闭只 emit close，不作废后端草稿（TTL 内仍可在聊天里说设备名）', async () => {
    const w = mountModal()
    await w.find('.modal-close').trigger('click')
    await flushPromises()
    expect(w.emitted('close')).toBeTruthy()
    expect(fetch).not.toHaveBeenCalled()
  })

  it('候选为空时不炸，确认按钮禁用', () => {
    const w = mountModal({ candidates: [] })
    expect(checkboxes(w).length).toBe(0)
    expect(buttonByText(w, '执行').attributes('disabled')).toBeDefined()
  })
})
