<script setup>
// 家庭报告页 — 图表为主体的"家庭发生了什么"视图。
//
// 交互结构：
//   顶部工具行：天数切换（今天/近7天/近30天）+ 生成周报
//   总览：每日事件趋势柱状图（点击某天的柱 → 下钻）+ 数字卡 + 家庭周报
//   下钻视图：某天的事件时间线（面包屑返回；周报/数字卡不在此页）
//
// 数据：/api/events/stats（聚合图表）、/api/events?date=（某天时间线）、
// /api/report/weekly（周报）。样式走全局设计 token + FlowSelect。
import { ref, computed, onMounted } from 'vue'
import FlowSelect from '../components/FlowSelect.vue'
import EventCharts from '../components/EventCharts.vue'
import { apiGet, apiPost } from '../utils/api'

const report = ref(null)
const reportLoading = ref(false)
const generating = ref(false)
const genMsg = ref('')
// FlowSelect 的 value 是字符串；days 直接拼 URL，无需转数字
const days = ref('7')
const stats = ref(null)
const statsLoading = ref(true)

// 下钻状态：null=总览（图表主体）；'YYYY-MM-DD'=某天的时间线视图
const drillDay = ref(null)
// 下钻时间线本地状态
const dayEvents = ref([])
const dayLoading = ref(false)
const dayKindFilter = ref('')

const dayOptions = [
  { value: '1', label: '今天' },
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
]
// 下钻视图内的类型筛选（不含周报——周报不按天）
const dayKindOptions = [
  { value: '', label: '全部类型' },
  { value: 'alert', label: '告警' },
  { value: 'task', label: '定时任务' },
  { value: 'automation', label: '自动化' },
  { value: 'device', label: '设备' },
]

// YYYY-MM-DD（本地时区）
function todayStr() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
// stats.daily 的 "MM-DD" → 当年 "YYYY-MM-DD"（图表桶由后端按本地时区生成）
function fullDay(mmdd) {
  return `${new Date().getFullYear()}-${mmdd}`
}

// 下钻到某天（图表柱点击入口）
function drillToDay(mmdd) {
  drillDay.value = fullDay(mmdd)
  dayKindFilter.value = ''
  loadDayEvents()
}

function exitDrill() {
  drillDay.value = null
  dayEvents.value = []
}

async function loadDayEvents() {
  dayLoading.value = true
  try {
    const data = await apiGet(`/api/events?date=${drillDay.value}`)
    const list = data || []
    dayEvents.value = list.filter(e => !dayKindFilter.value || e.kind.startsWith(dayKindFilter.value))
  } catch (e) {
    console.error('加载当天事件失败', e)
    dayEvents.value = []
  } finally {
    dayLoading.value = false
  }
}

async function loadStats() {
  statsLoading.value = true
  try {
    stats.value = await apiGet(`/api/events/stats?days=${days.value === '1' ? 1 : days.value}`)
  } catch (e) {
    console.error('加载统计失败', e)
    stats.value = null
  } finally {
    statsLoading.value = false
  }
}

const totals = computed(() => stats.value?.totals || {})
const opCount = computed(() => totals.value.device_op || 0)
const autoCount = computed(() => totals.value.automation || 0)
const taskOk = computed(() => totals.value.task_success || 0)
const taskFail = computed(() => totals.value.task_failed || 0)
const taskTotal = computed(() => taskOk.value + taskFail.value)
const taskRate = computed(() =>
  taskTotal.value ? Math.round((taskOk.value / taskTotal.value) * 100) : null)
