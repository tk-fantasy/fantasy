<script setup>
import { ref, computed, onMounted } from 'vue'
import BaseToggle from '../components/BaseToggle.vue'
import EmojiPicker from '../components/EmojiPicker.vue'
import { adaptControls, formatSliderValue, toActualValue } from '../utils/deviceCapabilities.js'
import { apiGet } from '../utils/api'

const entities = ref([])
const services = ref({})
const loading = ref(true)
const searchQuery = ref('')
const activeArea = ref('全部')
const selectedDevice = ref(null)
const showModal = ref(false)
const togglingDevices = ref(new Set())

const emojiPrefs = ref({})
const showEmojiPicker = ref(false)
const currentEmojiTarget = ref(null)

// ========================
//  Emoji preferences
// ========================

async function loadEmojiPrefs() {
  try {
    const res = await fetch('/api/emoji/preferences')
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

// ========================
//  Entity domain / state helpers
//  Inspired by HA frontend: computeDomain, turnOnOffEntity, computeStateDisplay
// ========================

function getDomain(entityId) {
  return entityId?.split('.')[0] || 'default'
}

// HA pattern: turn-on-off-entity.ts → switch on domain to find correct service
const TOGGLE_SERVICE_MAP = {
  lock: { on: 'unlock', off: 'lock' },
  cover: { on: 'open_cover', off: 'close_cover' },
  valve: { on: 'open_valve', off: 'close_valve' },
}

function getToggleService(domain, turnOn) {
  const mapped = TOGGLE_SERVICE_MAP[domain]
  if (mapped) return turnOn ? mapped.on : mapped.off
  return turnOn ? 'turn_on' : 'turn_off'
}

// HA pattern: isOn → check if state is considered "active"
function isOn(entity) {
  const state = entity.state
  if (!state || state === 'unavailable' || state === 'unknown') return false
  return state !== 'off' && state !== 'closed' && state !== 'locked'
    && state !== 'docked' && state !== 'idle' && state !== 'paused'
    && state !== 'standby'
}

// Derive controllability from available services (no hardcoded domain list)
function isToggleable(entity) {
  const domain = getDomain(entity.entity_id)
  const svc = services.value?.[domain] || {}
  return 'turn_on' in svc || 'turn_off' in svc || 'toggle' in svc
    || 'lock' in svc || 'open_cover' in svc || 'open_valve' in svc
}

function isControllable(entity) {
  const domain = getDomain(entity.entity_id)
  const svc = services.value?.[domain] || {}
  return Object.keys(svc).length > 0
}

// ========================
//  Card rendering – follows HA official state-display.ts patterns
//  DEFAULT_STATE_CONTENT_DOMAINS + computeStateDisplay
// ========================

// HA: DEFAULT_STATE_CONTENT_DOMAINS — per-domain primary content
const DOMAIN_PRIMARY_CONTENT = {
  climate: ['state', 'current_temperature'],
  cover:   ['state', 'current_position'],
  fan:     'percentage',
  humidifier: ['state', 'current_humidity'],
  light:   'brightness',
  timer:   'remaining_time',
  update:  'install_status',
  valve:   ['state', 'current_position'],
  water_heater: ['state', 'current_temperature'],
}

function fmtNum(v, unit) {
  if (v == null || isNaN(v)) return ''
  const n = Math.round(v * 100) / 100
  return unit ? `${n} ${unit}` : `${n}`
}

// HA computeStateDisplay — format a single attribute / state value
function formatEntityAttr(entity, attrName) {
  const attrs = entity.attributes || {}
  const domain = getDomain(entity.entity_id)
  const unit = attrs.unit_of_measurement || ''

  if (attrName === 'state') {
    const state = entity.state
    if (!state || state === 'unavailable') return '离线'
    if (state === 'unknown') return '未知'
    return formatState(entity)
  }

  if (attrName === 'current_temperature' || attrName === 'temperature') {
    const v = attrs[attrName]
    if (v != null) return fmtNum(v, unit || '°C')
    return ''
  }
  if (attrName === 'current_humidity' || attrName === 'humidity') {
    const v = attrs[attrName]
    if (v != null) return `${v}%`
    return ''
  }
  if (attrName === 'current_position') {
    const v = attrs[attrName]
    if (v != null) return `${v}%`
    return ''
  }
  if (attrName === 'brightness') {
    const v = attrs.brightness
    if (v != null) return `${Math.round(v * 100 / 255)}%`
    return ''
  }
  if (attrName === 'percentage') {
    const v = attrs.percentage
    if (v != null) return `${v}%`
    return ''
  }

  const v = attrs[attrName]
  if (v == null) return ''
  if (typeof v === 'number') return fmtNum(v, '')
  return String(v)
}

// HA localized state text (domain-specific)
function formatState(entity) {
  const state = entity.state
  const domain = getDomain(entity.entity_id)
  const attrs = entity.attributes || {}
  const unit = attrs.unit_of_measurement || ''

  if (domain === 'binary_sensor') {
    const classMap = { motion: '移动检测', door: '门磁', window: '窗磁', smoke: '烟雾', moisture: '漏水', occupancy: '有人', opening: '开关', vibration: '震动', gas: '燃气', carbon_monoxide: '一氧化碳', problem: '故障', safety: '安全', presence: '存在', running: '运行中', sound: '声音', tamper: '防拆', power: '电源', connectivity: '连接', lock: '锁定', plug: '插入', battery: '电池', cold: '低温', heat: '高温', light: '光照' }
    const label = attrs.device_class ? (classMap[attrs.device_class] || attrs.device_class) : '状态'
    return `${label}: ${state === 'on' ? '触发' : '正常'}`
  }

  if (domain === 'sensor') {
    const num = parseFloat(state)
    if (!isNaN(num)) return `${Math.round(num * 100) / 100} ${unit}`.trim()
    return state
  }
  if (domain === 'climate') {
    if (attrs.hvac_action) return attrs.hvac_action
    return state
  }
  if (domain === 'media_player') {
    const map = { playing: '播放中', paused: '已暂停', idle: '空闲', off: '已关闭', on: '待机', standby: '待机', buffering: '缓冲中' }
    return map[state] || state
  }
  if (domain === 'cover') {
    const map = { open: '已打开', opened: '已打开', closed: '已关闭', closing: '关闭中', opening: '打开中' }
    return map[state] || state
  }
  if (domain === 'lock') {
    const map = { locked: '已锁定', unlocked: '已解锁', unlocking: '解锁中', locking: '锁定中', jammed: '卡住' }
    return map[state] || state
  }
  if (domain === 'person') {
    const map = { home: '在家', not_home: '离家' }
    return map[state] || state
  }
  if (domain === 'sun') {
    return state === 'above_horizon' ? '日出' : '日落'
  }
  if (domain === 'alarm_control_panel') {
    const map = { armed_home: '居家设防', armed_away: '离家设防', armed_night: '夜间设防', armed_vacation: '度假设防', armed_custom_bypass: '部分设防', disarmed: '已撤防', pending: '等待中', triggered: '已触发' }
    return map[state] || state
  }
  if (domain === 'vacuum') {
    const map = { cleaning: '清扫中', docked: '已回充', idle: '空闲', paused: '已暂停', returning: '返回中', error: '故障' }
    return map[state] || state
  }
  if (domain === 'weather') {
    const map = { sunny: '晴', partlycloudy: '多云', cloudy: '阴', rainy: '雨', snowy: '雪', windy: '风', fog: '雾', hail: '冰雹', lightning: '雷电', pouring: '暴雨', 'clear-night': '晴夜', 'partly-cloudy-night': '多云夜', exceptional: '异常' }
    return map[state] || state
  }
  if (domain === 'device_tracker') {
    return state === 'home' ? '在家' : '离家'
  }
  if (domain === 'update') {
    const map = { on: '有更新', off: '最新', installing: '安装中' }
    return map[state] || state
  }
  if (domain === 'water_heater') {
    const map = { electric: '电热', gas: '燃气', heat_pump: '热泵', eco: '节能', performance: '性能', off: '关闭' }
    return map[state] || state
  }

  return state
}

// HA tile card primary display — follows DEFAULT_STATE_CONTENT_DOMAINS
function getCardPrimary(entity) {
  const domain = getDomain(entity.entity_id)
  const content = DOMAIN_PRIMARY_CONTENT[domain]

  if (content) {
    const items = Array.isArray(content) ? content : [content]
    return items.map(c => formatEntityAttr(entity, c)).filter(Boolean).join(' · ')
  }

  return formatEntityAttr(entity, 'state')
}

// HA tile card secondary display — shows extra useful info in footer
function getCardSecondary(entity) {
  const attrs = entity.attributes || {}
  const domain = getDomain(entity.entity_id)

  if (domain === 'light') {
    if (attrs.color_mode) return attrs.color_mode
    return ''
  }
  if (domain === 'media_player') {
    const parts = []
    if (attrs.volume_level != null) parts.push(`🔊${Math.round(attrs.volume_level * 100)}%`)
    if (attrs.source) parts.push(attrs.source)
    return parts.join(' · ')
  }
  if (domain === 'fan') {
    if (attrs.preset_mode) return attrs.preset_mode
    if (attrs.percentage != null) return `${attrs.percentage}%`
    return ''
  }
  if (domain === 'climate') {
    const parts = []
    if (attrs.temperature != null) parts.push(`${attrs.temperature}${attrs.unit_of_measurement || '°C'}`)
    if (attrs.preset_mode) parts.push(attrs.preset_mode)
    return parts.join(' · ')
  }
  if (domain === 'vacuum') {
    if (attrs.battery_level != null) return `电池 ${attrs.battery_level}%`
    return ''
  }
  if (domain === 'sun') {
    if (attrs.elevation != null) return `${Math.round(attrs.elevation)}°`
    return ''
  }
  if (domain === 'sensor' || domain === 'binary_sensor') return ''

  if (attrs.battery_level != null) return `电池 ${attrs.battery_level}%`

  return ''
}

// Domain icon — simple fallback to domain name if not in preset
const DOMAIN_ICONS = {
  light: '💡', switch: '⚡', sensor: '📊', binary_sensor: '🔔', climate: '🌡️',
  cover: '🪟', camera: '📷', lock: '🔐', media_player: '🎵', fan: '💨',
  vacuum: '🧹', input_boolean: '🔘', scene: '🎬', script: '📜', automation: '⚙️',
  button: '🔳', number: '🔢', select: '📋', text: '📝', time: '🕐',
  date: '📅', datetime: '📆', weather: '🌤️', alarm_control_panel: '🛡️',
  update: '⬆️', device_tracker: '📍', person: '👤', zone: '🗺️', sun: '☀️',
  water_heater: '🔥', humidifier: '💧', remote: '📱', notify: '📬',
  counter: '🔢', input_number: '🎚️', input_text: '📝', input_select: '📋',
  input_datetime: '📆', timer: '⏱️', schedule: '📅', tag: '🏷️', event: '📡',
  image: '🖼️', lawn_mower: '🌿', valve: '🚿',
  default: '●',
}
const DOMAIN_BG = {
  light: 'rgba(255,193,7,0.15)', switch: 'rgba(243,156,18,0.15)',
  sensor: 'rgba(52,152,219,0.15)', binary_sensor: 'rgba(231,76,60,0.15)',
  climate: 'rgba(26,188,156,0.15)', cover: 'rgba(155,89,182,0.15)',
  camera: 'rgba(52,152,219,0.15)', lock: 'rgba(231,76,60,0.15)',
  media_player: 'rgba(155,89,182,0.15)', fan: 'rgba(46,204,113,0.15)',
  vacuum: 'rgba(46,204,113,0.15)', input_boolean: 'rgba(243,156,18,0.15)',
  default: 'rgba(255,255,255,0.08)',
}
const DOMAIN_COLOR = {
  light: '#f0c040', switch: '#f4d03f', sensor: '#5dade2', binary_sensor: '#ec7063',
  climate: '#48c9b0', cover: '#af7ac5', camera: '#5dade2', lock: '#ec7063',
  media_player: '#af7ac5', fan: '#58d68d', vacuum: '#58d68d',
  input_boolean: '#f4d03f',
  default: 'var(--color-text-tertiary)',
}

function getDomainIcon(entity) {
  const domain = getDomain(entity.entity_id || entity)
  const entityId = entity.entity_id || entity
  // 先查 per-entity emoji，没设则 fallback 到 domain 默认
  const customEmoji = emojiPrefs.value[`entity:${entityId}`] || emojiPrefs.value[`domain:${domain}`]
  const icon = customEmoji || DOMAIN_ICONS[domain] || DOMAIN_ICONS.default
  const bg = DOMAIN_BG[domain] || DOMAIN_BG.default
  const color = DOMAIN_COLOR[domain] || DOMAIN_COLOR.default
  return { icon, bg, color }
}

// ========================
//  Entity list: filtering, grouping, stats
// ========================

const areas = computed(() => {
  const areaSet = new Set(entities.value.map(e => e.area_name || '未分组'))
  return ['全部', ...Array.from(areaSet).sort()]
})

const filteredEntities = computed(() => {
  let items = entities.value
  if (activeArea.value !== '全部') {
    items = items.filter(e => (e.area_name || '未分组') === activeArea.value)
  }
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.toLowerCase()
    items = items.filter(e =>
      (e.name || '').toLowerCase().includes(q) ||
      (e.entity_id || '').toLowerCase().includes(q)
    )
  }
  return items
})

const groupedEntities = computed(() => {
  const groups = {}
  for (const entity of filteredEntities.value) {
    const area = entity.area_name || '未分组'
    if (!groups[area]) groups[area] = []
    groups[area].push(entity)
  }
  return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b))
})

