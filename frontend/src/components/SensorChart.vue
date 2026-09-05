<script setup>
/**
 * 传感器历史趋势图 — 基于 ECharts。
 *
 * 数据源：后端 GET /api/ha/history?filter_entity_id=...&hours=24
 * HA history 返回 [[{state, last_updated}, ...]]（外层每项一个实体）。
 *
 * 功能：tooltip 悬停详情、dataZoom 时间缩放、时间窗切换（1h/6h/24h/7d）。
 *
 * 平铺场景：设备详情弹窗会为每个传感器各挂一个实例，因此本组件
 * 进入视口后才初始化/拉取数据（IntersectionObserver 懒加载），
 * 避免一次挂载几十个实例时齐发请求；canvas 容器常驻布局，
 * 避免 v-show 隐藏期间 echarts 拿到 0×0 尺寸。
 */
import { ref, computed, onMounted, onBeforeUnmount, watch, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, DataZoomComponent, MarkLineComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { apiGet } from '../utils/api'

echarts.use([
  LineChart, GridComponent, TooltipComponent, DataZoomComponent,
  MarkLineComponent, CanvasRenderer,
])

const props = defineProps({
  entityId: { type: String, required: true },
  unit: { type: String, default: '' },
})

const TIME_WINDOWS = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
]
const selectedHours = ref(24)

const rootEl = ref(null)
const chartContainer = ref(null)
const points = ref([])       // [[timestamp_ms, value], ...]
const awaitingView = ref(true)  // 尚未进入可视区，等待懒加载
const loading = ref(false)
const error = ref('')
let chart = null
let observer = null
let started = false

// 从 CSS 变量读取主题色，保持与页面风格一致
function themeColor(varName, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(varName).trim()
  return v || fallback
}

