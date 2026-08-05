<script setup>
import { ref, computed, onMounted } from 'vue'
import BaseToggle from '../components/BaseToggle.vue'
import EmojiPicker from '../components/EmojiPicker.vue'
import SensorChart from '../components/SensorChart.vue'
import { adaptControls, formatSliderValue, toActualValue } from '../utils/deviceCapabilities.js'
import { apiGet } from '../utils/api'

const entities = ref([])        // 扁平实体列表（兼容，也供 modal 内按 id 查找）
const devices = ref([])         // 设备分组（主数据源）
const services = ref({})
const loading = ref(true)
const searchQuery = ref('')
const activeArea = ref('全部')
const selectedDevice = ref(null)       // 当前打开的设备（含 entities 数组）
const selectedEntity = ref(null)       // 设备详情内当前展开的子实体
const showModal = ref(false)
const togglingDevices = ref(new Set())

const emojiPrefs = ref({})
const showEmojiPicker = ref(false)
const currentEmojiTarget = ref(null)

// 实体别名（用户自定义显示名，覆盖 HA 生成的难看名字）
const entityAliases = ref({})         // {entity_id: alias}
const editingName = ref(false)
const nameInput = ref('')

// 实体备注（用户自定义，注入 AI 认知，影响调用决策——如继电器反转语义）
const entityNotes = ref({})          // {entity_id: note}
const noteInput = ref('')
const editingNote = ref(false)

// ========================
//  Emoji preferences
// ========================

async function loadEmojiPrefs() {
  try {
    const res = await fetch('/api/emoji/preferences', { credentials: 'include' })
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
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, key, emoji_char: item.char }),
    })
    emojiPrefs.value[prefKey] = item.char
  } catch (e) {
    console.error('Failed to save emoji pref:', e)
  }
}

// ========================
//  Entity alias (用户自定义实体显示名)
// ========================

async function loadEntityAliases() {
  try {
    const res = await fetch('/api/ha/entity-aliases', { credentials: 'include' })
    const json = await res.json()
    entityAliases.value = json.data?.aliases || {}
  } catch (e) {
    console.error('Failed to load entity aliases:', e)
  }
}

function startEditName() {
  if (!selectedEntity.value) return
  nameInput.value = selectedEntity.value.name || selectedEntity.value.entity_id
  editingName.value = true
}

async function saveName() {
  if (!selectedEntity.value) return
  const eid = selectedEntity.value.entity_id
  const alias = nameInput.value.trim()
  try {
    await fetch('/api/ha/entity-aliases', {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: eid, alias }),
    })
    entityAliases.value[eid] = alias
    // 立即更新当前实体和卡片里的显示名
    selectedEntity.value.name = alias || selectedEntity.value.attributes?.friendly_name || eid
    refreshDeviceEntityName(eid, selectedEntity.value.name)
  } catch (e) {
    console.error('Failed to save entity alias:', e)
  }
  editingName.value = false
}

function resetName() {
  if (!selectedEntity.value) return
  const eid = selectedEntity.value.entity_id
  const original = selectedEntity.value.attributes?.friendly_name || eid
  nameInput.value = original
}

// ========================
//  Entity note (用户自定义备注，注入 AI 认知)
// ========================

async function loadEntityNotes() {
  try {
    const res = await fetch('/api/ha/entity-notes', { credentials: 'include' })
    const json = await res.json()
    entityNotes.value = json.data?.notes || {}
  } catch (e) {
    console.error('Failed to load entity notes:', e)
  }
}

function startEditNote() {
  if (!selectedEntity.value) return
  noteInput.value = entityNotes.value[selectedEntity.value.entity_id] || ''
  editingNote.value = true
}

async function saveNote() {
  if (!selectedEntity.value) return
  const eid = selectedEntity.value.entity_id
  const note = noteInput.value.trim()
  try {
    await fetch('/api/ha/entity-notes', {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: eid, note }),
    })
    if (note) {
      entityNotes.value[eid] = note
    } else {
      delete entityNotes.value[eid]
    }
  } catch (e) {
    console.error('Failed to save entity note:', e)
  }
  editingNote.value = false
}

function resetNote() {
  if (!selectedEntity.value) return
  noteInput.value = entityNotes.value[selectedEntity.value.entity_id] || ''
}

