import { ref } from 'vue'
import { apiGet } from '../utils/api'
import { getNodeColor } from '../utils/nodeColors.js'

export function useGraph() {
  const graphData = ref({ nodes: [], links: [] })
  const loading = ref(false)
  const selectedNode = ref(null)
  const highlightNodes = ref(new Set())
  const highlightLinks = ref(new Set())

  async function loadGraph(url = '/api/sg/latest') {
    loading.value = true
    try {
      // apiGet 已解包 ApiResponse.data；无产物时 data=null，apiGet 回退返回整个 json
      const result = await apiGet(url)
      const graph = result?.graph
      if (graph && graph.nodes?.length) {
        // 按 weight 降序截断，避免连线过密；上限从 localStorage 读，默认 150
        const maxLinks = parseInt(localStorage.getItem('sg_max_links'), 10) || 150
        const links = graph.links || []
        if (links.length > maxLinks) {
          links.sort((a, b) => (b.weight || 0) - (a.weight || 0))
          graph.links = links.slice(0, maxLinks)
        }
        graphData.value = graph
      } else {
        graphData.value = {
          nodes: [{ id: 'placeholder', name: '请先构建语义图', type: 'Document', val: 3 }],
          links: [],
        }
      }
    } catch (e) {
      console.warn('Failed to load graph:', e)
      graphData.value = {
        nodes: [{ id: 'placeholder', name: '请先构建语义图', type: 'Document', val: 3 }],
        links: [],
      }
    } finally {
      loading.value = false
    }
  }

  function onNodeClick(node) {
    selectedNode.value = node
    if (!node) {
      highlightNodes.value = new Set()
      highlightLinks.value = new Set()
      return
    }
    const connected = new Set([node.id])
    const linked = new Set()
    for (const link of graphData.value.links) {
      const srcId = typeof link.source === 'object' ? link.source.id : link.source
      const tgtId = typeof link.target === 'object' ? link.target.id : link.target
      if (srcId === node.id) {
        connected.add(tgtId)
        linked.add(link)
      }
      if (tgtId === node.id) {
        connected.add(srcId)
        linked.add(link)
      }
    }
    highlightNodes.value = connected
    highlightLinks.value = linked
  }

  function onFocusNode(id) {
    const node = graphData.value.nodes.find(n => n.id === id)
    if (node) {
      onNodeClick(node)
    }
  }

  function getColor(node) {
    return getNodeColor(node.type)
  }

  return {
    graphData, loading, selectedNode,
    highlightNodes, highlightLinks,
    loadGraph, onNodeClick, onFocusNode, getColor,
  }
}
