<script setup>
// 家庭报告页 — 事件时间线（告警/任务/自动化）+ 家庭周报。
// 数据来自 family_events 表（告警服务与各 hook 点写入）。
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '../utils/api'

const events = ref([])
const loading = ref(true)
const report = ref(null)
const reportLoading = ref(false)
const generating = ref(false)
const genMsg = ref('')
const days = ref(7)
const kindFilter = ref('')

const KIND_LABELS = {
  alert: '告警', alert_resolved: '恢复', task_success: '任务成功',
  task_failed: '任务失败', automation: '自动化', weekly_report: '周报',
  plugin: '插件',
}

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

function kindClass(kind) {
  if (kind === 'alert') return 'k-alert'
  if (kind === 'task_failed') return 'k-fail'
  if (kind === 'alert_resolved') return 'k-ok'
  return 'k-info'
}

onMounted(() => { loadEvents(); loadReport() })
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>家庭报告</h1>
      <p class="page-sub">这一周家里发生了什么 —— 告警、定时任务、自动化触发</p>
    </header>

    <section class="report-card">
      <div class="report-head">
        <h2>📋 家庭周报</h2>
        <button class="gen-btn" :disabled="generating" @click="generateReport">
          {{ generating ? '生成中…' : '立即生成' }}
        </button>
      </div>
      <p v-if="genMsg" class="gen-msg">{{ genMsg }}</p>
      <p v-if="reportLoading">加载中…</p>
      <template v-else-if="report">
        <p class="report-time">{{ fmtTime(report.generated_at) }}</p>
        <p class="report-text">{{ report.text }}</p>
      </template>
      <p v-else class="muted">还没有周报（每周日晚自动生成，可在 config 开启 weekly_report.enabled）</p>
    </section>

    <section class="events-card">
      <div class="events-head">
        <h2>🕘 事件时间线</h2>
        <span class="filters">
          <select v-model="days" @change="loadEvents" class="sel">
            <option :value="1">今天</option>
            <option :value="7">近 7 天</option>
            <option :value="30">近 30 天</option>
          </select>
          <select v-model="kindFilter" @change="loadEvents" class="sel">
            <option value="">全部</option>
            <option value="alert">告警</option>
            <option value="task">定时任务</option>
            <option value="automation">自动化</option>
            <option value="weekly_report">周报</option>
          </select>
        </span>
      </div>
      <p v-if="loading">加载中…</p>
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
.page { max-width: 760px; margin: 0 auto; padding: 20px 16px; }
.page-header { margin-bottom: 16px; }
.page-header h1 { font-size: 22px; margin: 0 0 4px; color: var(--text, #222); }
.page-sub { font-size: 13px; color: #888; margin: 0; }
.report-card, .events-card { background: var(--bg-soft, #fff); border: 1px solid var(--border, #e5e5e5); border-radius: 14px; padding: 16px; margin-bottom: 16px; }
.report-head, .events-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.report-head h2, .events-head h2 { font-size: 15px; margin: 0; color: var(--text, #333); }
.gen-btn { padding: 6px 14px; border: none; border-radius: 8px; background: var(--accent, #4a90d9); color: #fff; font-size: 12px; cursor: pointer; }
.gen-btn:disabled { opacity: .5; }
.gen-msg { font-size: 12px; color: #666; }
.report-time { font-size: 11px; color: #999; margin: 4px 0; }
.report-text { font-size: 14px; line-height: 1.7; white-space: pre-wrap; color: var(--text, #333); }
.muted { font-size: 13px; color: #999; }
.filters { display: flex; gap: 6px; }
.sel { padding: 4px 8px; border-radius: 8px; border: 1px solid var(--border, #ddd); background: var(--bg, #fff); color: var(--text, #333); font-size: 12px; }
.event-list { list-style: none; margin: 0; padding: 0; max-height: 480px; overflow-y: auto; }
.event-item { display: flex; align-items: baseline; gap: 10px; padding: 7px 2px; border-bottom: 1px solid var(--border, #f0f0f0); font-size: 13px; }
.event-kind { flex-shrink: 0; font-size: 11px; padding: 2px 8px; border-radius: 999px; }
.k-alert { background: #fdecea; color: #c62828; }
.k-fail { background: #fff3e0; color: #ef6c00; }
.k-ok { background: #e8f5e9; color: #2e7d32; }
.k-info { background: #e3f2fd; color: #1565c0; }
.event-msg { flex: 1; color: var(--text, #333); }
.event-time { flex-shrink: 0; font-size: 11px; color: #aaa; }
</style>
