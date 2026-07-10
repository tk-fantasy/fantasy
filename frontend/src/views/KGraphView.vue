<template>
  <div class="app">
    <div class="loading-overlay" v-if="loading">
      <div class="loading-text">加载语义图...</div>
    </div>
    <Graph3D ref="graphRef" :graphData="graphData" :highlightNodes="highlightNodes" :highlightLinks="highlightLinks" @node-click="onNodeClick" />
    <SearchPanel @search-results="onSearchResults" @focus-node="handleFocusNode" />
    <NodeDetail :node="selectedNode" :links="graphData.links" @close="onNodeClick(null)" @focus-node="handleFocusNode" />
    <button class="back-btn" @click="$router.back()">&#8592; 返回</button>
    <div class="status-bar">{{ graphData.nodes.length }} 节点 &#183; {{ graphData.links.length }} 关联</div>
    <div class="link-control">
      <span class="link-label">连线数</span>
      <input class="link-input" type="number" min="10" max="1000" step="10" v-model.number="maxLinks" @change="onMaxLinksChange" />
      <button class="link-reload" @click="reloadGraph">重载</button>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import Graph3D from '../components/Graph3D.vue'
import SearchPanel from '../components/SearchPanel.vue'
import NodeDetail from '../components/NodeDetail.vue'
import { useGraph } from '../composables/useGraph.js'

const graphRef = ref(null)
const { graphData, loading, selectedNode, highlightNodes, highlightLinks, loadGraph, onNodeClick, onFocusNode } = useGraph()

const maxLinks = ref(parseInt(localStorage.getItem('sg_max_links'), 10) || 150)

function onMaxLinksChange() {
  if (maxLinks.value && maxLinks.value >= 10) {
    localStorage.setItem('sg_max_links', String(maxLinks.value))
  }
}
function reloadGraph() {
  onMaxLinksChange()
  loadGraph()
}
function handleFocusNode(id) { onFocusNode(id); graphRef.value?.focusNode(id) }
function onSearchResults(ids) { highlightNodes.value = new Set(ids) }

onMounted(() => loadGraph())
</script>

<style scoped>
.app { position: absolute; inset: 0; background: var(--color-bg); }
.app::after {
  content: '';
  position: absolute; inset: 0; z-index: 0; pointer-events: none;
  background:
    radial-gradient(ellipse 120% 60% at 30% 100%, rgba(74, 124, 112, 0.2) 0%, transparent 60%),
    radial-gradient(ellipse 100% 50% at 70% 100%, rgba(232, 168, 124, 0.12) 0%, transparent 55%),
    radial-gradient(ellipse 80% 40% at 50% 100%, rgba(183, 128, 176, 0.1) 0%, transparent 50%);
  animation: kgGlow 8s ease-in-out infinite;
}
.light-mode .app::after {
  background:
    radial-gradient(circle at 50% 50%, rgba(255, 200, 100, 0.25) 0%, rgba(255, 180, 80, 0.12) 20%, rgba(255, 160, 60, 0.04) 40%, transparent 55%);
  background-size: 250% 250%;
  will-change: background-position;
  animation: sunOrbit 24s cubic-bezier(0.45, 0, 0.55, 1) infinite;
}
@keyframes sunOrbit {
  0%   { background-position: 10% 10%; }
  25%  { background-position: 85% 15%; }
  50%  { background-position: 75% 85%; }
  75%  { background-position: 15% 75%; }
  100% { background-position: 10% 10%; }
}
@keyframes kgGlow { 0%, 100% { opacity: 0.5; } 50% { opacity: 1; } }
.loading-overlay { position: fixed; inset: 0; z-index: 100; display: flex; align-items: center; justify-content: center; background: rgba(0,0,0,0.9); }
.light-mode .loading-overlay { background: rgba(255,255,255,0.9); }
.loading-text { font-size: var(--text-xl); color: var(--color-text-secondary); animation: pulse 1.5s ease-in-out infinite; }
@keyframes pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 1; } }
.back-btn { position: absolute; top: 16px; right: 16px; z-index: 20; background: rgba(0,0,0,0.5); backdrop-filter: blur(8px); border: 1px solid var(--color-border); color: var(--color-text-secondary); padding: 8px 16px; border-radius: var(--radius-sm); cursor: pointer; font-size: var(--text-sm); }
.light-mode .back-btn { background: rgba(255,255,255,0.7); }
.back-btn:hover { color: var(--color-text); border-color: var(--color-border-hover); }
.status-bar { position: absolute; bottom: 16px; left: 50%; transform: translateX(-50%); z-index: 10; font-size: var(--text-xs); color: var(--color-text-tertiary); background: rgba(0,0,0,0.5); padding: 6px 16px; border-radius: var(--radius-full); backdrop-filter: blur(8px); }
.light-mode .status-bar { background: rgba(255,255,255,0.7); }
.link-control { position: absolute; bottom: 16px; right: 16px; z-index: 10; display: flex; align-items: center; gap: 6px; background: rgba(0,0,0,0.5); backdrop-filter: blur(8px); padding: 6px 12px; border-radius: var(--radius-full); }
.light-mode .link-control { background: rgba(255,255,255,0.7); }
.link-label { font-size: var(--text-xs); color: var(--color-text-tertiary); }
.link-input { width: 60px; background: transparent; border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 2px 6px; color: var(--color-text); font-size: var(--text-xs); font-variant-numeric: tabular-nums; outline: none; }
.link-input:focus { border-color: var(--color-primary); }
.link-reload { background: var(--color-primary-light); border: 1px solid var(--color-border); border-radius: var(--radius-sm); padding: 2px 10px; color: var(--color-primary); font-size: var(--text-xs); cursor: pointer; }
.link-reload:hover { background: var(--color-primary-hover); }
</style>
