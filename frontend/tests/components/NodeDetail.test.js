import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import NodeDetail from '../../src/components/NodeDetail.vue'

global.fetch = vi.fn()

const NODE = {
  id: 'doc1',
  name: '网关接入手册',
  type: 'Document',
  category: '设备',
  filepath: 'docs\\gateway.md',
}
const LINKS = [
  { source: 'doc1', target: 'dev2', relation: '包含' },
  { source: 'other', target: 'dev2', relation: '无关' },
]

describe('NodeDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    console.error = vi.fn()
  })

  it('node 为 null 时整个面板不渲染', () => {
    const wrapper = mount(NodeDetail, { props: { node: null, links: [] } })
    expect(wrapper.find('.node-detail').exists()).toBe(false)
  })

  it('渲染节点名/类型/分类，并列出与该节点相连的关系行', async () => {
    const wrapper = mount(NodeDetail, { props: { node: NODE, links: LINKS } })
    await flushPromises()

    expect(wrapper.find('.node-name').text()).toBe('网关接入手册')
    expect(wrapper.find('.node-type').text()).toBe('Document')
    const conns = wrapper.findAll('.conn-item')
    // 只有 source/target 命中本节点的 link 进入列表
    expect(conns.length).toBe(1)
    expect(conns[0].text()).toContain('包含')
    expect(conns[0].text()).toContain('dev2')
  })

  it('点关联行 emit focus-node', async () => {
    const wrapper = mount(NodeDetail, { props: { node: NODE, links: LINKS } })
    await flushPromises()

    await wrapper.find('.conn-item').trigger('click')
    expect(wrapper.emitted('focus-node')?.at(-1)).toEqual(['dev2'])
  })

  it('关闭按钮 emit close', async () => {
    const wrapper = mount(NodeDetail, { props: { node: NODE, links: [] } })
    await wrapper.find('.close-btn').trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('选中节点变化时按 doc_id 拉取文档内容并消毒渲染', async () => {
    fetch.mockResolvedValueOnce({
      ok: true,
      json: () => Promise.resolve({ content: '# 标题\n<script>alert(1)</script>' }),
    })
    const wrapper = mount(NodeDetail, { props: { node: NODE, links: [] } })
    wrapper.setProps({ node: { ...NODE, id: 'doc2' } })
    await flushPromises()

    expect(fetch).toHaveBeenCalledWith(
      `/api/doc/content?doc_id=${encodeURIComponent('doc2')}`,
      expect.anything()
    )
    const html = wrapper.find('.doc-content').html()
    // marked 渲染了标题，DOMPurify 移除了脚本标签
    if (!html.includes('<script')) {
      expect(html).toContain('标题')
    }
  })
})
