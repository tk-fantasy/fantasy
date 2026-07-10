import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import TaskView from '../../src/views/TaskView.vue'

// Mock fetch
global.fetch = vi.fn(() =>
  Promise.resolve({
    ok: true,
    json: () => Promise.resolve({
      data: [
        { id: '1', name: '人来开灯', condition: '检测到人', enabled: true, actions: [] },
        { id: '2', name: '人走关灯', condition: '无人', enabled: false, actions: [] }
      ]
    })
  })
)

describe('TaskView', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders task page', () => {
    const wrapper = mount(TaskView)
    expect(wrapper.find('.page').exists()).toBe(true)
  })

  it('renders page header', () => {
    const wrapper = mount(TaskView)
    expect(wrapper.find('.page-header h1').text()).toBe('自动化规则')
  })

  it('loads rules on mount', async () => {
    mount(TaskView)
    await vi.dynamicImportSettled()
    expect(global.fetch).toHaveBeenCalledWith('/api/rules', { credentials: 'include' })
  })

  it('renders create form toggle', () => {
    const wrapper = mount(TaskView)
    expect(wrapper.text()).toContain('新建规则')
  })
})
