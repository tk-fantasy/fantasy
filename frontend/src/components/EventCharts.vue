<script setup>
/**
 * 家庭事件统计图 — 基于 ECharts（家庭报告页图表区）。
 *
 * 数据源：GET /api/events/stats（report_routes 聚合 family_events 得到），
 * 由父组件拉取后以 props 传入，本组件只负责渲染。
 *
 * 两张图：每日事件趋势（堆叠柱状，device_op/automation/task/alert）、
 * 设备操作 TOP5（横向条形，AI/手动堆叠）。AI/手动占比用 CSS 条表达，不单独成图。
 *
 * 报告页在页面底部、可能滚出视口，照 SensorChart 范式做 IntersectionObserver
 * 懒初始化；颜色从 CSS 变量读取保持主题一致。
 */
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { BarChart, PieChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

echarts.use([
  BarChart, PieChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer,
])

const props = defineProps({
  stats: { type: Object, default: null },
})

// 点击趋势柱某一天 → 通知父组件下钻到当天时间线
const emit = defineEmits(['drill'])

const rootEl = ref(null)
const trendEl = ref(null)
const topEl = ref(null)
const awaitingView = ref(true)
let trendChart = null
let topChart = null
let observer = null

function themeColor(varName, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim()
  return v || fallback
}

// 语义色：操作=主题色 / 自动化=蓝 / 任务=黄 / 告警=红（与时间线徽章同语）
function palette() {
  return {
    device_op: themeColor('--color-primary', '#4a7c70'),
    automation: themeColor('--color-info', '#5dade2'),
    task: themeColor('--color-warning', '#f0c040'),
    alert: themeColor('--color-danger', '#e57373'),
    manual: themeColor('--color-text-muted', 'rgba(255,255,255,0.4)'),
  }
}

function textColor() {
  return themeColor('--color-text-secondary', 'rgba(255,255,255,0.6)')
}

function gridColor() {
  return 'rgba(255,255,255,0.06)'
}

const KIND_SERIES = [
  { key: 'device_op', label: '设备操作' },
  { key: 'automation', label: '自动化' },
  { key: 'task', label: '定时任务' },
  { key: 'alert', label: '告警' },
]

function buildTrendOption(daily) {
  const colors = palette()
  return {
    grid: { left: 40, right: 16, top: 32, bottom: 28, containLabel: true },
    legend: {
      top: 0, right: 0, icon: 'roundRect', itemWidth: 12, itemHeight: 8,
      textStyle: { color: textColor(), fontSize: 11 },
    },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(30,35,38,0.95)',
      borderColor: gridColor(),
      textStyle: { color: 'rgba(255,255,255,0.9)', fontSize: 12 },
      formatter: (params) => {
        if (!params?.length) return ''
        const total = params.reduce((s, p) => s + (p.value || 0), 0)
        const rows = params
          .filter(p => p.value > 0)
          .map(p => `${p.marker}${p.seriesName} ${p.value}`)
        return [`<b>${params[0].name}</b>（点击查看当天）`, `共 ${total} 条`, ...rows]
          .join('<br/>')
      },
    },
    xAxis: {
      type: 'category',
      data: daily.map(d => d.day),
      axisLabel: { color: textColor(), fontSize: 10 },
      axisLine: { lineStyle: { color: gridColor() } },
      axisTick: { show: false },
      triggerEvent: true,
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: textColor(), fontSize: 10 },
      splitLine: { lineStyle: { color: gridColor() } },
    },
    series: KIND_SERIES.map(s => ({
      name: s.label,
      type: 'bar',
      stack: 'total',
      barMaxWidth: 18,
      itemStyle: { color: colors[s.key], borderRadius: s.key === 'alert' ? [3, 3, 0, 0] : 0 },
      cursor: 'pointer',
      data: daily.map(d => d[s.key] || 0),
    })),
  }
}

function buildTopOption(topDevices) {
  const colors = palette()
  const top = topDevices.slice(0, 5)
  // 横向条形图 index 越大越靠上，反转使第一名在顶部
  const names = [...top].reverse().map(d => d.name)
  return {
    grid: { left: 8, right: 24, top: 30, bottom: 8, containLabel: true },
    legend: {
      top: 0, right: 0, icon: 'roundRect', itemWidth: 12, itemHeight: 8,
      textStyle: { color: textColor(), fontSize: 11 },
    },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      backgroundColor: 'rgba(30,35,38,0.95)',
      borderColor: gridColor(),
      textStyle: { color: 'rgba(255,255,255,0.9)', fontSize: 12 },
    },
    xAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: textColor(), fontSize: 10 },
      splitLine: { lineStyle: { color: gridColor() } },
    },
    yAxis: {
      type: 'category',
      data: names,
      axisLabel: {
        color: textColor(), fontSize: 11,
        // 设备名过长截断
        formatter: (v) => (v.length > 8 ? v.slice(0, 8) + '…' : v),
      },
      axisTick: { show: false },
      axisLine: { show: false },
    },
    series: [
      {
        name: 'AI 操作', type: 'bar', stack: 'op', barMaxWidth: 14,
        itemStyle: { color: colors.device_op },
        data: [...top].reverse().map(d => d.ai),
      },
      {
        name: '手动', type: 'bar', stack: 'op', barMaxWidth: 14,
        itemStyle: { color: colors.manual },
        data: [...top].reverse().map(d => d.manual),
      },
    ],
  }
}

