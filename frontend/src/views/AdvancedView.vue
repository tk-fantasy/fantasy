<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import FlowSelect from '../components/FlowSelect.vue'
import AdvancedModal from '../components/AdvancedModal.vue'
import BaseToggle from '../components/BaseToggle.vue'
import { apiGet, apiPost } from '../utils/api'
import { EGRESS_MODES, useEgressMode } from '../composables/useEgressMode'

// ===== Modal 管理 =====
const activeModal = ref(null) // 'weather' | 'exa' | 'camparams' | 'ha' | 'unique' | 'keys' | 'automation' | 'egress'

const modalTitle = computed(() => {
  const titles = {
    weather: '天气 API（和风天气）',
    exa: '网页搜索（Exa）',
    camparams: '摄像头参数',
    ha: 'Home Assistant',
    unique: '助手角色',
    keys: 'API Keys',
    automation: '自动化',
    egress: '数据出网模式',
  }
  return titles[activeModal.value] || ''
})

function openModal(section) {
  activeModal.value = section
}

function closeModal() {
  activeModal.value = null
}

// ===== 各配置数据 =====
const loading = ref(true)

// 天气 API
const weatherConfig = ref({
  host: '',
  kid: '',
  sub: '',
  private_key: '',
  has_private_key: false,
})

// Exa 搜索
const webSearchConfig = ref({
  exa: { api_key: '' },
})

// 视觉参数 — 保留全部字段回传（避免 pydantic 默认值覆盖），模板只展示部分
const visionConfig = ref({
  downscale_max_side: 448,
  jpeg_quality: 70,
  motion_hash_size: 16,
  motion_threshold: 15,
  motion_check_interval_seconds: 0.2,
  min_infer_interval_seconds: 3.0,
  max_idle_interval_seconds: 60.0,
  vision_use_img_count: 3,
  frame_interval_ms: 1000,
})

// 自动化（dhash 事件触发(仅视觉) + 视觉/非视觉双静默兜底；dhash 阈值滑块留 P1）
const automationConfig = ref({
  silent_eval_enabled: true,
  silent_eval_interval_seconds: 60,
  nonvision_silent_enabled: true,
  nonvision_silent_interval_seconds: 30,
  default_cooldown_seconds: 5,
  motion_threshold: 15,
  motion_threshold_max: 256,
  min_trigger_interval: 3.0,
  camera_vl_display_enabled: true,
  running: false,
  eval_count: 0,
  nonvision_eval_count: 0,
})
const automationSaving = ref(false)
const automationSaved = ref(false)

// 自动化数字输入:即时校验(非法红字提示,阻止保存) + 回车/失焦即生效(未变化不重发)
const makeValidator = (min, max) => (v) => Number.isInteger(v) && v >= min && v <= max
const isValidInterval = makeValidator(5, 3600)
const isValidCooldown = makeValidator(1, 3600)
const isValidThreshold = (v) => makeValidator(1, automationConfig.value.motion_threshold_max)(v)
const silentIntervalValid = computed(() => isValidInterval(automationConfig.value.silent_eval_interval_seconds))
const nonvisionIntervalValid = computed(() => isValidInterval(automationConfig.value.nonvision_silent_interval_seconds))
const cooldownValid = computed(() => isValidCooldown(automationConfig.value.default_cooldown_seconds))
const thresholdValid = computed(() => isValidThreshold(automationConfig.value.motion_threshold))
const automationFieldsValid = () =>
  silentIntervalValid.value && nonvisionIntervalValid.value && cooldownValid.value && thresholdValid.value
// 各字段最近一次已生效的值(键→发送体构造器)
const appliedValues = ref({ vision: 60, nonvision: 30, cooldown: 5, threshold: 15 })
const appliedFeedback = ref({ vision: false, nonvision: false, cooldown: false, threshold: false })

async function applySilentInterval(scope) {
  const key = scope === 'nonvision' ? 'nonvision_silent_interval_seconds' : 'silent_eval_interval_seconds'
  const value = automationConfig.value[key]
  if (!isValidInterval(value) || value === appliedValues.value[scope]) return
  await applyAutomationField(scope, '/api/automation/silent', { interval_seconds: value, scope }, value)
}

async function applyCooldown() {
  const value = automationConfig.value.default_cooldown_seconds
  if (!isValidCooldown(value) || value === appliedValues.value.cooldown) return
  await applyAutomationField('cooldown', '/api/automation/cooldown', { cooldown_seconds: value }, value)
}

async function applyThreshold() {
  const value = automationConfig.value.motion_threshold
  if (!isValidThreshold(value) || value === appliedValues.value.threshold) return
  await applyAutomationField('threshold', '/api/automation/dhash-threshold', { threshold: value }, value)
}

async function applyAutomationField(key, endpoint, body, value) {
  try {
    await apiPost(endpoint, body)
    appliedValues.value[key] = value
    appliedFeedback.value[key] = true
    setTimeout(() => { appliedFeedback.value[key] = false }, 1500)
  } catch (e) {
    console.error('Failed to apply automation field:', key, e)
  }
}

// HA 配置
const haConfig = ref({ url: '', token_set: false, token_preview: '' })
const haTokenInput = ref('')
const haSaving = ref(false)
const haTesting = ref(false)
// null | { status: 'success' | 'fail', reason?: 'unauthorized' | 'unreachable' | 'error', detail?: string }
const haTestResult = ref(null)

// 助手角色
const persona = ref('')
const personaCustomized = ref(false)
const personaSaving = ref(false)
const personaSaved = ref(false)