const stats = computed(() => ({
  online: entities.value.filter(isOn).length,
  total: entities.value.length,
}))

// ========================
//  Data loading
// ========================

async function loadEntities() {
  try {
    loading.value = true
    const [entitiesData, servicesData] = await Promise.all([
      apiGet('/api/ha/entities'),
      apiGet('/api/ha/services'),
    ])
    entities.value = entitiesData.entities || entitiesData || []
    services.value = servicesData || {}
  } catch (e) {
    console.error('Failed to load entities:', e)
  } finally {
    loading.value = false
  }
}

// ========================
//  Service calls
// ========================

async function callService(domain, service, entityId, data = {}) {
  try {
    const res = await fetch('/api/ha/call_service', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain, service, entity_id: entityId, data }),
    })
    const json = await res.json()
    if (json.data?.success) {
      return true
    }
    return false
  } catch (e) {
    console.error('Failed to call service:', e)
    return false
  }
}

// HA pattern: toggle → use correct service per domain
async function toggleDevice(entity) {
  const domain = getDomain(entity.entity_id)
  const entityId = entity.entity_id
  const service = getToggleService(domain, !isOn(entity))

  togglingDevices.value.add(entityId)
  try {
    const res = await fetch('/api/ha/call_service', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain, service, entity_id: entityId }),
    })
    const json = await res.json()
    if (json.data?.success) {
      entity.state = isOn(entity) ? 'off' : 'on'
    }
  } catch (e) {
    console.error('Failed to toggle device:', e)
  } finally {
    togglingDevices.value.delete(entityId)
  }
}