// 同步更新 selectedDevice.entities 里同名实体的 name（卡片即时刷新）
function refreshDeviceEntityName(entityId, newName) {
  if (!selectedDevice.value) return
  const ent = (selectedDevice.value.entities || []).find(e => e.entity_id === entityId)
  if (ent) ent.name = newName
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

// 每个域 toggle 后的乐观状态字符串（isOn 判断的依据）
const TOGGLE_STATE_MAP = {
  lock: { on: 'unlocked', off: 'locked' },
  cover: { on: 'open', off: 'closed' },
  valve: { on: 'open', off: 'closed' },
}

function getToggleService(domain, turnOn) {
  const mapped = TOGGLE_SERVICE_MAP[domain]
  if (mapped) return turnOn ? mapped.on : mapped.off
  return turnOn ? 'turn_on' : 'turn_off'
}

function getToggleState(domain, turnOn) {
  const mapped = TOGGLE_STATE_MAP[domain]
  if (mapped) return turnOn ? mapped.on : mapped.off
  return turnOn ? 'on' : 'off'
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

// 传感器等只读实体也能点击 — 进入查看数值/历史趋势
function isClickable(entity) {
  const domain = getDomain(entity.entity_id)
  return isControllable(entity) || domain === 'sensor' || domain === 'binary_sensor'
}

// 是否显示历史趋势图：sensor 必须有 unit_of_measurement（排除音频ID等无单位标识符），
// binary_sensor 的 on/off 自动转 0/1 阶梯线。
function hasHistory(entity) {
  const domain = getDomain(entity.entity_id)
  if (domain === 'binary_sensor') return true
  if (domain === 'sensor') return !!entity?.attributes?.unit_of_measurement
  return false
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
    // 只有整个 state 是单一纯数值时才走数值格式化。
    // parseFloat("192.168.4.73") = 192.168，会被错误截断成 192.17；
    // 用严格正则 ^\d+(\.\d+)?$ 只匹配单一数值（整数或一位小数），IP/版本号等字符串原样返回。
    if (/^-?\d+(\.\d+)?$/.test(state.trim())) {
      const num = parseFloat(state)
      return `${Math.round(num * 100) / 100} ${unit}`.trim()
    }
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
  const areaSet = new Set(devices.value.map(d => d.area_name || '未分组'))
  return ['全部', ...Array.from(areaSet).sort()]
})

const filteredDevices = computed(() => {
  let items = devices.value
  if (activeArea.value !== '全部') {
    items = items.filter(d => (d.area_name || '未分组') === activeArea.value)
  }
  if (searchQuery.value.trim()) {
    const q = searchQuery.value.toLowerCase()
    items = items.filter(d =>
      (d.name || '').toLowerCase().includes(q) ||
      (d.model || '').toLowerCase().includes(q) ||
      (d.entities || []).some(e =>
        (e.name || '').toLowerCase().includes(q) ||
        (e.entity_id || '').toLowerCase().includes(q))
    )
  }
  return items
})

const groupedDevices = computed(() => {
  const groups = {}
  for (const dev of filteredDevices.value) {
    const area = dev.area_name || '未分组'
    ;(groups[area] ||= []).push(dev)
  }
  return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b))
})

const stats = computed(() => ({
  online: entities.value.filter(isOn).length,
  total: entities.value.length,
}))

// ========================
//  Device-level helpers
// ========================

// 设备在线：任一子实体非 unavailable/unknown 即在线
function isDeviceOnline(dev) {
  return (dev.entities || []).some(e =>
    e.state && e.state !== 'unavailable' && e.state !== 'unknown')
}

// 设备图标：取第一个可控实体的 domain，否则第一个实体的 domain
function deviceIconDomain(dev) {
  const ents = dev.entities || []
  const ctrl = ents.find(e => isControllable(e))
  return (ctrl || ents[0] || {}).entity_id || 'default'
}

// 设备内可控实体数 / 只读实体数
function deviceControllableCount(dev) {
  return (dev.entities || []).filter(e => isControllable(e)).length
}

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
    devices.value = entitiesData.devices || []
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
      credentials: 'include',
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
  const turnOn = !isOn(entity)
  const service = getToggleService(domain, turnOn)

  togglingDevices.value.add(entityId)

  // 乐观更新：先改状态，UI 立即响应
  const oldState = entity.state
  entity.state = getToggleState(domain, turnOn)

  try {
    const res = await fetch('/api/ha/call_service', {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ domain, service, entity_id: entityId }),
    })
    const json = await res.json()
    if (!json.data?.success) {
      entity.state = oldState // 请求失败，回滚
    }
  } catch (e) {
    console.error('Failed to toggle device:', e)
    entity.state = oldState // 网络异常，回滚
  } finally {
    togglingDevices.value.delete(entityId)
  }
}