// API Keys
const keys = ref([])
const showKeyForm = ref(false)
const deletingKey = ref(null)
const newKey = ref({ base_url: '', model: '', type: 'chat', api_key: '' })
const typeOptions = ['chat', 'summary', 'vision', 'embed', 'stt']
const typeSelectOptions = typeOptions.map(t => ({ value: t, label: t }))

// ===== 加载所有配置 =====
async function loadAll() {
  loading.value = true
  try {
    const [weatherRes, advRes, haRes, uniqueRes, keysRes, automationRes] = await Promise.all([
      fetch('/api/weather/config'),
      fetch('/api/advanced/config'),
      fetch('/api/ha/config'),
      fetch('/api/unique'),
      fetch('/api/llm_keys'),
      fetch('/api/automation/status'),
    ])

    if (weatherRes.ok) {
      const json = await weatherRes.json()
      weatherConfig.value = { ...weatherConfig.value, ...json.data }
    }
    if (advRes.ok) {
      const json = await advRes.json()
      const data = json.data || {}
      if (data.web_search) webSearchConfig.value = { ...webSearchConfig.value, ...data.web_search }
      if (data.vision) visionConfig.value = { ...visionConfig.value, ...data.vision }
    }
    if (haRes.ok) {
      const json = await haRes.json()
      const data = json.data || {}
      haConfig.value = { url: data.url || '', token_set: data.token_set || false, token_preview: data.token_preview || '' }
      haTokenInput.value = ''
    }
    if (uniqueRes.ok) {
      const json = await uniqueRes.json()
      const data = json.data || {}
      persona.value = data.persona || ''
      personaCustomized.value = data.persona_custom || false
    }
    if (keysRes.ok) {
      const json = await keysRes.json()
      keys.value = json.data || []
    }
    if (automationRes.ok) {
      const json = await automationRes.json()
      const data = json.data || {}
      automationConfig.value = {
        ...automationConfig.value,
        silent_eval_enabled: data.silent_eval_enabled ?? true,
        silent_eval_interval_seconds: data.silent_eval_interval_seconds ?? 60,
        nonvision_silent_enabled: data.nonvision_silent_enabled ?? true,
        nonvision_silent_interval_seconds: data.nonvision_silent_interval_seconds ?? 30,
        default_cooldown_seconds: data.default_cooldown_seconds ?? 5,
        motion_threshold: data.motion_threshold ?? 15,
        motion_threshold_max: data.motion_threshold_max ?? 256,
        min_trigger_interval: data.min_trigger_interval ?? 3.0,
        camera_vl_display_enabled: data.camera_vl_display_enabled ?? true,
        running: data.running ?? false,
        eval_count: data.eval_count ?? 0,
        nonvision_eval_count: data.nonvision_eval_count ?? 0,
      }
      appliedValues.value = {
        vision: automationConfig.value.silent_eval_interval_seconds,
        nonvision: automationConfig.value.nonvision_silent_interval_seconds,
        cooldown: automationConfig.value.default_cooldown_seconds,
        threshold: automationConfig.value.motion_threshold,
      }
    }
  } catch (e) {
    console.error('Failed to load config:', e)
  } finally {
    loading.value = false
  }
}

// ===== 天气保存 =====
const weatherSaving = ref(false)
const weatherSaved = ref(false)
// probe 结果：null | { status: 'success' | 'fail', reason?, detail? }
const weatherProbeResult = ref(null)
async function saveWeather() {
  weatherSaving.value = true
  weatherSaved.value = false
  weatherProbeResult.value = null
  try {
    const data = await apiPost('/api/weather/config', weatherConfig.value)
    // 后端 probe 失败返回 200 + data.saved=false（不抛错），这里检查
    if (data && data.saved === false) {
      weatherProbeResult.value = { status: 'fail', reason: data.reason || 'error', detail: data.detail || '' }
      return
    }
    await loadAll()
    weatherSaved.value = true
    setTimeout(() => { weatherSaved.value = false }, 2000)
  } catch (e) {
    // schema 格式错误走这里（422）
    weatherProbeResult.value = { status: 'fail', reason: 'bad_format', detail: e?.message || String(e) }
    console.error('Failed to save weather config:', e)
  } finally {
    weatherSaving.value = false
  }
}

// ===== 天气测试连接 =====
const weatherTesting = ref(false)
async function testWeather() {
  weatherTesting.value = true
  weatherProbeResult.value = null
  try {
    const data = await apiPost('/api/weather/test', {})
    if (data && data.connected) {
      weatherProbeResult.value = { status: 'success' }
    } else {
      weatherProbeResult.value = { status: 'fail', reason: data?.reason || 'error', detail: data?.detail || '' }
    }
  } catch (e) {
    weatherProbeResult.value = { status: 'fail', reason: 'error', detail: String(e) }
  } finally {
    weatherTesting.value = false
  }
}

// ===== Exa 保存 =====
const exaSaving = ref(false)
const exaSaved = ref(false)
const exaProbeResult = ref(null)
async function saveExa() {
  exaSaving.value = true
  exaSaved.value = false
  exaProbeResult.value = null
  try {
    const data = await apiPost('/api/advanced/config', { web_search: webSearchConfig.value })
    if (data && data.saved === false) {
      exaProbeResult.value = { status: 'fail', reason: data.reason || 'error', detail: data.detail || '' }
      return
    }
    await loadAll()
    exaSaved.value = true
    setTimeout(() => { exaSaved.value = false }, 2000)
  } catch (e) {
    exaProbeResult.value = { status: 'fail', reason: 'bad_format', detail: e?.message || String(e) }
    console.error('Failed to save exa config:', e)
  } finally {
    exaSaving.value = false
  }
}

