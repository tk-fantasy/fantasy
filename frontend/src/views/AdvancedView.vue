<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import FlowSelect from '../components/FlowSelect.vue'
import AdvancedModal from '../components/AdvancedModal.vue'
import { apiGet, apiPost } from '../utils/api'

// ===== Modal 管理 =====
const activeModal = ref(null) // 'weather' | 'exa' | 'vision' | 'ha' | 'unique' | 'keys'

const modalTitle = computed(() => {
  const titles = {
    weather: '天气 API（和风天气）',
    exa: '网页搜索（Exa）',
    vision: '视觉参数',
    ha: 'Home Assistant',
    unique: '助手角色',
    keys: 'API Keys',
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
  rtsp_url: '',
  rtsp_username: '',
  has_rtsp_password: false,
})
const rtspPassword = ref('')

// HA 配置
const haConfig = ref({ url: '', token_set: false, token_preview: '' })
const haTokenInput = ref('')
const haSaving = ref(false)
const haTesting = ref(false)
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
    const [weatherRes, advRes, haRes, uniqueRes, keysRes] = await Promise.all([
      fetch('/api/weather/config'),
      fetch('/api/advanced/config'),
      fetch('/api/ha/config'),
      fetch('/api/unique'),
      fetch('/api/llm_keys'),
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
  } catch (e) {
    console.error('Failed to load config:', e)
  } finally {
    loading.value = false
  }
}

// ===== 天气保存 =====
const weatherSaving = ref(false)
const weatherSaved = ref(false)
async function saveWeather() {
  weatherSaving.value = true
  weatherSaved.value = false
  try {
    await apiPost('/api/weather/config', weatherConfig.value)
    await loadAll()
    weatherSaved.value = true
    setTimeout(() => { weatherSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save weather config:', e)
  } finally {
    weatherSaving.value = false
  }
}

// ===== Exa 保存 =====
const exaSaving = ref(false)
const exaSaved = ref(false)
async function saveExa() {
  exaSaving.value = true
  exaSaved.value = false
  try {
    await apiPost('/api/advanced/config', { web_search: webSearchConfig.value })
    await loadAll()
    exaSaved.value = true
    setTimeout(() => { exaSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save exa config:', e)
  } finally {
    exaSaving.value = false
  }
}

// ===== 视觉保存 =====
const visionSaving = ref(false)
const visionSaved = ref(false)
async function saveVision() {
  visionSaving.value = true
  visionSaved.value = false
  try {
    await apiPost('/api/advanced/config', {
      vision: visionConfig.value,
      rtsp_password: rtspPassword.value,
    })
    rtspPassword.value = ''
    await loadAll()
    visionSaved.value = true
    setTimeout(() => { visionSaved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save vision config:', e)
  } finally {
    visionSaving.value = false
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
    haTestResult.value = data.connected ? 'success' : 'fail'
  } catch (e) {
    console.error('Failed to test HA:', e)
    haTestResult.value = 'fail'
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
const docRebuildStatus = ref({ rebuilding: false, model: '', chunk_count: 0 })
let docPollTimer = null

async function startDocRebuild() {
  try {
    docRebuilding.value = true
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

// ===== 卡片摘要 =====
const weatherSummary = computed(() => weatherConfig.value.host || '未配置')
const exaSummary = computed(() => webSearchConfig.value.exa?.api_key ? '已配置' : '匿名')
const visionSummary = computed(() => visionConfig.value.rtsp_url || 'USB')
const haSummary = computed(() => haConfig.value.url || '未配置')
const uniqueSummary = computed(() => personaCustomized.value ? '已自定义' : '默认')
const keysSummary = computed(() => `${keys.value.length} 个`)

onMounted(() => {
  loadAll()
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

        <div class="config-card" @click="openModal('vision')">
          <span class="config-icon">&#128247;</span>
          <div class="config-info">
            <span class="config-title">视觉参数</span>
            <span class="config-status">{{ visionSummary }}</span>
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
      </div>

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
          <div v-if="docRebuilding" class="rebuild-info">
            <div class="rebuild-message">正在后台重建文档向量索引...</div>
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
        <div class="modal-save-bar">
          <button class="btn-primary" :class="{ saved: exaSaved }" @click="saveExa" :disabled="exaSaving">
            {{ exaSaving ? '保存中...' : exaSaved ? '已保存' : '保存' }}
          </button>
        </div>
      </div>

      <!-- 视觉参数 -->
      <div v-else-if="activeModal === 'vision'" class="modal-content">
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">摄像头源（RTSP）</span>
            <span class="label-desc">填 RTSP 地址走网络摄像头，留空走 USB</span>
          </label>
          <input v-model="visionConfig.rtsp_url" class="setting-input" placeholder="rtsp://192.168.1.100:554/stream" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">RTSP 用户名</span>
            <span class="label-desc">无鉴权摄像头留空</span>
          </label>
          <input v-model="visionConfig.rtsp_username" class="setting-input" placeholder="admin" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">RTSP 密码</span>
            <span class="label-desc">{{ visionConfig.has_rtsp_password ? '已配置（留空保持不变）' : '未配置' }}</span>
          </label>
          <input v-model="rtspPassword" type="password" class="setting-input" placeholder="摄像头密码" />
        </div>
        <div class="setting-row">
          <label class="setting-label">
            <span class="label-text">运动检测阈值</span>
            <span class="label-desc">画面变化多大算"有动静"</span>
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
          <button class="btn-primary" :class="{ saved: visionSaved }" @click="saveVision" :disabled="visionSaving">
            {{ visionSaving ? '保存中...' : visionSaved ? '已保存' : '保存' }}
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
            <span v-if="haTestResult === 'success'" class="test-result success">连接成功</span>
            <span v-else-if="haTestResult === 'fail'" class="test-result fail">连接失败</span>
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