function buildOption(data, isBinary = false) {
  const primary = themeColor('--color-primary', '#4a7c70')
  const textColor = themeColor('--color-text-secondary', 'rgba(255,255,255,0.6)')
  const gridColor = 'rgba(255,255,255,0.06)'
  const u = props.unit

  return {
    grid: { left: 48, right: 20, top: 20, bottom: 56, containLabel: false },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(30,35,38,0.95)',
      borderColor: gridColor,
      textStyle: { color: 'rgba(255,255,255,0.9)', fontSize: 12 },
      formatter: (params) => {
        const p = params[0]
        if (!p) return ''
        const d = new Date(p.value[0])
        const ts = `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
        const val = Math.round(p.value[1] * 100) / 100
        return `${ts}<br/>${val}${u}`
      },
    },
    xAxis: {
      type: 'time',
      axisLabel: { color: textColor, fontSize: 10, hideOverlap: true },
      axisLine: { lineStyle: { color: gridColor } },
      splitLine: { show: false },
    },
    yAxis: {
      type: 'value',
      scale: !isBinary,
      min: isBinary ? 0 : undefined,
      max: isBinary ? 1 : undefined,
      interval: isBinary ? 1 : undefined,
      name: isBinary ? '' : u,
      nameTextStyle: { color: textColor, fontSize: 10 },
      axisLabel: {
        color: textColor, fontSize: 10,
        formatter: (v) => (isBinary ? (v === 1 ? '开' : '关') : Math.round(v * 100) / 100),
      },
      splitLine: { lineStyle: { color: gridColor } },
    },
    dataZoom: [
      { type: 'inside', start: 0, end: 100 },
      {
        type: 'slider', start: 0, end: 100, height: 18, bottom: 8,
        borderColor: 'transparent', backgroundColor: 'rgba(255,255,255,0.04)',
        fillerColor: 'rgba(74,124,112,0.15)',
        handleStyle: { color: primary },
        textStyle: { color: textColor, fontSize: 10 },
      },
    ],
    series: [{
      type: 'line',
      data,
      // 二值数据（开关/占用等）用阶梯线，避免 smooth 把 0/1 画成斜线。
      // 连续度量值（温度/湿度）用轻微平滑。
      step: isBinary ? 'end' : false,
      smooth: isBinary ? false : 0.2,
      symbol: 'none',
      lineStyle: { color: primary, width: 2 },
      areaStyle: isBinary ? undefined : {
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: 'rgba(74,124,112,0.25)' },
          { offset: 1, color: 'rgba(74,124,112,0.02)' },
        ]),
      },
    }],
  }
}

async function loadHistory() {
  loading.value = true
  error.value = ''
  const requestedId = props.entityId
  try {
    const data = await apiGet(`/api/ha/history?filter_entity_id=${encodeURIComponent(props.entityId)}&hours=${selectedHours.value}`)
    // 实体已切换时丢弃过期响应，防止旧数据画进新图表
    if (requestedId !== props.entityId) return
    const history = data?.history || []
    const entityHistory = Array.isArray(history[0]) ? history[0] : []
    const pts = entityHistory
      .map(h => {
        // 支持 on/off/unavailable 等 binary 状态 → 0/1
        const raw = h.state
        let v = parseFloat(raw)
        if (isNaN(v)) {
          const lower = String(raw).toLowerCase()
          if (lower === 'on') v = 1
          else if (lower === 'off') v = 0
          else return null
        }
        return [new Date(h.last_updated).getTime(), v]
      })
      .filter(Boolean)
    points.value = pts
    renderChart()
  } catch (e) {
    if (requestedId !== props.entityId) return
    error.value = e.message || '加载失败'
  } finally {
    if (requestedId === props.entityId) loading.value = false
  }
}

function renderChart() {
  if (!chart || points.value.length < 2) return
  chart.setOption(buildOption(points.value, isBinary.value), { notMerge: true })
}

function initChart() {
  if (!chartContainer.value || chart) return
  chart = echarts.init(chartContainer.value)
  chart.setOption(buildOption([]))
}

function handleResize() {
  chart?.resize()
}

// 懒加载入口：进入视口才初始化图表并拉取历史
function start() {
  if (started) return
  started = true
  awaitingView.value = false
  initChart()
  loadHistory()
}

const hasData = computed(() => points.value.length >= 2)
// 二值检测：所有数据点的值仅落在 {0, 1} → 阶梯线（binary_sensor / 开关类）
const isBinary = computed(() => {
  if (!hasData.value) return false
  return points.value.every(p => p[1] === 0 || p[1] === 1)
})
const currentValue = computed(() => {
  if (!hasData.value) return null
  return Math.round(points.value[points.value.length - 1][1] * 100) / 100
})

onMounted(async () => {
  await nextTick()
  window.addEventListener('resize', handleResize)
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
  }, { rootMargin: '120px' })
  observer.observe(rootEl.value)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
})

// entityId 变化时重新加载（切换 sensor 实体）；尚未懒启动的实例等可见时再取
watch(() => props.entityId, () => { if (started) loadHistory() })
</script>

<template>
  <div ref="rootEl" class="sensor-chart">
    <div class="chart-toolbar" v-if="!awaitingView">
      <div class="chart-stats" v-if="hasData">
        <span class="chart-current">{{ currentValue }}{{ unit }}</span>
      </div>
      <div class="time-windows">
        <button
          v-for="w in TIME_WINDOWS"
          :key="w.hours"
          class="tw-btn"
          :class="{ active: selectedHours === w.hours }"
          @click="selectedHours = w.hours; loadHistory()"
        >{{ w.label }}</button>
      </div>
    </div>
    <div class="chart-wrap">
      <div ref="chartContainer" class="chart-canvas"></div>
      <div v-if="awaitingView" class="chart-status">滚动到此处加载趋势</div>
      <div v-else-if="loading" class="chart-status">加载历史数据…</div>
      <div v-else-if="error" class="chart-status chart-error">{{ error }}</div>
      <div v-else-if="!hasData" class="chart-status">暂无历史数据</div>
    </div>
  </div>
</template>

<style scoped>
.sensor-chart {
  width: 100%;
  min-width: 0;
}

.chart-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--space-8);
}

.chart-current {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.time-windows {
  display: flex;
  gap: var(--space-2);
}

.tw-btn {
  padding: var(--space-2) var(--space-6);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md, 6px);
  cursor: pointer;
  transition: all var(--duration-normal, 0.15s) var(--ease-out, ease);
}

.tw-btn:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.tw-btn.active {
  background: var(--color-primary-light);
  border-color: var(--color-border-active);
  color: var(--color-primary);
}

/* 画布常驻布局：即使处于加载/错误态也占住 220px 高度，
   保证 echarts 初始化时能拿到真实尺寸 */
.chart-wrap {
  position: relative;
  height: 220px;
}

.chart-canvas {
  width: 100%;
  height: 100%;
}

.chart-status {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}

.chart-error {
  color: var(--color-error, #e57373);
}
</style>