// ========================
//  Modal
// ========================

function openDeviceModal(entity) {
  selectedDevice.value = entity
  showModal.value = true
}

function closeModal() {
  showModal.value = false
  selectedDevice.value = null
}

// Capabilities — 复用后端 _controls（已由 entity_controls.py 在 /api/ha/entities 时附加）
const capabilities = computed(() => {
  if (!selectedDevice.value) return []
  return adaptControls(selectedDevice.value._controls, selectedDevice.value)
})

async function handleCapability(cap, value) {
  if (!selectedDevice.value) return
  const actualValue = cap.type === 'slider' ? toActualValue(cap, value) : value
  const data = { [cap.param]: actualValue }
  await callService(cap.service, cap.action, selectedDevice.value.entity_id, data)
  if (cap.type === 'enum') {
    if (cap.currentAttr === 'state') {
      selectedDevice.value.state = value
    } else {
      selectedDevice.value.attributes[cap.currentAttr] = value
    }
  } else if (cap.type === 'slider') {
    const storedValue = cap.pctMatch ? Math.round(actualValue * 255 / 100) : actualValue
    selectedDevice.value.attributes[cap.key] = storedValue
  }
}

async function handleAction(act) {
  if (!selectedDevice.value) return
  const ok = await callService(act.service, act.action, selectedDevice.value.entity_id)
  if (ok) {
    // action 类型没有本地属性可更新，刷新设备列表拿 HA 最新状态
    await refreshSelectedDevice()
  }
}

