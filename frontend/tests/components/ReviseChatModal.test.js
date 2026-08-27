import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import ReviseChatModal from '../../src/components/ReviseChatModal.vue'

global.fetch = vi.fn()

function okData(data) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve({ data }) })
}

const TASK = {
  id: 't1',
  name: '起床提醒',
  schedule: { kind: 'cron', expr: '0 8 * * *' },
  payload: { kind: 'reminder', intent: '提醒起床', original: '每天8点提醒起床' },
}

async function mountTask(initial = TASK) {
  const wrapper = mount(ReviseChatModal, {
    props: { kind: 'task', itemId: 't1', initial },
    global: { stubs: { teleport: true } },
  })
  await flushPromises()
  return wrapper
}

describe('ReviseChatModal', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('alert', vi.fn())
    console.error = vi.fn()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('plan 模式：展示任务类建议问题列表', async () => {
    const wrapper = await mountTask()
    expect(wrapper.text()).toContain('这个任务是执行一次还是每天重复？')
  })

  it('点建议问题即提问：POST explain 端点并展示回答', async () => {
    fetch.mockImplementation(url =>
      String(url).endsWith('/explain') ? okData({ answer: '这个任务每天早上8点执行一次。' }) : okData({})
    )
    const wrapper = await mountTask()

    const q = wrapper.findAll('button').find(b => b.text().includes('每天重复'))
    await q.trigger('click')
    // assistant 气泡稍后由 sendInstruction 异步更新
    await flushPromises()
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/scheduled-tasks/t1/explain')).toBe(true)
    expect(wrapper.text()).toContain('每天早上8点执行一次')
  })

  it('modify 模式：发送修改指令调 revise，成功后「应用」按钮可用', async () => {
    fetch.mockImplementation(url => {
      if (String(url).endsWith('/revise')) {
        return okData({
          task: { ...TASK, schedule: { kind: 'cron', expr: '0 9 * * *' } },
          summary: '改为9点',
        })
      }
      return okData({})
    })
    const wrapper = await mountTask()

    await wrapper.findAll('.mode-btn')[1].trigger('click') // 切到 modify

    // 组件手写比较 e.key === 'Enter'（大小写敏感），测试直接点发送按钮更稳
    await wrapper.find('.revise-input').setValue('改成9点')
    await wrapper.find('.btn-send').trigger('click')
    await flushPromises()
    await flushPromises()

    expect(fetch.mock.calls.some(c => c[0] === '/api/scheduled-tasks/t1/revise')).toBe(true)
    expect(wrapper.text()).toContain('改为9点')

    // 应用修改 → PUT 落库 + emit applied
    const applyBtn = wrapper.findAll('button').find(b => b.text().includes('应用修改'))
    expect(applyBtn).toBeTruthy()
    fetch.mockClear()
    fetch.mockImplementation(() => okData({ ...TASK, schedule: { kind: 'cron', expr: '0 9 * * *' } }))
    await applyBtn.trigger('click')
    await flushPromises()

    expect(fetch.mock.calls[0][0]).toBe('/api/scheduled-tasks/t1')
    expect(wrapper.emitted('applied')).toBeTruthy()
  })

  it('explain 失败时把错误写进助手气泡而非崩溃', async () => {
    fetch.mockRejectedValue(new Error('LLM 超时'))
    const wrapper = await mountTask()

    const q = wrapper.findAll('button').find(b => b.text().includes('每天重复'))
    await q.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(wrapper.text()).toMatch(/失败|超时|错误/)
  })
})
