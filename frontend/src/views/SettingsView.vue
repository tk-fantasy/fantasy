<script setup>
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { apiGet } from '../utils/api'
import BaseToggle from '../components/BaseToggle.vue'
import FlowSelect from '../components/FlowSelect.vue'
import { getProvinces, getPrefectures, getCounties } from 'china-region'

function buildRegions() {
  const r = {}
  const list = getProvinces()
  for (const p of list) {
    const prefs = getPrefectures(p.code)
    if (prefs && prefs.length > 0) {
      r[p.name] = {}
      for (const pref of prefs) {
        const counties = getCounties(pref.code) || []
        r[p.name][pref.name] = counties.map(c => c.name)
      }
    } else {
      const counties = getCounties(p.code) || []
      r[p.name] = { [p.name]: counties.map(c => c.name) }
    }
  }
  return r
}

const regions = buildRegions()
const provinces = Object.keys(regions)

const homeName = ref('')
const userName = ref('')
const province = ref('')
const city = ref('')
const district = ref('')
const loading = ref(true)
const saving = ref(false)
const saved = ref(false)

onMounted(() => {
  loadHomeInfo()
})

async function loadHomeInfo() {
  try {
    loading.value = true
    const data = await apiGet('/api/home/info')
    homeName.value = data.home_name || ''
    userName.value = data.owner_name || ''
    province.value = data.province || ''
    city.value = data.city || ''
    district.value = data.district || ''
  } catch (e) {
    console.error('Failed to load home info:', e)
  } finally {
    loading.value = false
  }
}

async function saveHomeInfo() {
  try {
    saving.value = true
    saved.value = false
    await fetch('/api/home/info', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        home_name: homeName.value,
        owner_name: userName.value,
        province: province.value,
        city: city.value,
        district: district.value,
      }),
    })
    saved.value = true
    // 通知天气组件地址已变更，立即刷新
    window.dispatchEvent(new Event('home-info-changed'))
    setTimeout(() => { saved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save home info:', e)
  } finally {
    saving.value = false
  }
}

const cities = computed(() => {
  return province.value ? Object.keys(regions[province.value] || {}) : []
})

const districts = computed(() => {
  return province.value && city.value ? (regions[province.value]?.[city.value] || []) : []
})

const provinceOptions = computed(() => provinces.map(p => ({ value: p, label: p })))
const cityOptions = computed(() => cities.value.map(c => ({ value: c, label: c })))
const districtOptions = computed(() => districts.value.map(d => ({ value: d, label: d })))

const preferences = ref({
  darkMode: true,
})

function applyTheme(dark) {
  document.documentElement.classList.toggle('light-mode', !dark)
  localStorage.setItem('aether-theme', dark ? 'dark' : 'light')
}

onMounted(() => {
  preferences.value.darkMode = !document.documentElement.classList.contains('light-mode')
})

watch(() => preferences.value.darkMode, (val) => {
  applyTheme(val)
})

watch(province, () => {
  if (!cities.value.includes(city.value)) {
    city.value = ''
  }
})
watch(city, () => {
  if (!districts.value.includes(district.value)) {
    district.value = ''
  }
})

// ---- Emoji 索引重建 ----
const emojiRebuilding = ref(false)
const emojiRebuildStatus = ref({ running: false, total: 0, done: 0, errors: 0, message: '' })
let emojiPollTimer = null

const emojiProgress = computed(() => {
  const s = emojiRebuildStatus.value
  if (!s.total) return 0
  return Math.round((s.done / s.total) * 100)
})

async function startEmojiRebuild() {
  try {
    emojiRebuilding.value = true
    emojiRebuildStatus.value = { running: true, total: 0, done: 0, errors: 0, message: '正在启动...' }
    const res = await fetch('/api/emoji/rebuild', { method: 'POST' })
    const data = await res.json()
    if (!res.ok) {
      emojiRebuildStatus.value.message = data.message || '启动失败'
      emojiRebuilding.value = false
      return
    }
    // 开始轮询进度
    emojiPollTimer = setInterval(pollEmojiRebuild, 2000)
  } catch (e) {
    emojiRebuildStatus.value.message = '网络错误: ' + e.message
    emojiRebuilding.value = false
  }
}

async function pollEmojiRebuild() {
  try {
    const res = await fetch('/api/emoji/rebuild/status')
    const data = await res.json()
    emojiRebuildStatus.value = data
    if (!data.running) {
      // 重建结束，停止轮询
      if (emojiPollTimer) { clearInterval(emojiPollTimer); emojiPollTimer = null }
      emojiRebuilding.value = false
    }
  } catch (e) {
    console.error('Failed to poll emoji rebuild status:', e)
  }
}