// ===== Exa 测试连接 =====
const exaTesting = ref(false)
async function testExa() {
  exaTesting.value = true
  exaProbeResult.value = null
  try {
    const data = await apiPost('/api/advanced/test/exa', {})
    if (data && data.connected) {
      exaProbeResult.value = { status: 'success' }
    } else {
      exaProbeResult.value = { status: 'fail', reason: data?.reason || 'error', detail: data?.detail || '' }
    }
  } catch (e) {
    exaProbeResult.value = { status: 'fail', reason: 'error', detail: String(e) }
  } finally {
    exaTesting.value = false
  }
}

// ===== 视觉/RTSP 配置已随多摄像头体系移入「摄像头设置」页 =====
// 此处仅保留视觉处理参数;RTSP 源(url/用户名/密码)的试连与保存走
// /api/cameras/{id}/test-stream 和 PUT /api/cameras/{id}。


// ===== 自动化保存 =====
async function saveAutomation() {
  // 任一数字输入非法时不保存(输入框旁已红字提示),修复后再点
  if (!automationFieldsValid()) return
  automationSaving.value = true
  automationSaved.value = false
  try {
    await Promise.all([
      apiPost('/api/automation/silent', {
        enabled: automationConfig.value.silent_eval_enabled,
        interval_seconds: automationConfig.value.silent_eval_interval_seconds,
        scope: 'vision',
      }),
      apiPost('/api/automation/silent', {
        enabled: automationConfig.value.nonvision_silent_enabled,
        interval_seconds: automationConfig.value.nonvision_silent_interval_seconds,
        scope: 'nonvision',
      }),
      apiPost('/api/automation/cooldown', {
        cooldown_seconds: automationConfig.value.default_cooldown_seconds,
      }),
      apiPost('/api/automation/dhash-threshold', {
        threshold: automationConfig.value.motion_threshold,
      }),
    ])
    await loadAll()
    automationSaved.value = true
    setTimeout(() => { automationSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save automation config:', e)
  } finally {
    automationSaving.value = false
  }
}

// ===== 数据出网模式 =====
const { egressMode, egressLabel, egressWarnings, loadEgressMode } = useEgressMode()
const egressModeOptions = EGRESS_MODES
const egressDraftMode = ref('cloud')   // 弹窗里的未保存选择
const egressSaving = ref(false)
const egressSaved = ref(false)

function openEgressModal() {
  egressDraftMode.value = egressMode.value
  openModal('egress')
}

async function saveEgressMode() {
  egressSaving.value = true
  try {
    await apiPost('/api/egress', { mode: egressDraftMode.value })
    await loadEgressMode()
    egressSaved.value = true
    setTimeout(() => { egressSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save egress mode:', e)
  } finally {
    egressSaving.value = false
  }
}

// ===== 摄像头参数保存(运动检测+推理间隔走 vision 全局) =====
const camParamsSaving = ref(false)
const camParamsSaved = ref(false)
async function saveCamParams() {
  camParamsSaving.value = true
  camParamsSaved.value = false
  try {
    // vision 段:只提交视觉处理参数,不含 RTSP 源字段(已归摄像头设置页),
    // 避免遗留旧 URL 触发无关 probe 卡死保存
    const { rtsp_url, rtsp_username, has_rtsp_password, ...visionParams } = visionConfig.value
    await apiPost('/api/advanced/config', {
      vision: visionParams,
    })
    await loadAll()
    camParamsSaved.value = true
    setTimeout(() => { camParamsSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save cam params:', e)
  } finally {
    camParamsSaving.value = false
  }
}

// ===== HA 保存 + 测试 =====
async function saveHa() {
  haSaving.value = true
  try {
    const payload = { url: haConfig.value.url }
    if (haTokenInput.value.trim()) {
      payload.token = haTokenInput.value.trim()
    }
    await apiPost('/api/ha/config', payload)
    await loadAll()
  } catch (e) {
    console.error('Failed to save HA config:', e)
  } finally {
    haSaving.value = false
  }
}

async function testHa() {
  haTesting.value = true
  haTestResult.value = null
  try {
    const res = await fetch('/api/ha/test', { method: 'POST' })
    const json = await res.json()
    const data = json.data || json || {}
    if (data.connected) {
      haTestResult.value = { status: 'success' }
    } else {
      // 后端区分 unauthorized（Token 错）/ unreachable（URL 不通）/ error（其他）
      haTestResult.value = { status: 'fail', reason: data.reason || 'error', detail: data.detail || '' }
    }
  } catch (e) {
    console.error('Failed to test HA:', e)
    haTestResult.value = { status: 'fail', reason: 'error', detail: String(e) }
  } finally {
    haTesting.value = false
  }
}

// ===== 助手角色保存 =====
async function saveUnique() {
  personaSaving.value = true
  personaSaved.value = false
  try {
    const res = await fetch('/api/unique', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ persona: persona.value }),
    })
    const json = await res.json()
    const data = json.data || json || {}
    personaCustomized.value = data.persona_custom || false
    personaSaved.value = true
    setTimeout(() => { personaSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save unique:', e)
  } finally {
    personaSaving.value = false
  }
}

// ===== API Keys 增删 =====
async function addKey() {
  try {
    const res = await fetch('/api/llm_keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(newKey.value),
    })
    if (res.ok) {
      newKey.value = { base_url: '', model: '', type: 'chat', api_key: '' }
      showKeyForm.value = false
      await loadAll()
    }
  } catch (e) {
    console.error('Failed to add key:', e)
  }
}

async function deleteKey(id) {
  try {
    deletingKey.value = id
    const res = await fetch(`/api/llm_keys/${id}`, { method: 'DELETE' })
    if (res.ok) {
      await loadAll()
    }
  } catch (e) {
    console.error('Failed to delete key:', e)
  } finally {
    deletingKey.value = null
  }
}

// ===== Emoji 索引重建 =====
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
      if (emojiPollTimer) { clearInterval(emojiPollTimer); emojiPollTimer = null }
      emojiRebuilding.value = false
    }
  } catch (e) {
    console.error('Failed to poll emoji rebuild status:', e)
  }
}

// ===== 文档向量重建 =====
const docRebuilding = ref(false)
const docRebuildStatus = ref({ rebuilding: false, total: 0, done: 0, errors: 0, message: '', model: '', chunk_count: 0 })
let docPollTimer = null

const docProgress = computed(() => {
  const s = docRebuildStatus.value
  if (!s.total) return 0
  return Math.round((s.done / s.total) * 100)
})

async function startDocRebuild() {
  try {
    docRebuilding.value = true
    docRebuildStatus.value = { rebuilding: true, total: 0, done: 0, errors: 0, message: '正在启动...', model: docRebuildStatus.value.model, chunk_count: docRebuildStatus.value.chunk_count }
    await fetch('/api/doc/rebuild', { method: 'POST' })
    docPollTimer = setInterval(pollDocRebuild, 2000)
  } catch (e) {
    console.error('Failed to start doc rebuild:', e)
    docRebuilding.value = false
  }
}

async function pollDocRebuild() {
  try {
    const res = await fetch('/api/doc/rebuild/status')
    const json = await res.json()
    docRebuildStatus.value = json.data || json
    if (!docRebuildStatus.value.rebuilding) {
      if (docPollTimer) { clearInterval(docPollTimer); docPollTimer = null }
      docRebuilding.value = false
    }
  } catch (e) {
    console.error('Failed to poll doc rebuild status:', e)
  }
}

// ===== 虚拟设备（模拟器 + MQTT）开关 =====
const simulator = ref({ available: false, running: false, simulator: {}, mqtt: {} })
const simulatorBusy = ref(false)
const simulatorError = ref('')

async function loadSimulatorStatus() {
  try {
    const res = await fetch('/api/simulator/status')
    const json = await res.json()
    simulator.value = { available: false, running: false, simulator: {}, mqtt: {}, ...(json.data || json) }
  } catch (e) {
    console.error('Failed to load simulator status:', e)
  }
}

async function toggleSimulator() {
  simulatorBusy.value = true
  simulatorError.value = ''
  try {
    const action = simulator.value.running ? 'stop' : 'start'
    const data = await apiPost(`/api/simulator/${action}`, {})
    if (data && data.ok === false) {
      // 优先展示服务端给出的具体原因（如容器未创建时的首次启用命令）
      simulatorError.value = data.hint || '操作失败，请检查 docker 服务状态'
    }
    await loadSimulatorStatus()
  } catch (e) {
    simulatorError.value = '操作失败：' + (e?.message || String(e))
  } finally {
    simulatorBusy.value = false
  }
}

// ===== 卡片摘要 =====
const weatherSummary = computed(() => weatherConfig.value.host || '未配置')
const exaSummary = computed(() => webSearchConfig.value.exa?.api_key ? '已配置' : '匿名')
const camParamsSummary = computed(() => `阈值${visionConfig.value.motion_threshold} · 间隔${visionConfig.value.min_infer_interval_seconds}s`)
const haSummary = computed(() => haConfig.value.url || '未配置')
const uniqueSummary = computed(() => personaCustomized.value ? '已自定义' : '默认')
const keysSummary = computed(() => `${keys.value.length} 个`)
const egressSummary = computed(() => egressLabel.value)
const automationSummary = computed(() => {
  const visionPart = automationConfig.value.silent_eval_enabled
    ? `视觉兜底 ${automationConfig.value.silent_eval_interval_seconds}s`
    : '视觉仅运动触发'
  const nonvisionPart = automationConfig.value.nonvision_silent_enabled
    ? `定时/天气 ${automationConfig.value.nonvision_silent_interval_seconds}s`
    : '定时/天气已停'
  return `${visionPart} · ${nonvisionPart}`
})

onMounted(() => {
  loadAll()
  loadSimulatorStatus()
  loadEgressMode()
  // 加载文档重建状态
  fetch('/api/doc/rebuild/status').then(r => r.json()).then(j => {
    docRebuildStatus.value = j.data || j
  }).catch(() => {})
})

onUnmounted(() => {
  if (emojiPollTimer) { clearInterval(emojiPollTimer); emojiPollTimer = null }
  if (docPollTimer) { clearInterval(docPollTimer); docPollTimer = null }
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>高级配置</h1>
      <p class="page-sub">管理系统级参数和第三方服务</p>
    </header>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="settings-sections">
      <!-- 配置卡片网格 -->
      <div class="config-grid">
        <div class="config-card" @click="openModal('weather')">
          <span class="config-icon">&#127780;</span>
          <div class="config-info">
            <span class="config-title">天气 API</span>
            <span class="config-status">{{ weatherSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openModal('exa')">
          <span class="config-icon">&#128269;</span>
          <div class="config-info">
            <span class="config-title">网页搜索（Exa）</span>
            <span class="config-status">{{ exaSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openModal('camparams')">
          <span class="config-icon">&#128247;</span>
          <div class="config-info">
            <span class="config-title">摄像头参数</span>
            <span class="config-status">{{ camParamsSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openModal('ha')">
          <span class="config-icon">&#127968;</span>
          <div class="config-info">
            <span class="config-title">Home Assistant</span>
            <span class="config-status">{{ haSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openModal('unique')">
          <span class="config-icon">&#129302;</span>
          <div class="config-info">
            <span class="config-title">助手角色</span>
            <span class="config-status">{{ uniqueSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openModal('keys')">
          <span class="config-icon">&#128273;</span>
          <div class="config-info">
            <span class="config-title">API Keys</span>
            <span class="config-status">{{ keysSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openModal('automation')">
          <span class="config-icon">&#9881;</span>
          <div class="config-info">
            <span class="config-title">自动化</span>
            <span class="config-status">{{ automationSummary }}</span>
          </div>
        </div>

        <div class="config-card" @click="openEgressModal()">
          <span class="config-icon">&#127760;</span>
          <div class="config-info">
            <span class="config-title">数据出网模式</span>
            <span class="config-status">{{ egressSummary }}</span>
          </div>
        </div>
      </div>

      <!-- 虚拟设备（模拟器 + MQTT） -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128268;</span>
          虚拟设备
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">模拟器 + MQTT broker</span>
              <span class="label-desc">
                关闭后虚拟设备（灯/空调/窗帘等）全部下线，
                <template v-if="!simulator.available">（需 Docker 部署并挂载 docker.sock）</template>
                <template v-else-if="simulator.simulator && simulator.simulator.exists === false">
                  默认关闭：首次启用在主机执行 docker compose --profile simulator up -d simulator，之后可在此开关
                </template>
                <template v-else>当前：{{ simulator.running ? '运行中' : '已关闭' }}</template>
              </span>
            </div>
            <BaseToggle
              v-if="simulator.available"
              :modelValue="simulator.running"
              :disabled="simulatorBusy"
              @update:modelValue="toggleSimulator"
            />
          </div>
          <div v-if="simulatorError" class="rebuild-message">{{ simulatorError }}</div>
        </div>
      </section>

      <!-- Emoji 索引重建 -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128248;</span>
          Emoji 索引重建
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">向量索引重建</span>
              <span class="label-desc">
                索引文件未纳入版本控制，换机器后需重建。<br />
                前提：已配置 embed 模型。
              </span>
            </div>
            <button class="btn-primary rebuild-btn" @click="startEmojiRebuild" :disabled="emojiRebuilding">
              {{ emojiRebuilding ? '重建中...' : '重建索引' }}
            </button>
          </div>
          <div v-if="emojiRebuildStatus.message" class="rebuild-info">
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

      <!-- 文档向量重建 -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128218;</span>
          文档向量重建
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <div class="setting-label">
              <span class="label-text">RAG 索引重建</span>
              <span class="label-desc">
                切换 embed 模型后需重建文档向量索引。<br />
                当前模型：{{ docRebuildStatus.model || '未配置' }}
                · 索引 {{ docRebuildStatus.chunk_count }} chunks
              </span>
            </div>
            <button class="btn-primary rebuild-btn" @click="startDocRebuild" :disabled="docRebuilding">
              {{ docRebuilding ? '重建中...' : '重建向量' }}
            </button>
          </div>
          <div v-if="docRebuildStatus.message || docRebuilding" class="rebuild-info">
            <div class="rebuild-message">{{ docRebuildStatus.message || '重建中...' }}</div>
            <div v-if="docRebuildStatus.total > 0" class="rebuild-progress-bar">
              <div class="rebuild-progress-fill" :style="{ width: docProgress + '%' }"></div>
              <span class="rebuild-progress-text">
                {{ docRebuildStatus.done }} / {{ docRebuildStatus.total }}
                <span v-if="docRebuildStatus.errors > 0" class="rebuild-errors">
                  (失败 {{ docRebuildStatus.errors }})
                </span>
              </span>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- ===== 配置 Modal ===== -->
    <AdvancedModal v-if="activeModal" :title="modalTitle" @close="closeModal">
      <!-- 天气 API -->
      <div v-if="activeModal === 'weather'" class="modal-content">
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">Host</span>
            <span class="label-desc">API 主机地址</span>
          </label>
          <input v-model="weatherConfig.host" class="setting-input" placeholder="devapi.qweather.com" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">Key ID (kid)</span>
          </label>
          <input v-model="weatherConfig.kid" class="setting-input" placeholder="xxxxxxxx" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">Subscriber (sub)</span>
          </label>
          <input v-model="weatherConfig.sub" class="setting-input" placeholder="xxxxxxxx" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">Private Key</span>
            <span class="label-desc">{{ weatherConfig.has_private_key ? '已配置（留空保持不变）' : '未配置' }}</span>
          </label>
          <input v-model="weatherConfig.private_key" type="password" class="setting-input" placeholder="Ed25519 私钥" />
        </div>
        <div class="setting-row test-row">
          <label class="setting-label">
            <span class="label-text">连接测试</span>
            <span class="label-desc">验证和风凭证是否正确</span>
          </label>
          <div class="test-actions">
            <button class="btn-test" :disabled="weatherTesting || !weatherConfig.host" @click="testWeather">
              {{ weatherTesting ? '测试中...' : '测试' }}
            </button>
            <span v-if="weatherProbeResult?.status === 'success'" class="test-result success">✅ 连接成功</span>
            <span v-else-if="weatherProbeResult?.status === 'fail'" class="test-result fail">
              <template v-if="weatherProbeResult.reason === 'unauthorized'">❌ 凭证无效或已过期（请检查 kid/sub/private_key）</template>
              <template v-else-if="weatherProbeResult.reason === 'unreachable'">❌ 地址不可达（请检查 host）</template>
              <template v-else-if="weatherProbeResult.reason === 'bad_format'">❌ 格式错误：{{ weatherProbeResult.detail }}</template>
              <template v-else>❌ 连接失败：{{ weatherProbeResult.detail || '未知错误' }}</template>
            </span>
          </div>
        </div>
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: weatherSaved }" @click="saveWeather" :disabled="weatherSaving">
            {{ weatherSaving ? '保存中...' : weatherSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>

      <!-- Exa 搜索 -->
      <div v-else-if="activeModal === 'exa'" class="modal-content">
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">API Key</span>
            <span class="label-desc">留空则匿名调用 Exa MCP（有速率限制）</span>
          </label>
          <input v-model="webSearchConfig.exa.api_key" type="password" class="setting-input" placeholder="exa api key" />
        </div>
        <div class="setting-row test-row">
          <label class="setting-label">
            <span class="label-text">连接测试</span>
            <span class="label-desc">验证 Exa API key 是否有效（留空测匿名）</span>
          </label>
          <div class="test-actions">
            <button class="btn-test" :disabled="exaTesting" @click="testExa">
              {{ exaTesting ? '测试中...' : '测试' }}
            </button>
            <span v-if="exaProbeResult?.status === 'success'" class="test-result success">✅ 连接成功</span>
            <span v-else-if="exaProbeResult?.status === 'fail'" class="test-result fail">
              <template v-if="exaProbeResult.reason === 'unauthorized'">❌ API key 无效或被拒</template>
              <template v-else-if="exaProbeResult.reason === 'unreachable'">❌ Exa 服务不可达</template>
              <template v-else>❌ 连接失败：{{ exaProbeResult.detail || '未知错误' }}</template>
            </span>
          </div>
        </div>
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: exaSaved }" @click="saveExa" :disabled="exaSaving">
            {{ exaSaving ? '保存中...' : exaSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>

      <!-- 摄像头参数(运动检测+推理间隔；云台速度/步进已并入各摄像头的 PTZ 设置) -->
      <div v-else-if="activeModal === 'camparams'" class="modal-content">
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">运动检测阈值</span>
            <span class="label-desc">画面变化多大算"有动静"(1~256,拉满=关 dhash 降级定时器)</span>
          </label>
          <input v-model.number="visionConfig.motion_threshold" type="number" class="setting-input narrow" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">推理最小间隔 (秒)</span>
            <span class="label-desc">防止频繁调用视觉模型</span>
          </label>
          <input v-model.number="visionConfig.min_infer_interval_seconds" type="number" step="0.5" class="setting-input narrow" />
        </div>
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: camParamsSaved }" @click="saveCamParams" :disabled="camParamsSaving">
            {{ camParamsSaving ? '保存中...' : camParamsSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>

      <!-- HA 配置 -->
      <div v-else-if="activeModal === 'ha'" class="modal-content">
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">URL</span>
            <span class="label-desc">Home Assistant 地址</span>
          </label>
          <input v-model="haConfig.url" class="setting-input" placeholder="http://homeassistant.local:8123" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">Token</span>
            <span class="label-desc">{{ haConfig.token_set ? `已设置（${haConfig.token_preview}）` : '未设置' }}</span>
          </label>
          <input v-model="haTokenInput" class="setting-input" type="password" placeholder="留空不修改" />
        </div>
        <div class="setting-row test-row">
          <label class="setting-label">
            <span class="label-text">连接测试</span>
            <span class="label-desc">验证 HA 配置是否正确</span>
          </label>
          <div class="test-actions">
            <button class="btn-test" :disabled="haTesting || !haConfig.url" @click="testHa">
              {{ haTesting ? '测试中...' : '测试' }}
            </button>
            <span v-if="haTestResult?.status === 'success'" class="test-result success">✅ 连接成功</span>
            <span v-else-if="haTestResult?.status === 'fail'" class="test-result fail">
              <template v-if="haTestResult.reason === 'unauthorized'">❌ Token 无效或已过期（URL 可达，请检查 Token）</template>
              <template v-else-if="haTestResult.reason === 'unreachable'">❌ HA 地址不可达（请检查 URL）</template>
              <template v-else>❌ 连接失败：{{ haTestResult.detail || '未知错误' }}</template>
            </span>
          </div>
        </div>
        <div class="modal-save-bar">
          <button class="btn-primary" @click="saveHa" :disabled="haSaving">
            {{ haSaving ? '保存中...' : '保存' }}
          </button>
        </div>
      </div>

      <!-- 助手角色 -->
      <div v-else-if="activeModal === 'unique'" class="modal-content">
        <div class="setting-row unique-row">
          <span class="label-text" v-if="personaCustomized">
            <span class="custom-badge">已自定义</span>
          </span>
          <textarea
            v-model="persona"
            class="setting-textarea"
            rows="8"
            placeholder="描述助手的角色身份、性格特征和交互风格..."
          ></textarea>
        </div>
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: personaSaved }" @click="saveUnique" :disabled="personaSaving">
            {{ personaSaving ? '保存中...' : personaSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>

      <!-- API Keys -->
      <div v-else-if="activeModal === 'keys'" class="modal-content">
        <div class="keys-toolbar">
          <button class="btn-add-key" @click="showKeyForm = !showKeyForm">
            {{ showKeyForm ? '取消' : '+ 添加 Key' }}
          </button>
        </div>
        <div v-if="showKeyForm" class="key-form">
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">Base URL</span>
            </label>
            <input v-model="newKey.base_url" class="setting-input" placeholder="https://api.openai.com/v1" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">Model</span>
            </label>
            <input v-model="newKey.model" class="setting-input" placeholder="gpt-4o" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">Type</span>
            </label>
            <FlowSelect v-model="newKey.type" :options="typeSelectOptions" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">API Key</span>
            </label>
            <input v-model="newKey.api_key" class="setting-input" type="password" placeholder="sk-..." />
          </div>
          <div class="modal-save-bar">
            <button class="btn-primary" @click="addKey">保存</button>
          </div>
        </div>
        <div v-if="keys.length === 0" class="empty-hint">暂无配置的 API Key</div>
        <div v-else class="key-list">
          <div v-for="key in keys" :key="key.id" class="key-row">
            <div class="key-info">
              <span class="key-model">{{ key.model }}</span>
              <span class="key-meta">
                <span class="key-type-badge">{{ key.type }}</span>
                <span class="key-url">{{ key.base_url }}</span>
                <span v-if="key.api_key_set" class="key-set">已配置</span>
                <span v-else class="key-unset">未配置</span>
              </span>
            </div>
            <button class="btn-delete-key" :disabled="deletingKey === key.id" @click="deleteKey(key.id)">
              {{ deletingKey === key.id ? '...' : '删除' }}
            </button>
          </div>
        </div>
      </div>

      <!-- 自动化配置 -->
      <div v-else-if="activeModal === 'automation'" class="modal-content">
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">全局(定时/天气)规则兜底</span>
            <span class="label-desc">时间/天气规则仅由此循环按间隔评估；关闭后将不再评估</span>
          </label>
          <input type="checkbox" v-model="automationConfig.nonvision_silent_enabled" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">全局兜底间隔（秒）</span>
            <span class="label-desc">输入 5~3600 的整数，回车或失焦即生效；时间规则触发精度=此间隔</span>
          </label>
          <div class="interval-input-group">
            <input type="number" min="5" max="3600" step="1" class="setting-input narrow"
                   v-model.number="automationConfig.nonvision_silent_interval_seconds"
                   :class="{ 'input-invalid': !nonvisionIntervalValid }"
                   @keydown.enter="$event.target.blur()"
                   @blur="applySilentInterval('nonvision')" />
            <span v-if="!nonvisionIntervalValid" class="field-error">需为 5~3600 的整数</span>
            <span v-else-if="appliedFeedback.nonvision" class="field-ok">已生效</span>
          </div>
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">摄像头(视觉)规则兜底</span>
            <span class="label-desc">dhash 阈值拉满时即轮询间隔；关掉仅靠运动触发</span>
          </label>
          <input type="checkbox" v-model="automationConfig.silent_eval_enabled" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">摄像头兜底间隔（秒）</span>
            <span class="label-desc">输入 5~3600 的整数，回车或失焦即生效；无运动时按此周期评估视觉规则</span>
          </label>
          <div class="interval-input-group">
            <input type="number" min="5" max="3600" step="1" class="setting-input narrow"
                   v-model.number="automationConfig.silent_eval_interval_seconds"
                   :class="{ 'input-invalid': !silentIntervalValid }"
                   @keydown.enter="$event.target.blur()"
                   @blur="applySilentInterval('vision')" />
            <span v-if="!silentIntervalValid" class="field-error">需为 5~3600 的整数</span>
            <span v-else-if="appliedFeedback.vision" class="field-ok">已生效</span>
          </div>
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">默认冷却（秒）</span>
            <span class="label-desc">输入 1~3600 的整数，回车或失焦即生效；只影响新建/无显式 cooldown 的规则</span>
          </label>
          <div class="interval-input-group">
            <input type="number" min="1" max="3600" step="1" class="setting-input narrow"
                   v-model.number="automationConfig.default_cooldown_seconds"
                   :class="{ 'input-invalid': !cooldownValid }"
                   @keydown.enter="$event.target.blur()"
                   @blur="applyCooldown" />
            <span v-if="!cooldownValid" class="field-error">需为 1~3600 的整数</span>
            <span v-else-if="appliedFeedback.cooldown" class="field-ok">已生效</span>
          </div>
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">dhash 阈值</span>
            <span class="label-desc">输入 1~{{ automationConfig.motion_threshold_max }} 的整数，回车或失焦即生效；拉满=关 dhash 降级定时器</span>
          </label>
          <div class="interval-input-group">
            <input type="number" min="1" :max="automationConfig.motion_threshold_max" step="1" class="setting-input narrow"
                   v-model.number="automationConfig.motion_threshold"
                   :class="{ 'input-invalid': !thresholdValid }"
                   @keydown.enter="$event.target.blur()"
                   @blur="applyThreshold" />
            <span v-if="!thresholdValid" class="field-error">需为 1~{{ automationConfig.motion_threshold_max }} 的整数</span>
            <span v-else-if="appliedFeedback.threshold" class="field-ok">已生效</span>
            <span v-else class="slider-value">{{ automationConfig.motion_threshold }} / {{ automationConfig.motion_threshold_max }}</span>
          </div>
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">dhash 节流</span>
            <span class="label-desc">运动触发评估的最小间隔，复用 vision.min_infer_interval_seconds</span>
          </label>
          <span class="slider-value">{{ automationConfig.min_trigger_interval }}s</span>
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">运行状态</span>
            <span class="label-desc">视觉(含运动触发) / 定时天气 评估次数，重启后清零</span>
          </label>
          <span class="slider-value">{{ automationConfig.running ? '运行中' : '已停' }} · {{ automationConfig.eval_count }} / {{ automationConfig.nonvision_eval_count }} 次</span>
        </div>
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: automationSaved }" @click="saveAutomation" :disabled="automationSaving">
            {{ automationSaving ? '保存中...' : automationSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>

      <!-- 数据出网模式 -->
      <div v-else-if="activeModal === 'egress'" class="modal-content">
        <div
          v-for="m in egressModeOptions"
          :key="m.key"
          class="setting-row"
        >
          <label class="setting-label">
            <input type="radio" name="egress-draft" :value="m.key" v-model="egressDraftMode" />
            <span class="label-text">{{ m.icon }} {{ m.label }}</span>
            <span class="label-desc">{{ m.desc }}</span>
          </label>
        </div>
        <p v-if="egressWarnings.length" class="egress-warnings">
          ⚠️ {{ egressWarnings.join('；') }}
        </p>
        <p class="egress-hint">
          纯内网模式：到「API Keys」把各角色 base_url 指向 OpenAI 兼容的内网端点即可。<br />
          内置 Ollama：<code>docker compose --profile local-llm up -d ollama</code> 启动后填
          <code>http://ollama:11434/v1</code>；也可用内网其他机器（Mac 上的 Ollama / LM Studio、自建 vLLM）。<br />
          切到纯内网后，保存公网端点会被拒绝（硬拦截，切回云端/混合立刻放行）。
        </p>
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: egressSaved }" @click="saveEgressMode" :disabled="egressSaving">
            {{ egressSaving ? '保存中...' : egressSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>
    </AdvancedModal>
  </div>
</template>

<style scoped>
/* 配置卡片网格 */
.config-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-8);
  margin-bottom: var(--space-16);
}

.config-card {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-12) var(--space-14);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-2xl);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.config-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-primary);
  transform: translateY(-2px);
  box-shadow: var(--shadow-lg);
}

.config-icon {
  font-size: var(--text-2xl);
  flex-shrink: 0;
}

.config-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

.config-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.config-status {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Modal 内表单 */
.setting-input {
  width: 240px;
}

.setting-input.narrow {
  width: 100px;
}

/* 自动化 modal 的滑块行 */
.slider-row {
  display: flex;
  align-items: center;
  gap: var(--space-4, 10px);
  min-width: 220px;
}
.slider-row input[type="range"] {
  flex: 1;
  min-width: 140px;
  accent-color: var(--color-accent, #4a9eff);
}
.slider-value {
  font-size: var(--text-sm, 13px);
  color: var(--color-text-secondary, #888);
  white-space: nowrap;
  min-width: 60px;
  text-align: right;
}

/* 自动化 modal 的间隔数字输入 */
.interval-input-group {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 220px;
}
.interval-input-group .setting-input {
  width: 110px;
}
.input-invalid {
  border-color: var(--color-danger, #e5484d) !important;
}
.field-error {
  font-size: var(--text-xs, 12px);
  color: var(--color-danger, #e5484d);
  white-space: nowrap;
}
.field-ok {
  font-size: var(--text-xs, 12px);
  color: var(--color-success, #46a758);
  white-space: nowrap;
}

.setting-textarea {
  width: 100%;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.6;
  resize: vertical;
  min-height: 140px;
  padding: var(--space-6) var(--space-8);
  outline: none;
}

.setting-textarea:focus {
  border-color: var(--color-primary);
}

.unique-row {
  flex-direction: column;
  align-items: stretch;
  gap: var(--space-4);
}

.modal-save-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: var(--space-12);
}

/* 数据出网模式弹窗 */
.egress-warnings {
  margin: var(--space-8) 0;
  padding: var(--space-8) var(--space-10);
  background: var(--color-warning-bg);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  color: var(--color-warning);
  line-height: 1.6;
}

.egress-hint {
  margin: var(--space-8) 0 0;
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  line-height: 1.6;
}

/* HA 测试 */
.test-row {
  gap: var(--space-8);
}

.test-actions {
  display: flex;
  align-items: center;
  gap: var(--space-6);
}

.btn-test {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid rgba(74, 124, 112, 0.25);
  padding: var(--space-3) var(--space-12);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  white-space: nowrap;
}

.btn-test:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.test-result.success { color: var(--color-success); font-size: var(--text-xs); }
.test-result.fail { color: #e74c3c; font-size: var(--text-xs); }

/* 助手角色 */
.custom-badge {
  font-size: 10px;
  font-weight: var(--weight-semibold);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
}

/* API Keys */
.keys-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: var(--space-6);
}

.btn-add-key {
  padding: var(--space-3) var(--space-10);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}

.btn-add-key:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.key-form {
  padding: var(--space-10);
  background: rgba(255, 255, 255, 0.02);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  margin-bottom: var(--space-10);
}

.empty-hint {
  text-align: center;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  padding: var(--space-12);
}

.key-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.key-row {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  padding: var(--space-6) var(--space-10);
  background: rgba(255, 255, 255, 0.02);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
}

.key-info {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  flex: 1;
}

.key-model {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.key-meta {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.key-type-badge {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.key-url {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 160px;
}

.key-set { font-size: var(--text-xs); color: var(--color-success); }
.key-unset { font-size: var(--text-xs); color: var(--color-text-muted); }

.btn-delete-key {
  background: transparent;
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-border);
  padding: var(--space-2) var(--space-8);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  cursor: pointer;
  white-space: nowrap;
}

.btn-delete-key:hover:not(:disabled) {
  color: #e74c3c;
  border-color: rgba(231, 76, 60, 0.3);
}

.btn-delete-key:disabled {
  opacity: 0.5;
}

/* 重建进度 */
.rebuild-btn {
  flex-shrink: 0;
  white-space: nowrap;
}

.rebuild-info {
  margin-top: var(--space-4);
  padding-top: var(--space-4);
  border-top: 1px solid var(--color-border, rgba(255, 255, 255, 0.1));
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
  border: 1px solid var(--color-border, rgba(255, 255, 255, 0.1));
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
  .config-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .setting-input {
    width: 160px;
  }

  .setting-input.narrow {
    width: 80px;
  }
}
</style>