const alertCount = computed(() =>
  (totals.value.alert || 0) + (totals.value.alert_resolved || 0))

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
function fmtTimeInDay(ms) {
  return new Date(ms).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
function fmtDayTitle(iso) {
  const today = todayStr()
  if (iso === today) return '今天'
  const d = new Date(iso + 'T00:00:00')
  const week = ['日', '一', '二', '三', '四', '五', '六'][d.getDay()]
  return `${d.getMonth() + 1} 月 ${d.getDate()} 日 · 周${week}`
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
  plugin: '插件', device_state: '设备变化', device_op: '设备操作',
}

// 统计数字卡（总览页图表下方展示；样式模式与 MonitorView 的 metric-card 一致）
const statCards = computed(() => [
  { label: '设备操作', value: String(opCount.value), detail: `近 ${days.value === '1' ? '今天' : days.value + ' 天'}` },
  { label: '自动化触发', value: String(autoCount.value), detail: `近 ${days.value === '1' ? '今天' : days.value + ' 天'}` },
  {
    label: '任务成功率',
    value: taskRate.value == null ? '—' : `${taskRate.value}%`,
    detail: taskTotal.value ? `成功 ${taskOk.value} / 失败 ${taskFail.value}` : '暂无执行',
  },
  { label: '告警', value: String(alertCount.value), detail: alertCount.value ? `含恢复 ${totals.value.alert_resolved || 0} 次` : '一切正常' },
])

function onDaysChange() {
  exitDrill()
  loadStats()
}

onMounted(() => { loadStats(); loadReport() })
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div class="header-left">
        <h1>家庭报告</h1>
        <p class="page-sub">家里发生了什么 —— 点击柱状图某一天，看那天的明细</p>
      </div>
      <div class="header-tools">
        <FlowSelect v-model="days" :options="dayOptions" width="108px" @change="onDaysChange" />
        <button class="btn-primary gen-btn" :disabled="generating" @click="generateReport">
          {{ generating ? '生成中…' : '生成周报' }}
        </button>
      </div>
    </header>
    <p v-if="genMsg" class="gen-msg">{{ genMsg }}</p>

    <!-- 下钻视图：某天的事件时间线（周报/数字卡不在此页） -->
    <section v-if="drillDay" class="setting-card day-card">
      <div class="events-head">
        <div class="crumb">
          <button class="crumb-link" @click="exitDrill">‹ 返回总览</button>
          <h2>{{ fmtDayTitle(drillDay) }}</h2>
          <span class="crumb-count">{{ dayEvents.length }} 条</span>
        </div>
        <FlowSelect v-model="dayKindFilter" :options="dayKindOptions" width="118px" @change="loadDayEvents" />
      </div>
      <p v-if="dayLoading" class="loading-state">加载中…</p>
      <p v-else-if="!dayEvents.length" class="muted">这一天家里很平静，没有记录 🍃</p>
      <ul v-else class="event-list event-list--day">
        <li v-for="e in dayEvents" :key="e.id" class="event-item">
          <span class="event-time">{{ fmtTimeInDay(e.created_at) }}</span>
          <span class="event-kind" :class="kindClass(e.kind)">{{ KIND_LABELS[e.kind] || e.kind }}</span>
          <span class="event-msg">{{ e.message }}</span>
        </li>
      </ul>
    </section>

    <!-- 总览视图：图表为主体 + 数字卡 + 周报 -->
    <template v-else>
      <section class="chart-card setting-card">
        <div class="events-head">
          <h2>📊 每日事件趋势</h2>
          <span class="chart-hint">点击柱子查看当天明细</span>
        </div>
        <p v-if="statsLoading" class="loading-state">统计加载中…</p>
        <EventCharts v-else :stats="stats" @drill="drillToDay" />
      </section>

      <section class="stats-card setting-card">
        <div class="metrics-grid">
          <div v-for="c in statCards" :key="c.label" class="metric-card">
            <span class="metric-label">{{ c.label }}</span>
            <span class="metric-value">{{ c.value }}</span>
            <span class="metric-detail">{{ c.detail }}</span>
          </div>
        </div>
      </section>

      <section class="report-card setting-card">
        <div class="report-head">
          <h2>📋 家庭周报</h2>
        </div>
        <p v-if="reportLoading" class="loading-state">加载中…</p>
        <template v-else-if="report">
          <p class="report-time">{{ fmtTime(report.generated_at) }}</p>
          <p class="report-text">{{ report.text }}</p>
        </template>
        <p v-else class="muted">还没有周报（每周日晚自动生成，可在 config 开启 weekly_report.enabled）</p>
      </section>
    </template>
  </div>
</template>

<style scoped>
.page-header--split {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-10);
}

.header-tools {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  padding-top: var(--space-2);
}

.chart-card,
.stats-card,
.report-card,
.day-card {
  padding: var(--space-16);
  margin-bottom: var(--space-16);
}

.chart-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
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
  margin: 0 var(--space-2) var(--space-8);
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

/* 下钻面包屑 */
.crumb {
  display: flex;
  align-items: baseline;
  gap: var(--space-10);
}

.crumb-link {
  background: none;
  border: none;
  color: var(--color-primary);
  font-size: var(--text-sm);
  cursor: pointer;
  padding: 0;
}

.crumb-link:hover { text-decoration: underline; }

.crumb h2 {
  margin: 0;
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.crumb-count {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

/* 数字卡片网格（模式与 MonitorView 的 metrics-grid 一致） */
.metrics-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: var(--space-8);
}

.metric-card {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-8) var(--space-10);
}

.metric-label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.metric-value {
  font-size: var(--text-2xl, 26px);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  line-height: 1.2;
}

.metric-detail {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.event-list {
  list-style: none;
  margin: 0;
  padding: 0;
  max-height: 480px;
  overflow-y: auto;
}

.event-list--day {
  max-height: none;
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
  .page-header--split {
    flex-direction: column;
  }
  .events-head {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