// ========================
//  Modal
// ========================

function openDeviceModal(dev) {
  selectedDevice.value = dev
  // 默认展开第一个可控实体；无可控则展开第一个
  const ents = dev.entities || []
  selectedEntity.value = ents.find(e => isControllable(e)) || ents[0] || null
  showModal.value = true
}

function closeModal() {
  showModal.value = false
  selectedDevice.value = null
  selectedEntity.value = null
}

function selectEntity(ent) {
  selectedEntity.value = ent
}

// Capabilities — 基于 selectedEntity（设备内当前展开的子实体）
const capabilities = computed(() => {
  if (!selectedEntity.value) return []
  return adaptControls(selectedEntity.value._controls, selectedEntity.value)
})

async function handleCapability(cap, value) {
  if (!selectedEntity.value) return
  const actualValue = cap.type === 'slider' ? toActualValue(cap, value) : value
  const data = { [cap.param]: actualValue }

  // 乐观更新：先改本地状态，UI 立即响应
  if (cap.type === 'enum') {
    if (cap.currentAttr === 'state') {
      selectedEntity.value.state = value
    } else {
      selectedEntity.value.attributes[cap.currentAttr] = value
    }
    const ctrl = selectedEntity.value._controls?.[cap.key]
    if (ctrl) ctrl.current = value
  } else if (cap.type === 'slider') {
    const storedValue = cap.pctMatch ? Math.round(actualValue * 255 / 100) : actualValue
    selectedEntity.value.attributes[cap.key] = storedValue
    const ctrl = selectedEntity.value._controls?.[cap.key]
    if (ctrl) ctrl.current = value
  }

  await callService(cap.service, cap.action, selectedEntity.value.entity_id, data)
}

async function handleAction(act) {
  if (!selectedEntity.value) return
  const ok = await callService(act.service, act.action, selectedEntity.value.entity_id)
  if (ok) {
    refreshSelectedEntity()
  }
}

// 刷新当前展开子实体的状态（从 HA 重新拉取整批，更新该实体）
async function refreshSelectedEntity() {
  if (!selectedEntity.value) return
  const entityId = selectedEntity.value.entity_id
  try {
    const [entitiesData, servicesData] = await Promise.all([
      apiGet('/api/ha/entities'),
      apiGet('/api/ha/services'),
    ])
    const freshEntities = entitiesData.entities || []
    // 同步扁平 entities（卡片计数等依赖）
    entities.value = freshEntities
    devices.value = entitiesData.devices || []
    const fresh = freshEntities.find(e => e.entity_id === entityId)
    if (fresh) {
      selectedEntity.value.state = fresh.state
      selectedEntity.value.attributes = fresh.attributes || {}
      selectedEntity.value._controls = fresh._controls
    }
    services.value = servicesData || {}
  } catch (e) {
    console.error('Failed to refresh entity:', e)
  }
}