// 刷新当前选中设备的状态（从 HA 重新拉取）
async function refreshSelectedDevice() {
  if (!selectedDevice.value) return
  const entityId = selectedDevice.value.entity_id
  try {
    const [entitiesData, servicesData] = await Promise.all([
      apiGet('/api/ha/entities'),
      apiGet('/api/ha/services'),
    ])
    const freshEntities = entitiesData.entities || entitiesData || []
    const fresh = freshEntities.find(e => e.entity_id === entityId)
    if (fresh) {
      // 用新数据替换 selectedDevice 的 state 和 attributes，保持引用不变
      selectedDevice.value.state = fresh.state
      selectedDevice.value.attributes = fresh.attributes || {}
    }
    services.value = servicesData || {}
  } catch (e) {
    console.error('Failed to refresh device:', e)
  }
}

// Dynamic info rows from attributes (data-driven, no hardcoded attribute names)
const dynamicInfoRows = computed(() => {
  if (!selectedDevice.value) return []
  const attrs = selectedDevice.value.attributes || {}
  const rows = []

  for (const [key, value] of Object.entries(attrs)) {
    if (typeof value === 'object' || key === 'friendly_name' || key.startsWith('supported_')) continue
    if (key.startsWith('current_') || key === 'temperature' || key === 'humidity' || key === 'pressure' || key === 'battery_level' || key === 'volume_level') {
      const unit = attrs.unit_of_measurement || ''
      const label = key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
      rows.push({ label, value: typeof value === 'number' ? `${value}${unit}` : value })
    }
  }

  return rows
})

