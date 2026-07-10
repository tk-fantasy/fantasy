<template>
  <transition name="slide">
    <div v-if="node" class="node-detail" :style="{ width: panelWidth + 'px' }">
      <div class="resize-handle" @mousedown="startResize"></div>
      <div class="sticky-header">
        <button class="close-btn" @click="$emit('close')">&#x2715;</button>
        <div class="node-type" :style="{ color: nodeColor }">{{ node.type }}</div>
        <h2 class="node-name">{{ node.name }}</h2>
        <div class="tabs">
          <button :class="{ active: activeTab === 'info' }" @click="activeTab = 'info'">信息</button>
          <button :class="{ active: activeTab === 'doc' }" @click="activeTab = 'doc'">文档</button>
        </div>
      </div>
      <div v-show="activeTab === 'info'" class="tab-content">
        <div class="node-meta">
          <div class="meta-item" v-if="node.category"><span class="label">分类</span><span class="value">{{ node.category }}</span></div>
          <div class="meta-item" v-if="node.subcategory"><span class="label">子分类</span><span class="value">{{ node.subcategory }}</span></div>
          <div class="meta-item" v-if="filepathAbs"><span class="label">文件</span><a class="value filepath" :href="filepathAbs" target="_blank">{{ node.filepath }}</a></div>
        </div>
        <div class="connections" v-if="connections.length">
          <div class="section-title">关联 ({{ connections.length }})</div>
          <div class="conn-item" v-for="conn in connections" :key="conn.id" @click="goToNode(conn.id)">
            <span class="conn-relation">{{ conn.relation }}</span>
            <span class="conn-target">&#8594; {{ conn.target }}</span>
          </div>
        </div>
      </div>
      <div v-show="activeTab === 'doc'" class="tab-content doc-content">
        <div v-if="contentLoading" class="loading-hint">加载中...</div>
        <div v-else-if="contentError" class="error-hint">{{ contentError }}</div>
        <div v-else class="markdown-body" v-html="mdHtml"></div>
      </div>
    </div>
  </transition>
</template>

<script setup>
import { ref, computed, watch } from 'vue'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { getNodeColor } from '../utils/nodeColors.js'

const props = defineProps({ node: { type: Object, default: null }, links: { type: Array, default: () => [] } })
const emit = defineEmits(['close', 'focus-node'])

const MIN_WIDTH = 300, MAX_WIDTH = 800, DEFAULT_WIDTH = 360
const activeTab = ref('info'), mdHtml = ref(''), contentLoading = ref(false), contentError = ref(''), panelWidth = ref(DEFAULT_WIDTH)

const nodeColor = computed(() => getNodeColor(props.node?.type))
const filepathAbs = computed(() => props.node?.filepath ? `/docs/${props.node.filepath.replace(/\\/g, '/')}` : '')
const connections = computed(() => {
  if (!props.node || !props.links.length) return []
  const result = []
  for (const link of props.links) {
    const s = typeof link.source === 'object' ? link.source.id : link.source
    const t = typeof link.target === 'object' ? link.target.id : link.target
    if (s === props.node.id) result.push({ relation: link.relation || '关联', target: t, id: t })
    else if (t === props.node.id) result.push({ relation: link.relation || '关联', target: s, id: s })
  }
  return result
})

function goToNode(id) { emit('focus-node', id) }
function startResize(e) { e.preventDefault(); const sx = e.clientX, sw = panelWidth.value; const m = ev => { panelWidth.value = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, sw + (sx - ev.clientX))) }; const u = () => { window.removeEventListener('mousemove', m); window.removeEventListener('mouseup', u) }; window.addEventListener('mousemove', m); window.addEventListener('mouseup', u) }

watch(() => props.node?.id, (nid) => { mdHtml.value = ''; contentError.value = ''; if (!nid) return; activeTab.value = 'info'; fetchContent(nid) })

