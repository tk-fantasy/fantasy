import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useGraph } from '../../src/composables/useGraph'

global.fetch = vi.fn()

const PLACEHOLDER = '请先构建语义图'

function graphPayload(nodes, links) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ data: { graph: { nodes, links } } }),
  })
}

describe('useGraph', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    console.warn = vi.fn()
  })

  it('loadGraph 成功加载图数据', async () => {
    fetch.mockImplementationOnce(() =>
      graphPayload([{ id: 'a', name: '灯' }], [{ source: 'a', target: 'b', weight: 1 }])
    )
    const g = useGraph()
    await g.loadGraph()
    expect(g.graphData.value.nodes[0].id).toBe('a')
    expect(g.loading.value).toBe(false)
  })

  it('无产物时显示占位节点引导构建', async () => {
    fetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: null }) })
    const g1 = useGraph()
    await g1.loadGraph()
    expect(g1.graphData.value.nodes[0].name).toBe(PLACEHOLDER)

    // 空节点列表同样占位
    fetch.mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ data: { graph: { nodes: [], links: [] } } }) })
    const g2 = useGraph()
    await g2.loadGraph()
    expect(g2.graphData.value.nodes).toHaveLength(1)
    expect(g2.graphData.value.nodes[0].name).toBe(PLACEHOLDER)
  })

  it('请求异常时也占位且 loading 复位', async () => {
    fetch.mockRejectedValueOnce(new Error('offline'))
    const g = useGraph()
    await g.loadGraph()
    expect(g.graphData.value.nodes[0].name).toBe(PLACEHOLDER)
    expect(g.loading.value).toBe(false)
  })

  it('links 超上限时按 weight 降序截断（读 localStorage 配置）', async () => {
    localStorage.setItem('sg_max_links', '2')
    const links = Array.from({ length: 5 }, (_, i) => ({
      source: 'a', target: `n${i}`, weight: i,
    }))
    fetch.mockImplementationOnce(() =>
      graphPayload([{ id: 'a' }, { id: 'n0' }, { id: 'n1' }, { id: 'n2' }, { id: 'n3' }, { id: 'n4' }], links)
    )
    const g = useGraph()
    await g.loadGraph()
    expect(g.graphData.value.links).toHaveLength(2)
    // 截断保留 weight 最高的两条
    expect(g.graphData.value.links.map(l => l.weight)).toEqual([4, 3])
  })

  it('onNodeClick 计算邻接高亮；null 清空', async () => {
    fetch.mockImplementationOnce(() =>
      graphPayload(
        [{ id: 'a' }, { id: 'b' }, { id: 'c' }],
        [
          { source: 'a', target: 'b', weight: 2 },
          { source: { id: 'c' }, target: { id: 'a' }, weight: 1 }, // 已渲染后端点可能是对象
        ]
      )
    )
    const g = useGraph()
    await g.loadGraph()

    g.onNodeClick(g.graphData.value.nodes.find(n => n.id === 'a'))
    expect([...g.highlightNodes.value].sort()).toEqual(['a', 'b', 'c'])
    expect(g.highlightLinks.value.size).toBe(2)

    g.onNodeClick(null)
    expect(g.highlightNodes.value.size).toBe(0)
    expect(g.highlightLinks.value.size).toBe(0)
  })

  it('onFocusNode 按 id 定位节点并触发高亮', async () => {
    fetch.mockImplementationOnce(() =>
      graphPayload([{ id: 'x' }, { id: 'y' }], [{ source: 'x', target: 'y', weight: 1 }])
    )
    const g = useGraph()
    await g.loadGraph()

    g.onFocusNode('y')
    expect(g.selectedNode.value.id).toBe('y')

    g.onFocusNode('ghost') // 不存在 → 不改选中
    expect(g.selectedNode.value.id).toBe('y')
  })

  it('getColor 委托 nodeColors 映射，未知类型给缺省色', () => {
    const g = useGraph()
    expect(g.getColor({ type: 'Document' })).toBeDefined()
    expect(typeof g.getColor({ type: 'whatever-unknown-type' })).toBe('string')
  })
})