onMounted(() => {
  loadEntities()
  loadEmojiPrefs()
})
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div class="header-left">
        <h1>智能设备</h1>
        <p class="page-sub">{{ stats.online }} 台设备在线 / {{ stats.total }} 台</p>
      </div>
    </header>

    <div class="search-bar">
      <input v-model="searchQuery" class="search-input" placeholder="搜索设备名称..." />
    </div>

    <div class="area-tabs">
      <button v-for="area in areas" :key="area" class="area-tab" :class="{ active: activeArea === area }" @click="activeArea = area">{{ area }}</button>
    </div>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="area-groups">
      <div v-for="[area, items] in groupedEntities" :key="area" class="area-section">
        <h2 class="area-title" v-if="activeArea === '全部'">
          <span class="area-name">{{ area }}</span>
          <span class="area-count">{{ items.length }}</span>
        </h2>
        <div class="device-grid">
          <div
            v-for="entity in items"
            :key="entity.entity_id"
            class="device-card"
            :class="{ on: isOn(entity), clickable: isControllable(entity) }"
            @click="isControllable(entity) && openDeviceModal(entity)"
          >
            <div class="card-top">
              <div
                class="card-icon emoji-trigger"
                :style="{ background: getDomainIcon(entity).bg, color: getDomainIcon(entity).color }"
                @click.stop="openEmojiPicker('entity', entity.entity_id)"
              >{{ getDomainIcon(entity).icon }}</div>
            </div>
            <div class="card-body">
              <h3>{{ entity.name || entity.entity_id }}</h3>
              <span class="card-room">{{ entity.area_name || '未分组' }}</span>
            </div>
            <div class="card-footer">
              <span class="card-spec">{{ getCardSecondary(entity) }}</span>
              <span class="card-status" :class="{ on: isOn(entity) }">{{ getCardPrimary(entity) }}</span>
            </div>
            <div class="ctrl-badge" v-if="isControllable(entity)">可控</div>
          </div>
        </div>
      </div>

      <div v-if="groupedEntities.length === 0" class="empty-state empty-state--card">
        {{ searchQuery || activeArea !== '全部' ? '未找到匹配的设备。' : '暂无设备数据。' }}
      </div>
    </div>

    <!-- Modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showModal && selectedDevice" class="modal-overlay" @click.self="closeModal">
          <div class="modal-content">
            <div class="modal-header">
              <div
                class="modal-icon emoji-trigger"
                :style="{ background: getDomainIcon(selectedDevice).bg, color: getDomainIcon(selectedDevice).color }"
                @click="openEmojiPicker('entity', selectedDevice.entity_id)"
              >{{ getDomainIcon(selectedDevice).icon }}</div>
              <div class="modal-title">
                <h2>{{ selectedDevice.name || selectedDevice.entity_id }}</h2>
                <span class="modal-entity-id">{{ selectedDevice.entity_id }}</span>
              </div>
              <button class="modal-close" @click="closeModal">&times;</button>
            </div>

            <div class="modal-body">
              <div class="info-section">
                <div class="info-row">
                  <span class="info-label">状态</span>
                  <span class="info-value" :class="{ active: isOn(selectedDevice) }">{{ formatState(selectedDevice) }}</span>
                </div>
                <div class="info-row" v-if="selectedDevice.area_name">
                  <span class="info-label">区域</span>
                  <span class="info-value">{{ selectedDevice.area_name }}</span>
                </div>
                <div class="info-row" v-for="row in dynamicInfoRows" :key="row.label">
                  <span class="info-label">{{ row.label }}</span>
                  <span class="info-value">{{ row.value }}</span>
                </div>
              </div>

              <div class="control-section" v-if="isControllable(selectedDevice)">
                <h3>控制</h3>

                <div class="control-row" v-if="isToggleable(selectedDevice)">
                  <span class="control-label">开关</span>
                  <BaseToggle :modelValue="isOn(selectedDevice)" @update:modelValue="toggleDevice(selectedDevice)" />
                </div>

                <template v-for="cap in capabilities" :key="cap.key">
                  <div class="control-row" v-if="cap.type === 'enum'">
                    <span class="control-label">{{ cap.label }}</span>
                    <div class="mode-buttons">
                      <button v-for="opt in cap.options" :key="opt" class="mode-btn" :class="{ active: opt === cap.current }" @click="handleCapability(cap, opt)">{{ opt }}</button>
                    </div>
                  </div>

                  <div class="control-row" v-if="cap.type === 'slider'">
                    <span class="control-label">{{ cap.label }}</span>
                    <div class="slider-container">
                      <input type="range" :min="cap.min" :max="cap.max" :step="cap.step" :value="cap.current" @input="handleCapability(cap, parseFloat($event.target.value))" class="slider" />
                      <span class="slider-value">{{ formatSliderValue(cap) }}</span>
                    </div>
                  </div>

                  <div class="control-row" v-if="cap.type === 'action'">
                    <span class="control-label">{{ cap.label }}</span>
                    <div class="action-buttons">
                      <button v-for="act in cap.actions" :key="act.action" class="action-btn" :class="{ active: act.attrKey && selectedDevice.attributes[act.attrKey] }" @click="handleAction(act)">{{ act.label }}</button>
                    </div>
                  </div>
                </template>
              </div>

              <div class="attributes-section">
                <h3>属性 ({{ Object.keys(selectedDevice.attributes || {}).length }})</h3>
                <div class="attr-table">
                  <div class="attr-row" v-for="(value, key) in selectedDevice.attributes" :key="key">
                    <span class="attr-key">{{ key }}</span>
                    <span class="attr-value">{{ typeof value === 'object' ? JSON.stringify(value) : value }}</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <EmojiPicker :visible="showEmojiPicker" @update:visible="showEmojiPicker = $event" @select="onEmojiSelect" />
  </div>