async function fetchContent(id) {
  contentLoading.value = true; contentError.value = ''
  try {
    const res = await fetch(`/doc/content?doc_id=${encodeURIComponent(id)}`, {
      signal: AbortSignal.timeout(10000),
      headers: { 'Content-Type': 'application/json' },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    mdHtml.value = data.content ? DOMPurify.sanitize(marked.parse(data.content)) : ''
  } catch (e) { contentError.value = '无法加载文档' }
  finally { contentLoading.value = false }
}
</script>

<style scoped>
.node-detail { position: absolute; top: 16px; right: 16px; z-index: 10; max-height: calc(100vh - 32px); overflow-y: auto; background: rgba(0,0,0,0.85); -webkit-backdrop-filter: blur(16px); backdrop-filter: blur(16px); border: 1px solid var(--color-border); border-radius: var(--radius-lg); }
.light-mode .node-detail { background: rgba(255,255,255,0.9); }
.resize-handle { position: absolute; left: 0; top: 0; bottom: 0; width: 5px; cursor: ew-resize; z-index: 5; }
.resize-handle:hover { background: var(--color-primary); opacity: 0.5; }
.sticky-header { position: sticky; top: 0; z-index: 2; background: rgba(0,0,0,0.9); padding: 16px 20px 8px; border-bottom: 1px solid var(--color-border); border-radius: var(--radius-lg) var(--radius-lg) 0 0; }
.light-mode .sticky-header { background: rgba(255,255,255,0.95); }
.close-btn { position: absolute; top: 12px; right: 12px; background: none; border: none; color: var(--color-text-tertiary); font-size: 16px; cursor: pointer; padding: 4px 8px; border-radius: 4px; }
.close-btn:hover { background: var(--color-surface); color: var(--color-text); }
.node-type { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 1.5px; margin-bottom: 4px; }
.node-name { font-size: var(--text-xl); font-weight: var(--weight-semibold); margin-bottom: 8px; word-break: break-word; padding-right: 24px; color: var(--color-text); }
.tabs { display: flex; gap: 4px; }
.tabs button { flex: 1; padding: 6px 12px; font-size: var(--text-sm); background: none; border: none; color: var(--color-text-tertiary); cursor: pointer; border-bottom: 2px solid transparent; }
.tabs button.active { color: var(--color-primary); border-bottom-color: var(--color-primary); }
.tabs button:hover { color: var(--color-text-secondary); }
.tab-content { font-size: var(--text-sm); padding: 12px 20px 20px; }
.node-meta { display: flex; flex-direction: column; gap: 6px; margin-bottom: 12px; padding-bottom: 12px; border-bottom: 1px solid var(--color-border); }
.meta-item { display: flex; gap: 8px; font-size: var(--text-sm); }
.meta-item .label { color: var(--color-text-tertiary); flex-shrink: 0; }
.meta-item .value { color: var(--color-text-secondary); }
.meta-item .filepath { font-size: var(--text-xs); word-break: break-all; font-family: var(--font-mono); color: var(--color-primary); text-decoration: none; }
.meta-item .filepath:hover { text-decoration: underline; }
.section-title { font-size: var(--text-sm); color: var(--color-text-tertiary); text-transform: uppercase; letter-spacing: 1px; margin-bottom: 8px; }
.connections { display: flex; flex-direction: column; gap: 4px; }
.conn-item { display: flex; gap: 6px; font-size: var(--text-sm); padding: 6px 8px; margin: 0 -8px; border-radius: var(--radius-sm); cursor: pointer; }
.conn-item:hover { background: var(--color-surface-active); }
.conn-relation { color: var(--color-primary); flex-shrink: 0; font-size: var(--text-xs); background: var(--color-primary-light); padding: 1px 6px; border-radius: 4px; }
.conn-target { color: var(--color-text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.doc-content { min-height: 100px; }
.loading-hint, .error-hint { text-align: center; padding: 32px 16px; color: var(--color-text-tertiary); font-size: var(--text-sm); }
.error-hint { color: var(--color-danger); }
.markdown-body { color: var(--color-text-secondary); font-size: var(--text-base); line-height: 1.7; }
.markdown-body :deep(h1) { font-size: var(--text-2xl); margin: 0 0 12px; color: var(--color-text); border-bottom: 1px solid var(--color-border); padding-bottom: 8px; }
.markdown-body :deep(h2) { font-size: var(--text-xl); margin: 20px 0 10px; color: var(--color-text); }
.markdown-body :deep(h3) { font-size: var(--text-lg); margin: 16px 0 8px; color: var(--color-text-secondary); }
.markdown-body :deep(p) { margin: 8px 0; }
.markdown-body :deep(code) { background: var(--color-surface); padding: 2px 6px; border-radius: 4px; font-size: var(--text-sm); font-family: var(--font-mono); }
.markdown-body :deep(pre) { background: rgba(0,0,0,0.3); padding: 12px; border-radius: var(--radius-sm); overflow-x: auto; border: 1px solid var(--color-border); margin: 12px 0; }
.markdown-body :deep(pre code) { background: none; padding: 0; }
.markdown-body :deep(ul), .markdown-body :deep(ol) { padding-left: 20px; margin: 8px 0; }
.markdown-body :deep(li) { margin: 4px 0; }
.markdown-body :deep(a) { color: var(--color-primary); text-decoration: none; }
.markdown-body :deep(a:hover) { text-decoration: underline; }
.markdown-body :deep(blockquote) { border-left: 3px solid var(--color-primary); margin: 12px 0; padding: 8px 16px; background: var(--color-primary-light); border-radius: 0 var(--radius-sm) var(--radius-sm) 0; }
.markdown-body :deep(table) { width: 100%; border-collapse: collapse; margin: 12px 0; }
.markdown-body :deep(th), .markdown-body :deep(td) { padding: 8px 12px; border: 1px solid var(--color-border); text-align: left; }
.markdown-body :deep(th) { background: var(--color-surface); font-weight: 600; }
.markdown-body :deep(hr) { border: none; border-top: 1px solid var(--color-border); margin: 16px 0; }
.slide-enter-active, .slide-leave-active { transition: all 0.3s ease; }
.slide-enter-from, .slide-leave-to { transform: translateX(20px); opacity: 0; }
</style>
