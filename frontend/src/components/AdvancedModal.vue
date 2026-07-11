<script setup>
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'

const emit = defineEmits(['close'])
const router = useRouter()

const loading = ref(true)
const saving = ref(false)
const saved = ref(false)

// 天气 API 配置
const weatherConfig = ref({
  host: '',
  kid: '',
  sub: '',
  private_key: '',
  has_private_key: false,
})

// Exa 网页搜索配置
const webSearchConfig = ref({
  exa: { api_key: '' },
})

// ���觉参数 — 保留全部字段（保存时原样回传，避免 pydantic 默认值覆盖），
// 模板只展示 RTSP + 运动阈值 + 推理间隔三项
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

// ---- 文档向量重建 ----
const docRebuilding = ref(false)
const docRebuildStatus = ref({ rebuilding: false, model: '', chunk_count: 0 })
let docPollTimer = null

async function startDocRebuild() {
  try {
    docRebuilding.value = true
    const res = await fetch('/api/doc/rebuild', { method: 'POST' })
    if (!res.ok) {
      docRebuilding.value = false
      return
    }
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

// ---- 配置加载/保存 ----
async function loadConfig() {
  loading.value = true
  try {
    const weatherRes = await fetch('/api/weather/config')
    if (weatherRes.ok) {
      const weatherJson = await weatherRes.json()
      weatherConfig.value = { ...weatherConfig.value, ...weatherJson.data }
    }

    const advRes = await fetch('/api/advanced/config')
    if (advRes.ok) {
      const advJson = await advRes.json()
      const data = advJson.data || {}
      if (data.web_search) webSearchConfig.value = { ...webSearchConfig.value, ...data.web_search }
      if (data.vision) visionConfig.value = { ...visionConfig.value, ...data.vision }
    }

    // 加载文档重建状态
    const docRes = await fetch('/api/doc/rebuild/status')
    if (docRes.ok) {
      const docJson = await docRes.json()
      docRebuildStatus.value = docJson.data || docJson
    }
  } catch (e) {
    console.error('Failed to load config:', e)
  } finally {
    loading.value = false
  }
}

async function saveAll() {
  saving.value = true
  saved.value = false
  try {
    await fetch('/api/weather/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(weatherConfig.value),
    })

    await fetch('/api/advanced/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        web_search: webSearchConfig.value,
        vision: visionConfig.value,
        rtsp_password: rtspPassword.value,
      }),
    })
    // 密码提交后清空输入框，并刷新 has_rtsp_password 标志
    rtspPassword.value = ''
    await loadConfig()

    saved.value = true
    setTimeout(() => { saved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save config:', e)
  } finally {
    saving.value = false
  }
}

function close() {
  emit('close')
}

function goTo(path) {
  close()
  router.push(path)
}

onMounted(() => {
  loadConfig()
})

onUnmounted(() => {
  if (emojiPollTimer) { clearInterval(emojiPollTimer); emojiPollTimer = null }
  if (docPollTimer) { clearInterval(docPollTimer); docPollTimer = null }
})
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div class="modal-overlay" @click.self="close">
        <div class="modal-container">
          <div class="modal-header">
            <h2>高级配置</h2>
            <button class="modal-close" @click="close">关闭</button>
          </div>

          <div v-if="loading" class="loading-state">加载中...</div>

          <div v-else class="modal-body settings-sections">
            <!-- 天气 API -->
            <section class="setting-section">
              <h2 class="section-title">
                <span class="section-icon">&#127780;</span>
                天气 API（和风天气）
              </h2>
              <div class="setting-card">
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
              </div>
            </section>

            <!-- Exa 网页搜索 -->
            <section class="setting-section">
              <h2 class="section-title">
                <span class="section-icon">&#128269;</span>
                网页搜索（Exa）
              </h2>
              <div class="setting-card">
                <div class="setting-row">
                  <label class="setting-label">
                    <span class="label-text">API Key</span>
                    <span class="label-desc">留空则匿名调用 Exa MCP（有速率限制）</span>
                  </label>
                  <input v-model="webSearchConfig.exa.api_key" type="password" class="setting-input" placeholder="exa api key，留空匿名使用" />
                </div>
              </div>
            </section>

            <!-- 视觉参数 -->
            <section class="setting-section">
              <h2 class="section-title">
                <span class="section-icon">&#128247;</span>
                视觉参数
              </h2>
              <div class="setting-card">
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
                      索引文件 (emoji_index.json) 未纳入版本控制，换机器后需重建。<br />
                      前提：已在「密钥管理」页配置 embed 模型 (如 BAAI/bge-m3)。
                    </span>
                  </div>
                  <button
                    class="btn-primary rebuild-btn"
                    @click="startEmojiRebuild"
                    :disabled="emojiRebuilding"
                  >
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
                  <button
                    class="btn-primary rebuild-btn"
                    @click="startDocRebuild"
                    :disabled="docRebuilding"
                  >
                    {{ docRebuilding ? '重建中...' : '重建向量' }}
                  </button>
                </div>
                <div v-if="docRebuilding" class="rebuild-info">
                  <div class="rebuild-message">正在后台重建文档向量索引...</div>
                </div>
              </div>
            </section>

            <!-- 快捷入口 -->
            <section class="setting-section">
              <h2 class="section-title">
                <span class="section-icon">&#128279;</span>
                快捷入口
              </h2>
              <div class="setting-card quick-links">
                <button class="quick-link" @click="goTo('/ha')">
                  <span class="quick-link-icon">&#127968;</span>
                  <span>HA 配置</span>
                </button>
                <button class="quick-link" @click="goTo('/unique')">
                  <span class="quick-link-icon">&#129302;</span>
                  <span>助手角色</span>
                </button>
                <button class="quick-link" @click="goTo('/keys')">
                  <span class="quick-link-icon">&#128273;</span>
                  <span>API Keys</span>
                </button>
              </div>
            </section>

            <!-- 统一保存 -->
            <div class="save-bar">
              <button class="btn-primary" :class="{ saved }" @click="saveAll" :disabled="saving">
                {{ saving ? '保存中...' : saved ? '已保存' : '保存所有配置' }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-12);
}

.modal-container {
  background: var(--color-bg-app);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-3xl);
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xl);
}

.modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-12) var(--space-16);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.modal-header h2 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin: 0;
}

.modal-close {
  padding: var(--space-3) var(--space-10);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.modal-close:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
}

.modal-body {
  overflow-y: auto;
  padding: var(--space-12) var(--space-16);
}

/* 设置输入宽度 */
.setting-input {
  width: 240px;
}

.setting-input.narrow {
  width: 100px;
}

/* 重建按钮 */
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

/* 快捷入口 */
.quick-links {
  display: flex;
  gap: var(--space-6);
  padding: var(--space-10) var(--space-14);
}

.quick-link {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-10) var(--space-6);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.quick-link:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-primary);
  color: var(--color-primary);
  transform: translateY(-2px);
}

.quick-link-icon {
  font-size: var(--text-xl);
}

/* Modal Transition */
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.3s var(--ease-out);
}

.modal-enter-active .modal-container,
.modal-leave-active .modal-container {
  transition: all 0.3s var(--ease-out);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

.modal-enter-from .modal-container,
.modal-leave-to .modal-container {
  transform: scale(0.95) translateY(20px);
}

@media (max-width: 768px) {
  .setting-input {
    width: 160px;
  }

  .setting-input.narrow {
    width: 80px;
  }

  .quick-links {
    flex-direction: column;
  }
}
</style>