</template>

<style scoped>
.search-bar { margin-bottom: var(--space-16); }

.search-input {
  width: 100%;
  max-width: 400px;
  padding: var(--space-5) var(--space-10);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  font-size: var(--text-base);
  font-family: inherit;
  outline: none;
  background: rgba(255,255,255,0.04);
  color: var(--color-text);
  transition: border-color var(--duration-normal) var(--ease-out);
}

.search-input:focus {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74,124,112,0.1);
}

.search-input::placeholder { color: var(--color-text-muted); }

.area-tabs {
  display: flex;
  gap: var(--space-3);
  margin-bottom: var(--space-16);
  flex-wrap: wrap;
}

.area-tab {
  padding: var(--space-3) var(--space-12);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-tertiary);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.area-tab.active {
  background: var(--color-primary-light);
  border-color: var(--color-border-active);
  color: var(--color-primary);
  font-weight: var(--weight-semibold);
}

.area-tab:not(.active):hover {
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-color: var(--color-border-hover);
}

.area-groups {
  display: flex;
  flex-direction: column;
  gap: var(--space-24);
}

.area-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-8);
}

.area-title {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text-secondary);
  letter-spacing: 0.3px;
  padding: 0 var(--space-2);
}

.area-name { color: var(--color-text); }

.area-count {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  color: var(--color-text-muted);
  background: rgba(255,255,255,0.06);
  padding: var(--space-1) var(--space-4);
  border-radius: var(--radius-full);
}

.device-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
  gap: var(--space-10);
}

.device-card {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  padding: var(--space-14);
  border: 1px solid var(--color-border);
  transition: all var(--duration-normal) var(--ease-out);
  position: relative;
  animation: cardIn 0.5s var(--ease-out) forwards;
}

.device-card.clickable { cursor: pointer; }

.device-card.clickable:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-active);
  transform: translateY(-3px);
  box-shadow: var(--elevation-3);
}

.device-card.on { border-color: rgba(74,124,112,0.1); }