// Dynamic info rows from attributes (data-driven, no hardcoded attribute names)
const dynamicInfoRows = computed(() => {
  if (!selectedEntity.value) return []
  const attrs = selectedEntity.value.attributes || {}
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

// HA 内部系统字段，不在属性表里展示
const HIDDEN_ATTRS = new Set([
  'friendly_name', 'entity_id', 'device_class', 'state_class',
  'unit_of_measurement', 'supported_features', 'assumed_state',
  'restored', 'icon', 'available_modes',
])
// 常见属性键的中文标签
const ATTR_LABELS_ZH = {
  temperature: '目标温度',
  current_temperature: '当前温度',
  current_humidity: '当前湿度',
  humidity: '目标湿度',
  target_humidity: '目标湿度',
  min_temp: '最低温度',
  max_temp: '最高温度',
  min_humidity: '最低湿度',
  max_humidity: '最高湿度',
  target_temp_step: '温度步长',
  mode: '模式',
  hvac_mode: '运行模式',
  hvac_modes: '可用模式',
  fan_mode: '风速',
  swing_mode: '扫风',
  brightness: '亮度',
  color_mode: '色彩模式',
  percentage: '百分比',
  preset_mode: '预设模式',
  position: '位置',
  current_position: '当前位置',
  battery_level: '电量',
  volume_level: '音量',
  power: '功率',
  energy: '能耗',
}

// 属性表：过滤 HA 内部字段 + 中文友好标签
const displayAttributes = computed(() => {
  if (!selectedEntity.value) return []
  const attrs = selectedEntity.value.attributes || {}
  return Object.entries(attrs)
    .filter(([key, value]) => {
      if (HIDDEN_ATTRS.has(key)) return false
      if (key.startsWith('supported_')) return false
      // min_*/max_*/*_step 边界值已在滑块里体现，不重复显示
      if (key.startsWith('min_') || key.startsWith('max_') || key.endsWith('_step')) return false
      // *_list / available_modes 等选项数组不显示
      if (key.endsWith('_list') || key.endsWith('_modes')) return false
      return true
    })
    .map(([key, value]) => ({
      key,
      label: ATTR_LABELS_ZH[key] || key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()),
      value: typeof value === 'object' ? JSON.stringify(value) : value,
    }))
})

onMounted(() => {
  loadEntities()
  loadEmojiPrefs()
  loadEntityAliases()
  loadEntityNotes()
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
      <div v-for="[area, items] in groupedDevices" :key="area" class="area-section">
        <h2 class="area-title" v-if="activeArea === '全部'">
          <span class="area-name">{{ area }}</span>
          <span class="area-count">{{ items.length }}</span>
        </h2>
        <div class="device-grid">
          <div
            v-for="dev in items"
            :key="dev.device_id"
            class="device-card"
            :class="{ on: isDeviceOnline(dev), clickable: true }"
            @click="openDeviceModal(dev)"
          >
            <div class="card-top">
              <div
                class="card-icon emoji-trigger"
                :style="{ background: getDomainIcon(deviceIconDomain(dev)).bg, color: getDomainIcon(deviceIconDomain(dev)).color }"
                @click.stop="openEmojiPicker('device', dev.device_id)"
              >{{ getDomainIcon(deviceIconDomain(dev)).icon }}</div>
            </div>
            <div class="card-body">
              <h3>{{ dev.name || dev.device_id }}</h3>
              <span class="card-room">
                {{ dev.manufacturer ? dev.manufacturer + ' · ' : '' }}{{ dev.model || (dev.area_name || '未分组') }}
              </span>
            </div>
            <div class="card-footer">
              <span class="card-spec">{{ deviceControllableCount(dev) }} 可控 · {{ dev.entity_count }} 属性</span>
              <span class="card-status" :class="{ on: isDeviceOnline(dev) }">{{ isDeviceOnline(dev) ? '在线' : '离线' }}</span>
            </div>
          </div>
        </div>
      </div>

      <div v-if="groupedDevices.length === 0" class="empty-state empty-state--card">
        {{ searchQuery || activeArea !== '全部' ? '未找到匹配的设备。' : '暂无设备数据。' }}
      </div>
    </div>

    <!-- Modal: 设备详情 -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showModal && selectedDevice" class="modal-overlay" @click.self="closeModal">
          <div class="modal-content">
            <div class="modal-header">
              <div
                class="modal-icon emoji-trigger"
                :style="{ background: getDomainIcon(deviceIconDomain(selectedDevice)).bg, color: getDomainIcon(deviceIconDomain(selectedDevice)).color }"
                @click="openEmojiPicker('device', selectedDevice.device_id)"
              >{{ getDomainIcon(deviceIconDomain(selectedDevice)).icon }}</div>
              <div class="modal-title">
                <h2>{{ selectedDevice.name || selectedDevice.device_id }}</h2>
                <span class="modal-entity-id">
                  {{ [selectedDevice.manufacturer, selectedDevice.model].filter(Boolean).join(' · ') || selectedDevice.area_name || '' }}
                </span>
              </div>
              <button class="modal-close" @click="closeModal">&times;</button>
            </div>

            <div class="modal-body">
              <!-- 子实体列表：可控优先 -->
              <div class="entity-list-section" v-if="(selectedDevice.entities || []).length">
                <h3>控制 <span class="section-count">({{ deviceControllableCount(selectedDevice) }})</span></h3>
                <div class="entity-list">
                  <div
                    v-for="ent in (selectedDevice.entities || []).filter(e => isControllable(e))"
                    :key="ent.entity_id"
                    class="entity-row"
                    :class="{ active: selectedEntity && selectedEntity.entity_id === ent.entity_id, on: isOn(ent) }"
                    @click="selectEntity(ent)"
                  >
                    <span class="entity-icon" :style="{ color: getDomainIcon(ent.entity_id).color }">{{ getDomainIcon(ent.entity_id).icon }}</span>
                    <span class="entity-name">{{ ent.name || ent.entity_id }}</span>
                    <span class="entity-state">{{ getCardPrimary(ent) }}</span>
                    <BaseToggle v-if="isToggleable(ent)" :modelValue="isOn(ent)" @click.stop @update:modelValue="toggleDevice(ent)" />
                  </div>
                </div>

                <h3 v-if="(selectedDevice.entities || []).some(e => !isControllable(e))">
                  信息 <span class="section-count">({{ (selectedDevice.entities || []).filter(e => !isControllable(e)).length }})</span>
                </h3>
                <div class="entity-list" v-if="(selectedDevice.entities || []).some(e => !isControllable(e))">
                  <div
                    v-for="ent in (selectedDevice.entities || []).filter(e => !isControllable(e))"
                    :key="ent.entity_id"
                    class="entity-row"
                    :class="{ active: selectedEntity && selectedEntity.entity_id === ent.entity_id }"
                    @click="selectEntity(ent)"
                  >
                    <span class="entity-icon" :style="{ color: getDomainIcon(ent.entity_id).color }">{{ getDomainIcon(ent.entity_id).icon }}</span>
                    <span class="entity-name">{{ ent.name || ent.entity_id }}</span>
                    <span class="entity-state">{{ getCardPrimary(ent) }}</span>
                  </div>
                </div>
              </div>

              <!-- 当前展开实体的详情 -->
              <template v-if="selectedEntity">
                <div class="info-section">
                  <div class="info-row name-row">
                    <span class="info-label">名称</span>
                    <span v-if="!editingName" class="info-value name-display" @click="startEditName" title="点击修改名称">
                      {{ selectedEntity.name || selectedEntity.entity_id }}
                      <span class="edit-hint">✎</span>
                    </span>
                    <span v-else class="name-edit">
                      <input v-model="nameInput" class="name-input" @keyup.enter="saveName" @keyup.esc="editingName = false" autofocus />
                      <button class="name-btn name-btn--save" @click="saveName">保存</button>
                      <button class="name-btn" @click="resetName">还原</button>
                    </span>
                  </div>
                  <div class="info-row note-row">
                    <span class="info-label">备注</span>
                    <span v-if="!editingNote" class="info-value note-display" @click="startEditNote" title="点击给设备写备注（影响 AI 调用，如继电器反转语义）">
                      <span v-if="entityNotes[selectedEntity.entity_id]" class="note-text">{{ entityNotes[selectedEntity.entity_id] }}</span>
                      <span v-else class="note-empty">点此添加（让 AI 理解设备怪癖）</span>
                      <span class="edit-hint">✎</span>
                    </span>
                    <span v-else class="note-edit">
                      <textarea v-model="noteInput" class="note-input" rows="3" maxlength="200" placeholder="如：继电器 ON=关门，OFF=开门；用户说开门时调 turn_off" @keyup.esc="editingNote = false"></textarea>
                      <div class="note-btns">
                        <button class="name-btn name-btn--save" @click="saveNote">保存</button>
                        <button class="name-btn" @click="resetNote">还原</button>
                      </div>
                    </span>
                  </div>
                  <div class="info-row">
                    <span class="info-label">状态</span>
                    <span class="info-value" :class="{ active: isOn(selectedEntity) }">{{ formatState(selectedEntity) }}</span>
                  </div>
                  <div class="info-row" v-for="row in dynamicInfoRows" :key="row.label">
                    <span class="info-label">{{ row.label }}</span>
                    <span class="info-value">{{ row.value }}</span>
                  </div>
                </div>

                <div class="history-section" v-if="hasHistory(selectedEntity)">
                  <h3>历史趋势</h3>
                  <SensorChart :entityId="selectedEntity.entity_id" :unit="selectedEntity.attributes?.unit_of_measurement || ''" />
                </div>

                <div class="control-section" v-if="isControllable(selectedEntity)">
                  <h3>控制</h3>

                  <div class="control-row" v-if="isToggleable(selectedEntity)">
                    <span class="control-label">开关</span>
                    <BaseToggle :modelValue="isOn(selectedEntity)" @update:modelValue="toggleDevice(selectedEntity)" />
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
                        <button v-for="act in cap.actions" :key="act.action" class="action-btn" :class="{ active: act.attrKey && selectedEntity.attributes[act.attrKey] }" @click="handleAction(act)">{{ act.label }}</button>
                      </div>
                    </div>
                  </template>
                </div>

                <div class="attributes-section" v-if="displayAttributes.length">
                  <h3>属性 ({{ displayAttributes.length }})</h3>
                  <div class="attr-table">
                    <div class="attr-row" v-for="attr in displayAttributes" :key="attr.key">
                      <span class="attr-key">{{ attr.label }}</span>
                      <span class="attr-value">{{ attr.value }}</span>
                    </div>
                  </div>
                </div>
              </template>
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
.history-section,
.attributes-section { margin-bottom: var(--space-14); }

.info-section:last-child,
.control-section:last-child,
.history-section:last-child,
.attributes-section:last-child { margin-bottom: 0; }

.info-section h3,
.control-section h3,
.history-section h3,
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

/* 设备详情 modal — 子实体列表 */
.entity-list-section h3 { margin: var(--space-16) 0 var(--space-8); }
.entity-list-section h3:first-child { margin-top: 0; }
.section-count { opacity: 0.5; font-weight: normal; font-size: var(--text-sm); }

.entity-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.entity-row {
  display: flex;
  align-items: center;
  gap: var(--space-10);
  padding: var(--space-8) var(--space-10);
  border-radius: var(--radius-md);
  cursor: pointer;
  background: rgba(255,255,255,0.02);
  transition: background var(--duration-fast) var(--ease-out);
}
.entity-row:hover { background: rgba(255,255,255,0.06); }
.entity-row.active { background: rgba(255,255,255,0.08); outline: 1px solid var(--color-border-hover); }
.entity-icon { font-size: 18px; width: 24px; text-align: center; flex-shrink: 0; }
.entity-name { flex: 1; font-size: var(--text-base); }
.entity-state {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.entity-row.on .entity-state { color: var(--color-text-secondary); }

/* 实体别名编辑 */
.name-row { align-items: center; }
.name-display {
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: var(--space-6);
}
.name-display:hover { color: var(--color-text); }
.edit-hint {
  opacity: 0;
  font-size: var(--text-sm);
  transition: opacity var(--duration-fast);
}
.name-display:hover .edit-hint { opacity: 0.6; }
.name-edit { display: inline-flex; gap: var(--space-6); align-items: center; }
.name-input {
  background: rgba(255,255,255,0.06);
  border: 1px solid var(--color-border-active);
  border-radius: var(--radius-sm);
  padding: var(--space-4) var(--space-8);
  color: var(--color-text);
  font-size: var(--text-base);
  font-family: inherit;
  outline: none;
  min-width: 180px;
}
.name-btn {
  background: rgba(255,255,255,0.06);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-sm);
  padding: var(--space-4) var(--space-10);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: var(--text-sm);
  font-family: inherit;
}
.name-btn:hover { border-color: var(--color-border-active); color: var(--color-text); }
.name-btn--save {
  background: var(--color-accent, rgba(52,152,219,0.2));
  border-color: transparent;
  color: var(--color-text);
}

/* 实体备注编辑 */
.note-row {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}
.note-display {
  cursor: pointer;
  padding: 6px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  min-height: 24px;
  white-space: pre-wrap;
  word-break: break-all;
}
.note-display:hover {
  background: rgba(255, 255, 255, 0.06);
}
.note-text {
  display: inline;
}
.note-empty {
  color: var(--text-muted, #888);
  font-size: 0.9em;
}
.note-input {
  width: 100%;
  background: var(--bg-input, rgba(0, 0, 0, 0.2));
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.1));
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
  font-size: 0.92em;
  resize: vertical;
  font-family: inherit;
}
.note-btns {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
</style>