const hasTrendData = computed(() =>
  (props.stats?.daily || []).some(d =>
    KIND_SERIES.some(s => (d[s.key] || 0) > 0)))
const hasTopData = computed(() =>
  (props.stats?.top_devices || []).some(d => d.count > 0))
const hasAnyData = computed(() => hasTrendData.value || hasTopData.value)

const actorTotal = computed(() => {
  const a = props.stats?.actor || { ai: 0, manual: 0 }
  return a.ai + a.manual
})
const actorAiPct = computed(() => {
  if (!actorTotal.value) return 0
  return Math.round(((props.stats.actor.ai) / actorTotal.value) * 100)
})

function renderCharts() {
  if (!props.stats) return
  if (trendChart) {
    trendChart.setOption(
      buildTrendOption(props.stats.daily || []),
      { notMerge: true },
    )
  }
  if (topChart) {
    topChart.setOption(
      buildTopOption(props.stats.top_devices || []),
      { notMerge: true },
    )
  }
}

function initCharts() {
  if (trendEl.value && !trendChart) {
    trendChart = echarts.init(trendEl.value)
    // 点击柱子（某一天）→ 下钻当天时间线；图标 hover 提示可点
    trendChart.on('click', (params) => {
      if (params.componentType === 'series' && params.name) {
        emit('drill', params.name)
      }
    })
  }
  if (topEl.value && !topChart) topChart = echarts.init(topEl.value)
  renderCharts()
}

function handleResize() {
  trendChart?.resize()
  topChart?.resize()
}

function start() {
  awaitingView.value = false
  initCharts()
}

onMounted(async () => {
  await nextTick()
  window.addEventListener('resize', handleResize)
  // stats 已就绪说明父组件数据已到，直接渲染；否则等数据到了再启动
  if (typeof IntersectionObserver === 'undefined') {
    start()
    return
  }
  observer = new IntersectionObserver((entries) => {
    if (entries.some(e => e.isIntersecting)) {
      start()
      observer?.disconnect()
      observer = null
    }
  }, { rootMargin: '80px' })
  observer.observe(rootEl.value)
  if (props.stats) start()
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  observer?.disconnect()
  observer = null
  trendChart?.dispose()
  topChart?.dispose()
  trendChart = null
  topChart = null
})

watch(() => props.stats, () => {
  // 数据变化（切天数）时若已启动立即重渲染；未启动等可见时
  if (!awaitingView.value) renderCharts()
})
</script>

<template>
  <div ref="rootEl" class="event-charts">
    <template v-if="hasAnyData">
      <div class="chart-block">
        <h4 class="chart-title">每日事件趋势</h4>
        <div ref="trendEl" class="chart-canvas"></div>
      </div>
      <div class="chart-row">
        <div class="chart-block chart-block--top" v-if="hasTopData">
          <h4 class="chart-title">设备操作 TOP5</h4>
          <div ref="topEl" class="chart-canvas chart-canvas--top"></div>
        </div>
        <div class="chart-block chart-block--actor" v-if="actorTotal > 0">
          <h4 class="chart-title">操作发起方</h4>
          <div class="actor-stat">
            <div class="actor-bar">
              <div class="actor-fill" :style="{ width: actorAiPct + '%' }"></div>
            </div>
            <div class="actor-legend">
              <span class="actor-item"><i class="dot dot--ai"></i>AI {{ stats.actor.ai }} 次（{{ actorAiPct }}%）</span>
              <span class="actor-item"><i class="dot dot--manual"></i>手动 {{ stats.actor.manual }} 次</span>
            </div>
          </div>
        </div>
      </div>
    </template>
    <p v-else-if="!awaitingView" class="charts-empty">这段时间家里很平静，没有可统计的事件 🍃</p>
  </div>
</template>

<style scoped>
.event-charts {
  display: flex;
  flex-direction: column;
  gap: var(--space-10);
}

.chart-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: var(--space-10);
}

.chart-block {
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-8) var(--space-10);
  min-width: 0;
}

.chart-title {
  margin: 0 0 var(--space-4);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--color-text-secondary);
}

.chart-canvas {
  width: 100%;
  height: 220px;
}

.chart-canvas--top {
  height: 200px;
}

.actor-stat {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: var(--space-8);
  min-height: 200px;
}

.actor-bar {
  height: 12px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.06);
  overflow: hidden;
}

.actor-fill {
  height: 100%;
  border-radius: 6px;
  background: var(--color-primary);
  transition: width var(--duration-normal, 0.2s) var(--ease-out, ease);
}

.actor-legend {
  display: flex;
  gap: var(--space-12);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.actor-item {
  display: inline-flex;
  align-items: center;
  gap: var(--space-3);
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}

.dot--ai { background: var(--color-primary); }
.dot--manual { background: var(--color-text-muted); }

.charts-empty {
  margin: 0;
  padding: var(--space-10);
  text-align: center;
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}
</style>
