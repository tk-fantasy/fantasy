<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import EmojiPicker from './EmojiPicker.vue'

const weather = ref(null)
const loading = ref(true)
const error = ref(null)
const expanded = ref(false)
let timer = null

// Emoji 偏好管理
const emojiPrefs = ref({})
const showEmojiPicker = ref(false)
const currentEmojiTarget = ref(null)

async function loadEmojiPrefs() {
  try {
    const res = await fetch('/api/emoji/preferences')
    if (!res.ok) return
    const json = await res.json()
    const prefs = {}
    for (const item of (json.data || [])) {
      prefs[`${item.scope}:${item.key}`] = item.emoji_char
    }
    emojiPrefs.value = prefs
  } catch (e) {
    console.error('Failed to load emoji prefs:', e)
  }
}

function openEmojiPicker(scope, key) {
  currentEmojiTarget.value = { scope, key }
  showEmojiPicker.value = true
}

async function onEmojiSelect(item) {
  if (!currentEmojiTarget.value) return
  const { scope, key } = currentEmojiTarget.value
  const prefKey = `${scope}:${key}`
  
  try {
    await fetch('/api/emoji/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, key, emoji_char: item.char }),
    })
    emojiPrefs.value[prefKey] = item.char
  } catch (e) {
    console.error('Failed to save emoji pref:', e)
  }
}

const isNight = computed(() => {
  const hour = new Date().getHours()
  return hour < 6 || hour >= 19
})

const defaultWeatherIcons = {
  '100': '☀️', '101': '⛅', '102': '☁️', '103': '⛅', '104': '☁️',
  '150': '🌙', '151': '🌙', '153': '🌙',
  '300': '🌦️', '301': '🌧️', '302': '⛈️', '303': '⛈️', '304': '⛈️',
  '305': '🌧️', '306': '🌧️', '307': '🌧️', '308': '🌧️', '309': '🌧️',
  '310': '🌧️', '311': '🌧️', '312': '🌧️', '313': '🌧️', '314': '🌧️',
  '315': '🌧️', '316': '🌧️', '317': '🌧️', '318': '🌧️', '399': '🌧️',
  '400': '🌨️', '401': '🌨️', '402': '🌨️', '403': '🌨️', '404': '🌨️',
  '405': '🌨️', '406': '🌨️', '407': '🌨️', '408': '🌨️', '409': '🌨️',
  '410': '🌨️', '499': '🌨️',
  '500': '🌫️', '501': '🌫️', '502': '🌫️', '503': '🌫️', '504': '🌫️',
  '507': '🌫️', '508': '🌫️', '509': '🌫️', '510': '🌫️', '511': '🌫️',
  '512': '🌫️', '513': '🌫️', '514': '🌫️', '515': '🌫️',
}

const weatherIcon = computed(() => {
  if (!weather.value?.icon) return isNight.value ? '🌙' : '🌤️'
  const code = weather.value.icon
  const prefKey = `weather:${code}`
  return emojiPrefs.value[prefKey] || defaultWeatherIcons[code] || (isNight.value ? '🌙' : '🌤️')
})

async function fetchWeather() {
  try {
    loading.value = true
    error.value = null
    const res = await fetch('/api/weather')
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    const json = await res.json()
    if (json.code === 'ok' && json.data && !json.data.error) {
      weather.value = json.data
    } else {
      error.value = json.data?.error || json.message || '获取天气失败'
    }
  } catch (e) {
    error.value = e.message
  } finally {
    loading.value = false
  }
}

function toggleExpand() {
  expanded.value = !expanded.value
}

function onHomeInfoChanged() {
  fetchWeather()
}

onMounted(() => {
  fetchWeather()
  loadEmojiPrefs()
  timer = setInterval(fetchWeather, 15 * 60 * 1000)
  window.addEventListener('home-info-changed', onHomeInfoChanged)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
  window.removeEventListener('home-info-changed', onHomeInfoChanged)
})
</script>

