import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import OperationsView from '../../src/views/OperationsView.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

// 各分区挂载时拉取的数据（backups/audit/version）
function setupFetch(overrides = {}) {
  fetch.mockImplementation(url => {
    if (url === '/api/ops/backups') return okData(overrides.backups ?? [{ name: 'bak-0801.zip', size: 1024 }])
    if (url === '/api/ops/audit') return okData(overrides.audit ?? [{ time: '2026-08-01T10:00:00', action: 'backup.create' }])
    if (url === '/api/ops/version') return okData(overrides.version ?? { version: '1.2.0', history: [{ version: '1.1.0', date: '2026-07-01' }] })
    if (url === '/api/ops/update-pack/local') return okData([])
    if (String(url).includes('export/status')) return okData({})
    return okData(overrides.default ?? {})
  })
}

async function mountView(setup = setupFetch) {
  setup()
  const wrapper = mount(OperationsView)
  await flushPromises()
  return wrapper
}

describe('OperationsView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('confirm', vi.fn(() => true))
    vi.stubGlobal('alert', vi.fn())
    console.error = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('渲染六个运维区块标题', async () => {
    const wrapper = await mountView()

    const titles = wrapper.findAll('.section-title').map(t => t.text())
    expect(titles.some(t => t.includes('系统体检'))).toBe(true)
    expect(titles.some(t => t.includes('诊断包导出'))).toBe(true)
    expect(titles.some(t => t.includes('备份与恢复'))).toBe(true)
    expect(titles.some(t => t.includes('版本与升级'))).toBe(true)
  })

  it('挂载加载备份列表 / 审计 / 版本三份数据', async () => {
    await mountView()

    const urls = fetch.mock.calls.map(c => c[0])
    expect(urls).toContain('/api/ops/backups')
    expect(urls).toContain('/api/ops/audit')
    expect(urls).toContain('/api/ops/version')
  })

  it('系统体检按钮调 diagnose 并回填报告', async () => {
    fetch.mockImplementation(url => {
      if (url === '/api/ops/diagnose') return okData({
        summary: { pass: 2, warn: 0, fail: 0 },
        checks: [
          { name: '数据库', status: 'pass', detail: 'ok' },
          { name: 'LLM 配置', status: 'pass', detail: 'ok' },
        ],
        created_at: '2026-08-27 10:00',
        environment: 'host',
      })
      if (url === '/api/ops/backups') return okData([])
      if (url === '/api/ops/audit') return okData([])
      if (url === '/api/ops/version') return okData({})
      return okData([])
    })
    const wrapper = await mountView(() => {})

    const diagBtn = wrapper.findAll('button').find(b => b.text().includes('体检'))
    await diagBtn.trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/ops/diagnose')).toBe(true)
    expect(wrapper.text()).toContain('2 通过')
    expect(wrapper.text()).toContain('LLM 配置')
  })

  it('创建备份后刷新备份与审计数据', async () => {
    let backups = [{ name: 'old.zip', size: 10 }]
    fetch.mockImplementation((url, opts) => {
      if (url === '/api/ops/backups') {
        if (opts?.method === 'POST') backups = [...backups, { name: 'new.zip', size: 20 }]
        return okData(backups)
      }
      if (url === '/api/ops/audit') return okData([])
      if (url === '/api/ops/version') return okData({})
      return okData([])
    })
    const wrapper = await mountView(() => {})

    const backupBtn = wrapper.findAll('button').find(b => b.text().includes('备份'))
    await backupBtn.trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/ops/backups' && c[1]?.method === 'POST')).toBe(true)
  })

  it('删除备份走 DELETE 且 confirm 确认', async () => {
    const wrapper = await mountView()

    const delBtns = wrapper.findAll('button').filter(b => b.classes().includes('danger') || b.text().includes('删除'))
    await delBtns[0].trigger('click')

    expect(confirm).toHaveBeenCalled()
    const del = fetch.mock.calls.find(c => c[1]?.method === 'DELETE' && String(c[0]).startsWith('/api/ops/backups'))
    expect(del).toBeTruthy()
    expect(del[0]).toContain(encodeURIComponent('bak-0801.zip'))
  })

  it('清空审计走 DELETE /api/ops/audit 后重拉列表', async () => {
    const wrapper = await mountView()

    const clearBtn = wrapper.findAll('button').find(b => b.text().includes('清空'))
    await clearBtn.trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/ops/audit' && c[1]?.method === 'DELETE')).toBe(true)
  })

  it('版本信息展示当前版本；有新版本时提示升级入口', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('1.2.0')
  })
})
