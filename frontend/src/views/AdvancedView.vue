<script setup>
import { ref, onMounted } from 'vue'

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

// 视觉参数
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
  // 摄像头源：填 RTSP URL 走网络流，留空走 USB
  rtsp_url: '',
  rtsp_username: '',
  has_rtsp_password: false,
})

// RTSP 密码：单独字段，留空表示不修改
const rtspPassword = ref('')

// RAG 参数
const ragConfig = ref({
  recent_turns: 5,
  retrieve_top_k: 6,
  retrieve_top_n: 3,
  soft_max_turns: 12,
  hard_max_turns: 16,
  soft_max_tokens: 12000,
  hard_max_tokens: 16000,
  soft_max_chars: 24000,
  hard_max_chars: 32000,
  summary_blocks: 2,
})

// Embed 状态
const embedStatus = ref({
  configured: false,
  model: '',
  emoji_available: false,
  rag_available: false,
  rag_chunks: 0,
})

async function loadConfig() {
  loading.value = true
  try {
    // 加载天气配置
    const weatherRes = await fetch('/api/weather/config')
    if (weatherRes.ok) {
      const weatherJson = await weatherRes.json()
      weatherConfig.value = { ...weatherConfig.value, ...weatherJson.data }
    }

    // 加载高级配置（网页搜索、视觉、RAG）
    const advRes = await fetch('/api/advanced/config')
    if (advRes.ok) {
      const advJson = await advRes.json()
      const data = advJson.data || {}
      if (data.web_search) webSearchConfig.value = { ...webSearchConfig.value, ...data.web_search }
      if (data.vision) visionConfig.value = { ...visionConfig.value, ...data.vision }
      if (data.rag) ragConfig.value = { ...ragConfig.value, ...data.rag }
    }

    // 加载 Embed 状态
    const embedRes = await fetch('/api/advanced/embed-status')
    if (embedRes.ok) {
      const embedJson = await embedRes.json()
      embedStatus.value = { ...embedStatus.value, ...embedJson.data }
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
    // 保存天气配置
    await fetch('/api/weather/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(weatherConfig.value),
    })

    // 保存高级配置（网页搜索、视觉、RAG）
    await fetch('/api/advanced/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        web_search: webSearchConfig.value,
        vision: visionConfig.value,
        rag: ragConfig.value,
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

onMounted(() => {
  loadConfig()
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
      <!-- Embed 状态 -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128270;</span>
          Embed 模型状态
        </h2>
        <div class="setting-card">
          <div class="status-row">
            <span class="status-label">Embed 模型</span>
            <span class="status-value" :class="{ ok: embedStatus.configured }">
              {{ embedStatus.configured ? `✅ ${embedStatus.model}` : '⬜ 未配置' }}
            </span>
          </div>
          <div class="status-row">
            <span class="status-label">Emoji 搜索</span>
            <span class="status-value" :class="{ ok: embedStatus.emoji_available }">
              {{ embedStatus.emoji_available ? '✅ 可用' : '⬜ 不可用' }}
            </span>
          </div>
          <div class="status-row">
            <span class="status-label">文档 RAG</span>
            <span class="status-value" :class="{ ok: embedStatus.rag_available }">
              {{ embedStatus.rag_available ? `✅ 索引已构建 (${embedStatus.rag_chunks} chunks)` : '⬜ 未构建' }}
            </span>
          </div>
          <p class="section-hint">
            Embed 模型在 <router-link to="/keys">API Keys</router-link> 页面配置（type=embed），
            在 <router-link to="/models">模型管理</router-link> 页面分配给 embed 角色。
          </p>
        </div>
      </section>

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
          <div class="setting-row hint-row">
            <p class="form-hint">
              前往 <a href="https://console.qweather.com" target="_blank">和风天气开发平台</a>
              注册账号，创建项目后获取以上参数。
            </p>
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
          <div class="setting-row hint-row">
            <p class="form-hint">
              前往 <a href="https://dashboard.exa.ai" target="_blank">Exa Dashboard</a>
              注册账号获取 API Key，配置后享每月 2 万次免费额度。匿名调用可直接使用。
            </p>
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
          <div class="setting-row hint-row">
            <p class="form-hint">
              填了 RTSP 地址会优先用网络摄像头，没填则自动尝试 USB（dshow/msmf）。
              密码加密存 .env，不进 config.json。
            </p>
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">图片缩放最大边长</span>
              <span class="label-desc">发送给视觉模型前的缩放尺寸</span>
            </label>
            <input v-model.number="visionConfig.downscale_max_side" type="number" class="setting-input narrow" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">JPEG 质量</span>
              <span class="label-desc">图片压缩质量 (1-100)</span>
            </label>
            <input v-model.number="visionConfig.jpeg_quality" type="number" class="setting-input narrow" min="1" max="100" />
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
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">每次送图数量</span>
              <span class="label-desc">每次分析发送几张图片</span>
            </label>
            <input v-model.number="visionConfig.vision_use_img_count" type="number" class="setting-input narrow" min="1" max="5" />
          </div>
        </div>
      </section>

      <!-- RAG 参数 -->
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#128218;</span>
          RAG / 对话压缩参数
        </h2>
        <div class="setting-card">
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">保留最近轮数</span>
              <span class="label-desc">不参与压缩的最近对话轮数</span>
            </label>
            <input v-model.number="ragConfig.recent_turns" type="number" class="setting-input narrow" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">软限制轮数</span>
              <span class="label-desc">超过此值开始压缩</span>
            </label>
            <input v-model.number="ragConfig.soft_max_turns" type="number" class="setting-input narrow" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">硬限制轮数</span>
              <span class="label-desc">超过此值强制压缩</span>
            </label>
            <input v-model.number="ragConfig.hard_max_turns" type="number" class="setting-input narrow" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">软限制 Token</span>
            </label>
            <input v-model.number="ragConfig.soft_max_tokens" type="number" class="setting-input narrow" />
          </div>
          <div class="setting-row">
            <label class="setting-label">
              <span class="label-text">硬限制 Token</span>
            </label>
            <input v-model.number="ragConfig.hard_max_tokens" type="number" class="setting-input narrow" />
          </div>
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
</template>

<style scoped>
.hint-row {
  flex-direction: column;
  align-items: flex-start;
}

.setting-input {
  width: 240px;
}

.setting-input.narrow {
  width: 100px;
}

.form-hint {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  margin: 0;
}

.form-hint a {
  color: var(--color-primary);
  text-decoration: none;
}

.form-hint a:hover {
  text-decoration: underline;
}

.section-hint {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  padding: var(--space-10) var(--space-14);
  margin: 0;
  border-top: 1px solid var(--color-border);
}

.section-hint a {
  color: var(--color-primary);
  text-decoration: none;
}

.section-hint a:hover {
  text-decoration: underline;
}

.status-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-10) var(--space-14);
  border-bottom: 1px solid var(--color-border);
}

.status-row:last-child {
  border-bottom: none;
}

.status-label {
  font-size: var(--text-base);
  color: var(--color-text);
}

.status-value {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.status-value.ok {
  color: var(--color-success);
}

@media (max-width: 768px) {
  .setting-input.narrow {
    width: 80px;
  }
}
</style>
