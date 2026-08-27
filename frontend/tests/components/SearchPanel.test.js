import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import SearchPanel from '../../src/components/SearchPanel.vue'

global.fetch = vi.fn()

describe('SearchPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.warn = vi.fn()
  })

  it('空查询回车：清空结果并广播空集合，不发请求', async () => {
    const wrapper = mount(SearchPanel)
    await wrapper.find('input').setValue('   ')
    await wrapper.find('input').trigger('keydown.enter')

    expect(fetch).not.toHaveBeenCalled()
    expect(wrapper.emitted('search-results')?.at(-1)).toEqual([[]])
  })

  it('搜索成功：请求带编码后的关键词，emit 命中节点 id 列表', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({
        results: [
          { id: 'doc1', title: '网关手册', score: 0.87, category: '设备' },
          { id: 'doc2', title: '配件', score: 0.5, category: '其他' },
        ],
      }),
    })
    const wrapper = mount(SearchPanel)
    await wrapper.find('input').setValue('网关 手册')
    await wrapper.find('input').trigger('keydown.enter')
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith(
      `/api/sg/search?q=${encodeURIComponent('网关 手册')}&top_k=10`,
      expect.anything()
    )
    const items = wrapper.findAll('.result-item')
    expect(items.length).toBe(2)
    expect(items[0].text()).toContain('网关手册')
    expect(items[0].text()).toContain('87.0%') // 分数按百分比一位小数
    expect(wrapper.emitted('search-results')?.at(-1)).toEqual([['doc1', 'doc2']])
  })

  it('点结果项 emit focus-node 并清空结果', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ results: [{ id: 'doc9', title: 'X', score: 1, category: '' }] }),
    })
    const wrapper = mount(SearchPanel)
    await wrapper.find('input').setValue('x')
    await wrapper.find('input').trigger('keydown.enter')
    await flushPromises()

    await wrapper.find('.result-item').trigger('click')
    expect(wrapper.emitted('focus-node')?.at(-1)).toEqual(['doc9'])
    expect(wrapper.findAll('.result-item').length).toBe(0)
  })
})
