<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

const loading = ref(true)
const metrics = ref({
  http: { total: 0, errors: 0, avg_latency_s: 0, p95_latency_s: 0, latency_samples: 0 },
  tools: { calls: {}, errors: {} },
  llm: { calls: 0, errors: 0 },
  automation: { evals: 0 },
})

let refreshTimer = null

async function loadMetrics() {
  try {
    const res = await fetch('/api/metrics')
    if (res.ok) {
      const data = await res.json()
      if (data.code === 'ok') {
        metrics.value = data.data
      }
    }
  } catch (e) {
    console.error('Failed to load metrics:', e)
  } finally {
    loading.value = false
  }
}

function formatLatency(seconds) {
  if (seconds === 0) return '-'
  if (seconds < 0.001) return `${(seconds * 1000000).toFixed(0)}μs`
  if (seconds < 1) return `${(seconds * 1000).toFixed(1)}ms`
  return `${seconds.toFixed(2)}s`
}

function getToolNames() {
  return Object.keys(metrics.value.tools.calls || {})
}

function getToolCalls(name) {
  return metrics.value.tools.calls?.[name] || 0
}

function getToolErrors(name) {
  return metrics.value.tools.errors?.[name] || 0
}

onMounted(() => {
  loadMetrics()
  refreshTimer = setInterval(loadMetrics, 5000)
})

onUnmounted(() => {
  if (refreshTimer) {
    clearInterval(refreshTimer)
  }
})
</script>

<template>
  <div class="monitor-page">
    <div class="page-header">
      <h1>系统监控</h1>
      <span class="refresh-hint">每 5 秒自动刷新</span>
    </div>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="metrics-grid">
      <!-- HTTP 请求统计 -->
      <div class="metric-card">
        <div class="card-title">HTTP 请求</div>
        <div class="card-value">{{ metrics.http.total }}</div>
        <div class="card-detail">
          <span>错误: {{ metrics.http.errors }}</span>
          <span>样本: {{ metrics.http.latency_samples }}</span>
        </div>
      </div>

      <!-- 平均延迟 -->
      <div class="metric-card">
        <div class="card-title">平均延迟</div>
        <div class="card-value">{{ formatLatency(metrics.http.avg_latency_s) }}</div>
        <div class="card-detail">最近 {{ metrics.http.latency_samples }} 次请求</div>
      </div>

      <!-- P95 延迟 -->
      <div class="metric-card">
        <div class="card-title">P95 延迟</div>
        <div class="card-value">{{ formatLatency(metrics.http.p95_latency_s) }}</div>
        <div class="card-detail">95% 请求延迟</div>
      </div>

      <!-- LLM 调用 -->
      <div class="metric-card">
        <div class="card-title">LLM 调用</div>
        <div class="card-value">{{ metrics.llm.calls }}</div>
        <div class="card-detail">
          <span>错误: {{ metrics.llm.errors }}</span>
        </div>
      </div>

      <!-- 自动化评估 -->
      <div class="metric-card">
        <div class="card-title">自动化评估</div>
        <div class="card-value">{{ metrics.automation.evals }}</div>
        <div class="card-detail">规则评估次数</div>
      </div>

      <!-- 工具调用总数 -->
      <div class="metric-card">
        <div class="card-title">工具调用</div>
        <div class="card-value">{{ Object.values(metrics.tools.calls).reduce((a, b) => a + b, 0) }}</div>
        <div class="card-detail">
          <span>工具种类: {{ getToolNames().length }}</span>
        </div>
      </div>
    </div>

    <!-- 工具调用明细 -->
    <div v-if="getToolNames().length > 0" class="tools-section">
      <h2>工具调用明细</h2>
      <div class="tools-table">
        <div class="table-header">
          <span class="col-name">工具名称</span>
          <span class="col-calls">调用次数</span>
          <span class="col-errors">错误次数</span>
        </div>
        <div v-for="name in getToolNames()" :key="name" class="table-row">
          <span class="col-name tool-name">{{ name }}</span>
          <span class="col-calls">{{ getToolCalls(name) }}</span>
          <span class="col-errors" :class="{ 'has-error': getToolErrors(name) > 0 }">
            {{ getToolErrors(name) }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.monitor-page {
  padding: 2rem;
  max-width: 1200px;
  margin: 0 auto;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 2rem;
}

.page-header h1 {
  font-size: 1.5rem;
  color: var(--color-text);
  margin: 0;
}

.refresh-hint {
  font-size: 0.85rem;
  color: var(--color-text-secondary);
  opacity: 0.7;
}

.loading-state {
  text-align: center;
  padding: 3rem;
  color: var(--color-text-secondary);
}

.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 1.5rem;
  margin-bottom: 2rem;
}

.metric-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 1.5rem;
  transition: all 0.2s ease;
}

.metric-card:hover {
  border-color: var(--color-primary);
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
}

.card-title {
  font-size: 0.85rem;
  color: var(--color-text-secondary);
  margin-bottom: 0.5rem;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.card-value {
  font-size: 2rem;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 0.5rem;
}

.card-detail {
  font-size: 0.8rem;
  color: var(--color-text-secondary);
  display: flex;
  gap: 1rem;
}

.tools-section {
  margin-top: 2rem;
}

.tools-section h2 {
  font-size: 1.2rem;
  color: var(--color-text);
  margin-bottom: 1rem;
}

.tools-table {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow: hidden;
}

.table-header {
  display: grid;
  grid-template-columns: 1fr 120px 120px;
  padding: 1rem 1.5rem;
  background: var(--color-surface-hover);
  font-weight: 600;
  font-size: 0.85rem;
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.table-row {
  display: grid;
  grid-template-columns: 1fr 120px 120px;
  padding: 1rem 1.5rem;
  border-top: 1px solid var(--color-border);
  transition: background 0.15s ease;
}

.table-row:hover {
  background: var(--color-surface-hover);
}

.col-name {
  font-family: 'JetBrains Mono', monospace;
  font-size: 0.9rem;
}

.tool-name {
  color: var(--color-primary);
}

.col-calls,
.col-errors {
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.has-error {
  color: var(--color-danger);
  font-weight: 600;
}

@media (max-width: 768px) {
  .monitor-page {
    padding: 1rem;
  }

  .metrics-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 1rem;
  }

  .card-value {
    font-size: 1.5rem;
  }

  .table-header,
  .table-row {
    grid-template-columns: 1fr 80px 80px;
    padding: 0.75rem 1rem;
  }
}
</style>