@keyframes cardIn {
  from { opacity: 0; transform: translateY(var(--space-6)); }
  to { opacity: 1; transform: translateY(0); }
}

.card-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: var(--space-10);
}

.card-icon {
  width: 44px;
  height: 44px;
  border-radius: var(--radius-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-xl);
}

.card-icon.emoji-trigger,
.modal-icon.emoji-trigger {
  cursor: pointer;
  transition: transform var(--duration-fast), box-shadow var(--duration-fast);
}

.card-icon.emoji-trigger:hover,
.modal-icon.emoji-trigger:hover {
  transform: scale(1.1);
  box-shadow: 0 0 0 2px var(--color-primary-light);
}

.card-body h3 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  margin-bottom: var(--space-1);
  color: var(--color-text);
}

.card-room {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.card-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: var(--space-10);
  padding-top: var(--space-6);
  border-top: 1px solid var(--color-border);
}

.card-spec {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text-secondary);
}

.card-status {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-full);
  color: var(--color-text-muted);
  background: rgba(255,255,255,0.04);
}

.card-status.on {
  color: var(--color-success);
  background: var(--color-success-bg);
}

.ctrl-badge {
  position: absolute;
  top: var(--space-6);
  right: var(--space-6);
  font-size: 10px;
  color: var(--color-primary);
  background: var(--color-primary-light);
  border: 1px solid rgba(74,124,112,0.25);
  border-radius: var(--radius-sm);
  padding: 1px 6px;
}

/* === Modal === */
.modal-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-lg);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: var(--text-2xl);
  flex-shrink: 0;
}

.modal-title { flex: 1; min-width: 0; }

.modal-title h2 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.modal-entity-id {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  font-family: 'Cascadia Code', 'Fira Code', monospace;
}

.info-section,
.control-section,
.attributes-section { margin-bottom: var(--space-14); }

.info-section:last-child,
.control-section:last-child,
.attributes-section:last-child { margin-bottom: 0; }

.info-section h3,
.control-section h3,
.attributes-section h3 {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-6);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.info-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-4) 0;
  border-bottom: 1px solid var(--color-border);
}

.info-row:last-child { border-bottom: none; }

.info-label {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}

.info-value {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
}

.info-value.active { color: var(--color-success); }

.control-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-5) 0;
  gap: var(--space-8);
}

.control-label {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  min-width: 60px;
}

.slider-container {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  flex: 1;
}

.slider {
  flex: 1;
  -webkit-appearance: none;
  appearance: none;
  height: 6px;
  border-radius: 3px;
  background: var(--color-surface-hover);
  outline: none;
}

.slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--color-primary);
  cursor: pointer;
  box-shadow: 0 2px 6px rgba(74,124,112,0.3);
}

.slider-value {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
  min-width: 45px;
  text-align: right;
}

.mode-buttons {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.mode-btn {
  padding: var(--space-3) var(--space-8);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
  text-transform: capitalize;
}

.mode-btn:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
}

.mode-btn.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
  font-weight: var(--weight-semibold);
}

.action-buttons {
  display: flex;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.action-btn {
  padding: var(--space-4) var(--space-12);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.action-btn:hover {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.action-btn.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.attr-table {
  background: var(--color-surface);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  overflow: hidden;
}

.attr-row {
  display: flex;
  padding: var(--space-4) var(--space-8);
  border-bottom: 1px solid var(--color-border);
  font-size: var(--text-xs);
}

.attr-row:last-child { border-bottom: none; }

.attr-key {
  color: var(--color-text-muted);
  min-width: 120px;
  flex-shrink: 0;
}

.attr-value {
  color: var(--color-text-secondary);
  word-break: break-all;
  font-family: 'Cascadia Code', 'Fira Code', monospace;
}

.modal-enter-active,
.modal-leave-active { transition: all 0.3s var(--ease-out); }

.modal-enter-active .modal-content,
.modal-leave-active .modal-content { transition: all 0.3s var(--ease-out); }

.modal-enter-from,
.modal-leave-to { opacity: 0; }

.modal-enter-from .modal-content,
.modal-leave-to .modal-content { transform: scale(0.95) translateY(20px); }

@media (max-width: 900px) {
  .device-grid { grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); }
}

@media (max-width: 768px) {
  .device-grid { grid-template-columns: 1fr; }
  .search-input { max-width: 100%; }
  .modal-content { max-width: 100%; max-height: 90vh; }
}
</style>
