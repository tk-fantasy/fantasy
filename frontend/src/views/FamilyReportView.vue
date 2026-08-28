<script setup>
// 家庭报告页 — 事件时间线（告警/任务/自动化）+ 家庭周报。
// 数据来自 family_events 表（告警服务与各 hook 点写入）。
// 样式全部走全局设计 token + FlowSelect，与站内其他页面同一视觉语言。
import { ref, onMounted } from 'vue'
import FlowSelect from '../components/FlowSelect.vue'
import { apiGet, apiPost } from '../utils/api'

const events = ref([])
const loading = ref(true)
const report = ref(null)
const reportLoading = ref(false)
const generating = ref(false)
const genMsg = ref('')
// FlowSelect 的 value 是字符串；days 直接拼 URL，无需转数字
const days = ref('7')
const kindFilter = ref('')

const dayOptions = [
  { value: '1', label: '今天' },
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
]
const kindOptions = [
  { value: '', label: '全部类型' },
  { value: 'alert', label: '告警' },
  { value: 'task', label: '定时任务' },
  { value: 'automation', label: '自动化' },
  { value: 'weekly_report', label: '周报' },
]

async function loadEvents() {
  loading.value = true
  try {
    const data = await apiGet(`/api/events?days=${days.value}`)
    events.value = (data || []).filter(e => !kindFilter.value || e.kind.startsWith(kindFilter.value))
  } catch (e) {
    console.error('加载事件失败', e)
  } finally {
    loading.value = false
  }
}

async function loadReport() {
  reportLoading.value = true
  try {
    report.value = await apiGet('/api/report/weekly')
  } catch (e) {
    report.value = null
  } finally {
    reportLoading.value = false
  }
}

async function generateReport() {
  generating.value = true
  genMsg.value = ''
  try {
    const r = await apiPost('/api/report/weekly/generate', {})
    genMsg.value = r?.generated ? '已生成' : (r?.reason === 'no_events' ? '近 7 天没有事件，无事可报' : '生成失败')
    if (r?.generated) await loadReport()
  } catch (e) {
    genMsg.value = '生成失败：' + (e.message || e)
  } finally {
    generating.value = false
  }
}

function fmtTime(ms) {
  return new Date(ms).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

// 语义色映射：告警红 / 失败黄 / 恢复绿 / 其余信息蓝（对应全局语义 token）
function kindClass(kind) {
  if (kind === 'alert') return 'k-danger'
  if (kind === 'task_failed') return 'k-warning'
  if (kind === 'alert_resolved') return 'k-success'
  return 'k-info'
}

// 事件类型 → 中文标签（script setup 常量，模板可直接用）
const KIND_LABELS = {
  alert: '告警', alert_resolved: '恢复', task_success: '任务成功',
  task_failed: '任务失败', automation: '自动化', weekly_report: '周报',
  plugin: '插件',
}

onMounted(() => { loadEvents(); loadReport() })
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>家庭报告</h1>
      <p class="page-sub">这一周家里发生了什么 —— 告警、定时任务、自动化触发</p>
    </header>

    <section class="report-card setting-card">
      <div class="report-head">
        <h2>📋 家庭周报</h2>
        <button class="btn-primary gen-btn" :disabled="generating" @click="generateReport">
          {{ generating ? '生成中…' : '立即生成' }}
        </button>
      </div>
      <p v-if="genMsg" class="gen-msg">{{ genMsg }}</p>
      <p v-if="reportLoading" class="loading-state">加载中…</p>
      <template v-else-if="report">
        <p class="report-time">{{ fmtTime(report.generated_at) }}</p>
        <p class="report-text">{{ report.text }}</p>
      </template>
      <p v-else class="muted">还没有周报（每周日晚自动生成，可在 config 开启 weekly_report.enabled）</p>
    </section>

    <section class="events-card setting-card">
      <div class="events-head">
        <h2>🕘 事件时间线</h2>
        <span class="filters">
          <FlowSelect v-model="days" :options="dayOptions" width="108px" @change="loadEvents" />
          <FlowSelect v-model="kindFilter" :options="kindOptions" width="118px" @change="loadEvents" />
        </span>
      </div>
      <p v-if="loading" class="loading-state">加载中…</p>
      <p v-else-if="!events.length" class="muted">这段时间家里很平静，没有记录 🍃</p>
      <ul v-else class="event-list">
        <li v-for="e in events" :key="e.id" class="event-item">
          <span class="event-kind" :class="kindClass(e.kind)">{{ KIND_LABELS[e.kind] || e.kind }}</span>
          <span class="event-msg">{{ e.message }}</span>
          <span class="event-time">{{ fmtTime(e.created_at) }}</span>
        </li>
      </ul>
    </section>
  </div>
</template>

<style scoped>
.report-card,
.events-card {
  padding: var(--space-16);
  margin-bottom: var(--space-16);
}

.report-head,
.events-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-8);
  gap: var(--space-10);
}

.report-head h2,
.events-head h2 {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  margin: 0;
  color: var(--color-text);
}

.gen-btn {
  font-size: var(--text-xs);
  padding: var(--space-4) var(--space-14);
}

.gen-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.gen-msg {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  margin: var(--space-2) 0;
}

.report-time {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin: var(--space-2) 0;
}

.report-text {
  font-size: var(--text-base);
  line-height: var(--leading-relaxed);
  white-space: pre-wrap;
  color: var(--color-text);
}

.muted {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}

.filters {
  display: flex;
  gap: var(--space-2);
  align-items: center;
}

.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 480px;
  overflow-y: auto;
}

.event-item {
  display: flex;
  align-items: baseline;
  gap: var(--space-10);
  padding: var(--space-3) var(--space-2);
  border-bottom: 1px solid var(--color-border);
  font-size: var(--text-sm);
}

.event-item:last-child {
  border-bottom: none;
}

.event-kind {
  flex-shrink: 0;
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-8);
  border-radius: var(--radius-full);
}

.k-danger { background: var(--color-danger-bg); color: var(--color-danger); }
.k-warning { background: var(--color-warning-bg); color: var(--color-warning); }
.k-success { background: var(--color-success-bg); color: var(--color-success); }
.k-info { background: var(--color-info-bg); color: var(--color-info); }

.event-msg {
  flex: 1;
  color: var(--color-text-secondary);
}

.event-time {
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

@media (max-width: 768px) {
  .events-head {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
