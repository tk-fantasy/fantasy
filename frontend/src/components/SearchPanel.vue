<template>
  <div class="search-panel">
    <div class="search-box">
      <input v-model="query" type="text" placeholder="语义搜索文档..." @keydown.enter="doSearch" />
      <button class="search-btn" @click="doSearch" :disabled="searching">
        <span v-if="searching" class="spinner"></span>
        <span v-else>&#x1F50D;</span>
      </button>
    </div>
    <div class="search-status" v-if="statusMsg">{{ statusMsg }}</div>
    <div class="search-results" v-if="results.length">
      <div class="result-item" v-for="r in results" :key="r.id" @click="focusNode(r.id)">
        <span class="result-title">{{ r.title || r.id }}</span>
        <span class="result-score">{{ (r.score * 100).toFixed(1) }}%</span>
        <span class="result-cat">{{ r.category }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'

const emit = defineEmits(['search-results', 'focus-node'])
const query = ref(''), results = ref([]), searching = ref(false), statusMsg = ref('')

async function doSearch() {
  const q = query.value.trim()
  if (!q) { results.value = []; statusMsg.value = ''; emit('search-results', []); return }
  results.value = []; searching.value = true; statusMsg.value = '搜索中...'
  try {
    const res = await fetch(`/api/sg/search?q=${encodeURIComponent(q)}&top_k=10`, {
      signal: AbortSignal.timeout(15000),
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
    })
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const data = await res.json()
    results.value = data.results || []; statusMsg.value = results.value.length ? '' : '未找到匹配结果'
    emit('search-results', (data.results || []).map(r => r.id))
  } catch (e) {
    statusMsg.value = e.name === 'TimeoutError' ? '无法连接搜索服务' : '搜索出错：' + (e.message || '未知错误')
    results.value = []
  } finally { searching.value = false }
}

function focusNode(id) { emit('focus-node', id); emit('search-results', []); results.value = []; statusMsg.value = '' }
</script>

<style scoped>
.search-panel { position: absolute; top: 16px; left: 16px; z-index: 10; display: flex; flex-direction: column; gap: 8px; }
.search-box { display: flex; gap: 6px; }
.search-box input { flex: 1; padding: 10px 14px; border: 1px solid var(--color-border); border-radius: var(--radius-sm); background: rgba(0,0,0,0.6); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); color: var(--color-text); font-size: var(--text-base); outline: none; }
.light-mode .search-box input { background: rgba(255,255,255,0.8); }
.search-box input:focus { border-color: var(--color-primary); }
.search-box input::placeholder { color: var(--color-text-muted); }
.search-btn { display: flex; align-items: center; justify-content: center; min-width: 40px; padding: 6px 12px; border: 1px solid var(--color-border); border-radius: var(--radius-sm); background: var(--color-primary-light); color: var(--color-primary); cursor: pointer; font-size: 14px; }
.search-btn:hover:not(:disabled) { background: var(--color-primary-hover); }
.search-btn:disabled { opacity: 0.5; cursor: not-allowed; }
.spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid rgba(74,124,112,0.3); border-top-color: var(--color-primary); border-radius: 50%; animation: spin 0.6s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.search-status { font-size: var(--text-sm); color: var(--color-text-secondary); text-align: center; padding: 4px 8px; }
.search-results { background: rgba(0,0,0,0.7); -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px); border: 1px solid var(--color-border); border-radius: var(--radius-sm); max-height: 360px; overflow-y: auto; }
.light-mode .search-results { background: rgba(255,255,255,0.85); }
.result-item { display: flex; gap: 8px; align-items: center; padding: 8px 12px; cursor: pointer; border-bottom: 1px solid var(--color-border); font-size: var(--text-sm); }
.result-item:hover { background: var(--color-surface-active); }
.result-title { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--color-text); }
.result-score { color: var(--color-primary); font-size: var(--text-xs); flex-shrink: 0; }
.result-cat { color: var(--color-text-tertiary); font-size: var(--text-xs); flex-shrink: 0; }
</style>