<template>
  <div class="weather-container">
    <!-- 紧凑模式 -->
    <div class="weather-widget" v-if="weather && !error" @click="toggleExpand">
      <span 
        class="weather-icon emoji-trigger"
        @click.stop="openEmojiPicker('weather', weather?.icon || 'default')"
      >{{ weatherIcon }}</span>
      <span class="weather-temp">{{ weather.temperature }}°</span>
      <span class="weather-desc">{{ weather.weather }}</span>
      <span class="weather-location">{{ weather.location }}</span>
      <span class="expand-icon">{{ expanded ? '▲' : '▼' }}</span>
    </div>
    <div class="weather-widget weather-error" v-else-if="error && !loading">
      <span class="weather-desc">{{ error }}</span>
    </div>

    <!-- 展开详情 -->
    <div class="weather-dropdown" v-if="expanded && weather">
      <div class="dropdown-section">
        <h4 class="section-title">📍 位置信息</h4>
        <div class="info-grid">
          <div class="info-item">
            <span class="label">城市</span>
            <span class="value">{{ weather.location }}</span>
          </div>
          <div class="info-item" v-if="weather.location_id">
            <span class="label">位置ID</span>
            <span class="value">{{ weather.location_id }}</span>
          </div>
          <div class="info-item" v-if="weather.obs_time">
            <span class="label">更新时间</span>
            <span class="value">{{ weather.obs_time }}</span>
          </div>
        </div>
      </div>

      <div class="dropdown-section">
        <h4 class="section-title">🌡️ 当前天气</h4>
        <div class="info-grid">
          <div class="info-item">
            <span class="label">温度</span>
            <span class="value">{{ weather.temperature }}°C</span>
          </div>
          <div class="info-item">
            <span class="label">体感温度</span>
            <span class="value">{{ weather.feels_like }}°C</span>
          </div>
          <div class="info-item">
            <span class="label">湿度</span>
            <span class="value">{{ weather.humidity }}%</span>
          </div>
          <div class="info-item">
            <span class="label">风向</span>
            <span class="value">{{ weather.wind_dir }}</span>
          </div>
          <div class="info-item">
            <span class="label">风力</span>
            <span class="value">{{ weather.wind_scale }}级</span>
          </div>
          <div class="info-item">
            <span class="label">风速</span>
            <span class="value">{{ weather.wind_speed }}km/h</span>
          </div>
          <div class="info-item">
            <span class="label">能见度</span>
            <span class="value">{{ weather.visibility }}km</span>
          </div>
          <div class="info-item">
            <span class="label">紫外线</span>
            <span class="value">{{ weather.uv_index || '无数据' }}</span>
          </div>
        </div>
      </div>

      <div class="dropdown-section" v-if="weather.indices && weather.indices.length > 0">
        <h4 class="section-title">📊 生活指数</h4>
        <div class="indices-list">
          <div v-for="idx in weather.indices" :key="idx.type" class="index-item">
            <div class="index-header">
              <span class="index-name">{{ idx.name }}</span>
              <span class="index-category" :class="'level-' + idx.level">{{ idx.category }}</span>
            </div>
            <div class="index-text">{{ idx.text }}</div>
          </div>
        </div>
      </div>
    </div>

    <EmojiPicker
      :visible="showEmojiPicker"
      @update:visible="showEmojiPicker = $event"
      @select="onEmojiSelect"
    />
  </div>
</template>

<style scoped>
.weather-container {
  position: fixed;
  top: var(--space-10);
  right: var(--space-14);
  z-index: 100;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--space-4);
}

.weather-widget {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  padding: var(--space-4) var(--space-8);
  background: var(--color-surface);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  transition: all var(--duration-normal) var(--ease-out);
  cursor: pointer;
  -webkit-user-select: none;
  user-select: none;
}

.weather-widget:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-primary);
}

.weather-icon {
  font-size: var(--text-xl);
}

.weather-icon.emoji-trigger {
  cursor: pointer;
  transition: transform var(--duration-fast);
  border-radius: var(--radius-sm);
  padding: var(--space-1);
}

.weather-icon.emoji-trigger:hover {
  transform: scale(1.2);
}

.weather-temp {
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  font-size: var(--text-base);
}

.weather-desc {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.weather-location {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  max-width: 80px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.expand-icon {
  font-size: 10px;
  color: var(--color-text-muted);
  margin-left: var(--space-2);
}

.weather-error {
  opacity: 0.6;
  cursor: default;
}

/* 下拉详情面板 */
.weather-dropdown {
  width: 360px;
  max-height: 500px;
  overflow-y: auto;
  background: var(--color-surface);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-lg);
  padding: var(--space-8);
}

.dropdown-section {
  margin-bottom: var(--space-8);
}

.dropdown-section:last-child {
  margin-bottom: 0;
}

.section-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin-bottom: var(--space-4);
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--color-border);
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--space-4);
}

.info-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.info-item .label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.info-item .value {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

/* 生活指数列表 */
.indices-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.index-item {
  padding: var(--space-4);
  background: rgba(255, 255, 255, 0.03);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
}

.index-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: var(--space-2);
}

.index-name {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.index-category {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  padding: var(--space-1) var(--space-4);
  border-radius: var(--radius-full);
  background: rgba(255, 255, 255, 0.06);
  color: var(--color-text-secondary);
}

.index-category.level-1 { background: var(--color-success-bg); color: var(--color-success); }
.index-category.level-2 { background: var(--color-primary-light); color: var(--color-primary); }
.index-category.level-3 { background: var(--color-warning-bg); color: var(--color-warning); }
.index-category.level-4 { background: var(--color-danger-bg); color: var(--color-danger); }
.index-category.level-5 { background: var(--color-danger-bg); color: var(--color-danger); }

.index-text {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  line-height: 1.5;
}

/* 滚动条样式 */
.weather-dropdown::-webkit-scrollbar {
  width: 6px;
}

.weather-dropdown::-webkit-scrollbar-track {
  background: transparent;
}

.weather-dropdown::-webkit-scrollbar-thumb {
  background: var(--color-border);
  border-radius: 3px;
}

.weather-dropdown::-webkit-scrollbar-thumb:hover {
  background: var(--color-border-hover);
}

@media (max-width: 768px) {
  .weather-container {
    top: var(--space-6);
    right: var(--space-6);
  }
  
  .weather-location {
    display: none;
  }
  
  .weather-dropdown {
    width: calc(100vw - var(--space-12));
    max-width: 360px;
  }
}
</style>
