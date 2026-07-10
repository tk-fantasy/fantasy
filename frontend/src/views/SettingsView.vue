<script setup>
import { ref, computed, watch, onMounted } from 'vue'
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
