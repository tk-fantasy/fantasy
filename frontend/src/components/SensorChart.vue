<script setup>
/**
 * 传感器历史趋势图 — 手写 SVG 折线图，零依赖。
 *
 * 数据源：后端 GET /api/ha/history?filter_entity_id=...&hours=24
 * HA history 返回 [[{state, last_updated}, ...]]（外层每项一个实体）。
 */
import { ref, computed, onMounted } from 'vue'
import { apiGet } from '../utils/api'

const props = defineProps({
  entityId: { type: String, required: true },
  unit: { type: String, default: '' },
})

const points = ref([])       // [{t: Date, v: number}]
const loading = ref(true)
const error = ref('')

async function loadHistory() {
  loading.value = true
  error.value = ''
  try {
    const data = await apiGet(`/api/ha/history?filter_entity_id=${encodeURIComponent(props.entityId)}&hours=24`)
    const history = data?.history || []
    // HA 返回 [[entity_history], ...]，取第一个实体的历史
    const entityHistory = Array.isArray(history[0]) ? history[0] : []
    const pts = entityHistory
      .map(h => {
        const v = parseFloat(h.state)
        if (isNaN(v)) return null
        return { t: new Date(h.last_updated), v }
      })
      .filter(Boolean)
    points.value = pts
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(loadHistory)

// ---- SVG 几何计算 ----
const W = 480
const H = 160
const PAD = { top: 16, right: 16, bottom: 28, left: 40 }

const validPoints = computed(() => points.value.filter(p => !isNaN(p.v)))
const hasData = computed(() => validPoints.value.length >= 2)

const minV = computed(() => hasData.value ? Math.min(...validPoints.value.map(p => p.v)) : 0)
const maxV = computed(() => hasData.value ? Math.max(...validPoints.value.map(p => p.v)) : 100)
const rangeV = computed(() => maxV.value - minV.value || 1)

const minT = computed(() => hasData.value ? validPoints.value[0].t.getTime() : 0)
const maxT = computed(() => hasData.value ? validPoints.value[validPoints.value.length - 1].t.getTime() : 1)
const rangeT = computed(() => maxT.value - minT.value || 1)

function x(t) {
  return PAD.left + ((t - minT.value) / rangeT.value) * (W - PAD.left - PAD.right)
}
function y(v) {
  return PAD.top + (1 - (v - minV.value) / rangeV.value) * (H - PAD.top - PAD.bottom)
}

const pathD = computed(() => {
  if (!hasData.value) return ''
  return validPoints.value.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.t.getTime()).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')
})

const areaD = computed(() => {
  if (!hasData.value) return ''
  const base = H - PAD.bottom
  const first = validPoints.value[0]
  const last = validPoints.value[validPoints.value.length - 1]
  return `M${x(first.t.getTime()).toFixed(1)},${base} ` +
    validPoints.value.map(p => `L${x(p.t.getTime()).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ') +
    ` L${x(last.t.getTime()).toFixed(1)},${base} Z`
})

// Y 轴刻度（3 档：min / mid / max）
const yTicks = computed(() => {
  if (!hasData.value) return []
  const mid = (minV.value + maxV.value) / 2
  return [
    { v: maxV.value, y: y(maxV.value) },
    { v: mid, y: y(mid) },
    { v: minV.value, y: y(minV.value) },
  ]
})

// X 轴刻度（4 档时间标签）
const xTicks = computed(() => {
  if (!hasData.value) return []
  const fmt = ts => {
    const d = new Date(ts)
    return `${d.getHours().toString().padStart(2, '0')}:${d.getMinutes().toString().padStart(2, '0')}`
  }
  return [0, 1, 2, 3].map(i => {
    const ts = minT.value + (rangeT.value * i) / 3
    return { label: fmt(ts), x: x(ts) }
  })
})

const currentValue = computed(() => {
  if (!hasData.value) return null
  return validPoints.value[validPoints.value.length - 1].v
})
</script>

<template>
  <div class="sensor-chart">
    <div v-if="loading" class="chart-status">加载历史数据…</div>
    <div v-else-if="error" class="chart-status chart-error">{{ error }}</div>
    <div v-else-if="!hasData" class="chart-status">暂无历史数据</div>
    <template v-else>
      <div class="chart-header">
        <span class="chart-current">{{ Math.round(currentValue * 100) / 100 }}{{ unit }}</span>
        <span class="chart-range">{{ Math.round(minV * 100) / 100 }}{{ unit }} ~ {{ Math.round(maxV * 100) / 100 }}{{ unit }}</span>
      </div>
      <svg :viewBox="`0 0 ${W} ${H}`" class="chart-svg" preserveAspectRatio="xMidYMid meet">
        <!-- Y 轴刻度线 + 标签 -->
        <g class="axis-y">
          <line v-for="(tick, i) in yTicks" :key="'y'+i" :x1="PAD.left" :x2="W - PAD.right" :y1="tick.y" :y2="tick.y" class="grid-line" />
          <text v-for="(tick, i) in yTicks" :key="'yl'+i" :x="PAD.left - 6" :y="tick.y + 3" text-anchor="end" class="axis-label">{{ Math.round(tick.v * 100) / 100 }}</text>
        </g>
        <!-- X 轴刻度标签 -->
        <g class="axis-x">
          <text v-for="(tick, i) in xTicks" :key="'x'+i" :x="tick.x" :y="H - 8" text-anchor="middle" class="axis-label">{{ tick.label }}</text>
        </g>
        <!-- 填充区域 -->
        <path :d="areaD" class="chart-area" />
        <!-- 折线 -->
        <path :d="pathD" class="chart-line" fill="none" />
      </svg>
    </template>
  </div>
</template>

<style scoped>
.sensor-chart {
  width: 100%;
}

.chart-status {
  padding: var(--space-12);
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
}

.chart-error {
  color: var(--color-error, #e57373);
}

.chart-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  margin-bottom: var(--space-8);
}

.chart-current {
  font-size: var(--text-xl);
  font-weight: 600;
  color: var(--color-text);
}

.chart-range {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.chart-svg {
  width: 100%;
  height: auto;
  display: block;
}

.grid-line {
  stroke: rgba(255, 255, 255, 0.06);
  stroke-width: 1;
}

.axis-label {
  fill: var(--color-text-muted);
  font-size: 10px;
}

.chart-area {
  fill: var(--color-primary);
  opacity: 0.12;
}

.chart-line {
  stroke: var(--color-primary);
  stroke-width: 2;
  stroke-linejoin: round;
  stroke-linecap: round;
}
</style>