onUnmounted(() => {
  if (emojiPollTimer) { clearInterval(emojiPollTimer); emojiPollTimer = null }
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>设置</h1>
      <p class="page-sub">管理你的家庭和个人偏好</p>
    </header>

    <div class="settings-sections">
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#127968;</span>
          家庭信息
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">家庭名称</span>
            </label>
            <input v-model="homeName" class="setting-input" placeholder="我的家" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">主人称呼</span>
            </label>
            <input v-model="userName" class="setting-input" placeholder="Home" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">地点</span>
              <span class="label-desc">家庭所在省份和城市</span>
            </label>
            <div class="location-selects">
              <FlowSelect v-model="province" :options="provinceOptions" placeholder="省份" />
              <FlowSelect v-model="city" :options="cityOptions" placeholder="城市" :disabled="!province" />
              <FlowSelect v-model="district" :options="districtOptions" placeholder="区/县" :disabled="!city" />
            </div>
          </div>
        </div>
      </section>

      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#9881;</span>
          系统偏好
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">深色模式</span>
              <span class="label-desc">切换深色主题</span>
            </div>
            <BaseToggle v-model="preferences.darkMode" />
          </div>
        </div>
      </section>

      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128248;</span>
          Emoji 索引
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">向量索引重建</span>
              <span class="label-desc">
                索引文件 (emoji_index.json) 未纳入版本控制，换机器后需重建。<br />
                前提：已在「密钥管理」页配置 embed 模型 (如 BAAI/bge-m3)。
              </span>
            </div>
            <button
              class="btn-primary emoji-rebuild-btn"
              @click="startEmojiRebuild"
              :disabled="emojiRebuilding"
            >
              {{ emojiRebuilding ? '重建中...' : '重建索引' }}
            </button>
          </div>
          <div v-if="emojiRebuildStatus.message" class="emoji-rebuild-info">
            <div class="rebuild-message">{{ emojiRebuildStatus.message }}</div>
            <div v-if="emojiRebuildStatus.total > 0" class="rebuild-progress-bar">
              <div class="rebuild-progress-fill" :style="{ width: emojiProgress + '%' }"></div>
              <span class="rebuild-progress-text">
                {{ emojiRebuildStatus.done }} / {{ emojiRebuildStatus.total }}
                <span v-if="emojiRebuildStatus.errors > 0" class="rebuild-errors">
                  (失败 {{ emojiRebuildStatus.errors }})
                </span>
              </span>
            </div>
          </div>
        </div>
      </section>

      <div class="save-bar">
        <button class="btn-primary" :class="{ saved }" @click="saveHomeInfo" :disabled="saving">
          {{ saving ? '保存中...' : saved ? '已保存' : '保存设置' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setting-input {
  width: 180px;
}

.location-selects {
  display: flex;
  gap: var(--space-3);
}

.location-selects :deep(.flow-select) {
  width: 140px;
}

.location-selects :deep(.trigger) {
  font-size: var(--text-xs);
  padding: var(--space-4) var(--space-6);
}

.location-selects :deep(.trigger-text) {
  text-align: left;
}

.location-selects :deep(.option) {
  font-size: var(--text-xs);
  padding: var(--space-4) var(--space-6);
}

.emoji-rebuild-btn {
  flex-shrink: 0;
  white-space: nowrap;
}

.emoji-rebuild-info {
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border, rgba(255,255,255,0.1));
}

.rebuild-message {
  font-size: var(--text-sm);
  color: var(--color-text-secondary, #888);
  margin-bottom: var(--space-3);
}

.rebuild-progress-bar {
  position: relative;
  height: 28px;
  background: var(--color-bg-app, #1a1a1a);
  border-radius: var(--radius-md, 8px);
  overflow: hidden;
  border: 1px solid var(--color-border, rgba(255,255,255,0.1));
}

.rebuild-progress-fill {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  background: linear-gradient(90deg, #0ea5e9, #a855f7);
  transition: width 0.5s ease;
}

.rebuild-progress-text {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-xs);
  color: #fff;
  font-weight: 500;
}

.rebuild-errors {
  opacity: 0.7;
  margin-left: var(--space-2);
}

@media (max-width: 768px) {
  .setting-input {
    width: 120px;
  }

  .location-selects {
    width: 100%;
    flex-direction: column;
  }

  .location-selects :deep(.flow-select) {
    width: 100%;
  }
}
</style>
