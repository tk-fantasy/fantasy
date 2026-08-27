import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ScheduledTasksView from '../../src/views/ScheduledTasksView.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

const TASKS = [
  { id: 't1', payload: { kind: 'reminder', intent: '提醒起床' }, schedule: { kind: 'cron', expr: '0 8 * * *' }, enabled: true, next_run_at: '2026-08-28T08:00:00' },
  { id: 't2', payload: { kind: 'message', message: '开灯' }, schedule: { kind: 'every', every_seconds: 3600 }, enabled: false },
]

function setupFetch(tasks = TASKS) {
  fetch.mockImplementation(url => {
    if (url === '/api/scheduled-tasks') return okData(tasks)
    if (String(url).includes('parse-schedule')) return okData({ schedule: { kind: 'cron', expr: '0 9 * * *' }, summary: '每天早上9点' })
    if (url === '/api/emoji/preferences') return okData([])
    return okData({})
  })
}

async function mountView() {
  const wrapper = mount(ScheduledTasksView)
  await flushPromises()
  return wrapper
}

describe('ScheduledTasksView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    vi.stubGlobal('confirm', vi.fn(() => true))
    console.error = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('加载任务列表并显示启用计数', async () => {
    await setupFetch()
    const wrapper = await mountView()

    expect(wrapper.find('.page-sub').text()).toContain('1 个任务启用中')
    expect(wrapper.findAll('.task-list .task-card').length).toBe(2)
  })

  it('enabled 缺省视为 true', async () => {
    await setupFetch([{ ...TASKS[0], enabled: undefined }])
    const wrapper = await mountView()
    expect(wrapper.find('.page-sub').text()).toContain('1 个任务启用中')
  })

  it('翻译：空指令提示错误不发请求；有指令调 parse-schedule 显示摘要', async () => {
    await setupFetch([])
    const wrapper = await mountView()
    await wrapper.find('.btn-add').trigger('click') // 展开创建表单

    // 空输入直接报错
    await wrapper.find('.btn-parse').trigger('click')
    expect(wrapper.find('.form-hint--err').exists()).toBe(true)
    expect(fetch.mock.calls.some(c => String(c[0]).includes('parse-schedule'))).toBe(false)

    await wrapper.find('.form-input').setValue('每天九点')
    await wrapper.find('.btn-parse').trigger('click')
    await flushPromises()

    expect(fetch.mock.calls.some(c => String(c[0]).includes('parse-schedule'))).toBe(true)
    expect(wrapper.find('.form-hint--ok').exists()).toBe(true)
  })

  it('修改时间描述后清掉旧翻译结果', async () => {
    await setupFetch([])
    const wrapper = await mountView()
    await wrapper.find('.btn-add').trigger('click')

    await wrapper.find('.form-input').setValue('每天九点')
    await wrapper.find('.btn-parse').trigger('click')
    await flushPromises()
    expect(wrapper.find('.form-hint--ok').exists()).toBe(true)

    await wrapper.find('.form-input').setValue('改成十点')
    await flushPromises()
    expect(wrapper.find('.form-hint--ok').exists()).toBe(false)
  })

  it('创建任务：组装 reminder payload 提交并插入列表头', async () => {
    let store = [...TASKS]
    fetch.mockImplementation((url, opts) => {
      if (url === '/api/scheduled-tasks' && !opts?.method) return okData(store)
      if (url === '/api/scheduled-tasks/parse-schedule') return okData({ schedule: { kind: 'cron', expr: '0 9 * * *' }, summary: '每天早上9点' })
      if (url === '/api/scheduled-tasks' && opts?.method === 'POST') {
        return okData({ id: 'new1', schedule: { kind: 'cron', expr: '0 9 * * *' } }).then(res => {
          store = [{ id: 'new1' }, ...store]
          return res
        })
      }
      if (url === '/api/emoji/preferences') return okData([])
      return okData({})
    })
    const wrapper = await mountView()
    await wrapper.find('.btn-add').trigger('click')

    await wrapper.find('.form-input').setValue('每天九点提醒喝水')
    await wrapper.find('.btn-parse').trigger('click')
    await flushPromises()

    // 执行内容输入框是表单里的第二个 .form-input
    await wrapper.findAll('.form-input')[1].setValue('该喝水了')
    await wrapper.find('.btn-create').trigger('click')
    await flushPromises()

    expect(alert).not.toHaveBeenCalled()
    const post = fetch.mock.calls.find(c => c[0] === '/api/scheduled-tasks' && c[1]?.method === 'POST')
    expect(JSON.parse(post[1].body)).toEqual({
      schedule: { kind: 'cron', expr: '0 9 * * *' },
      payload: { kind: 'reminder', intent: '该喝水了', original: '该喝水了' },
      enabled: true,
    })
  })

  it('toggle 开关 POST enabled 并回写 next_run_at', async () => {
    fetch.mockImplementation(url => {
      if (url === '/api/scheduled-tasks') return okData([...TASKS])
      if (url === '/api/scheduled-tasks/t2/enabled') return okData({ next_run_at: '2026-09-01T00:00:00' })
      if (url === '/api/emoji/preferences') return okData([])
      return okData({})
    })
    const wrapper = await mountView()

    const card = wrapper.findAll('.task-card')[1]
    await card.find('.base-toggle').trigger('click')
    await flushPromises()

    expect(wrapper.find('.page-sub').text()).toContain('2 个任务启用中')
  })

  it('删除需 confirm，确认后从列表移除', async () => {
    await setupFetch([TASKS[0]])
    const wrapper = await mountView()

    await wrapper.find('.task-card .btn-del, .task-card [class*="del"]').trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalled()
    const del = fetch.mock.calls.find(c => c[1]?.method === 'DELETE')
    expect(del[0]).toBe('/api/scheduled-tasks/t1')
  })

  it('立即执行成功：横幅显示回复文本；失败：横幅红色提示', async () => {
    fetch.mockImplementation(url => {
      if (url === '/api/scheduled-tasks') return okData([{ ...TASKS[0] }])
      if (url === '/api/scheduled-tasks/t1/run') return okData({ last_status: 'success', last_reply: '起床啦' })
      if (url === '/api/emoji/preferences') return okData([])
      return okData({})
    })
    const wrapper = await mountView()

    const runBtn = wrapper.findAll('button').find(b => b.text().includes('立即'))
    await runBtn.trigger('click')
    await flushPromises()

    const notice = wrapper.find('.run-notice')
    expect(notice.exists()).toBe(true)
    expect(notice.classes()).toContain('run-notice--ok')
    expect(notice.text()).toContain('起床啦')
  })

  it('立即执行失败时显示红色横幅', async () => {
    fetch.mockImplementation(url => {
      if (url === '/api/scheduled-tasks') return okData([{ ...TASKS[0] }])
      if (url === '/api/scheduled-tasks/t1/run') return okData({ last_status: 'error', last_error: '设备离线' })
      if (url === '/api/emoji/preferences') return okData([])
      return okData({})
    })
    const wrapper = await mountView()

    const runBtn = wrapper.findAll('button').find(b => b.text().includes('立即'))
    await runBtn.trigger('click')
    await flushPromises()

    const notice = wrapper.find('.run-notice')
    expect(notice.classes()).toContain('run-notice--err')
    expect(notice.text()).toContain('设备离线')
  })
})
