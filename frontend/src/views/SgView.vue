<script setup>
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { useRouter } from 'vue-router'
import { apiGet, apiPost } from '../utils/api'

const router = useRouter()

const config = ref(null)
const form = ref({ threshold: 0.7, max_workers: 8, umap_n_neighbors: 15, umap_min_dist: 0.1 })
const saving = ref(false)
const saveMsg = ref('')
const status = ref({ status: 'idle', progress: 0, message: '' })
const latest = ref(null)
const loadingConfig = ref(true)
const building = ref(false)
let pollTimer = null

const isRunning = computed(() => status.value.status === 'running')
const statusText = computed(() => {
  const s = status.value.status
  return { idle: '空闲', running: '构建中', done: '已完成', error: '出错' }[s] || s
})

async function loadConfig() {
  try {
    loadingConfig.value = true
    config.value = await apiGet('/api/sg/config')
    // 同步可编辑参数到表单
    form.value = {
      threshold: config.value.threshold,
      max_workers: config.value.max_workers,
      umap_n_neighbors: config.value.umap_n_neighbors,
      umap_min_dist: config.value.umap_min_dist,
    }
  } catch (e) {
    console.error('Failed to load sg config:', e)
  } finally {
    loadingConfig.value = false
  }
}

async function saveConfig() {
  saving.value = true
  saveMsg.value = ''
  try {
    const res = await apiPost('/api/sg/config', form.value)
    saveMsg.value = res.saved ? '已保存，下次构建生效' : (res.message || '未修改')
    // 重新加载以同步展示值
    await loadConfig()
    setTimeout(() => { saveMsg.value = '' }, 3000)
  } catch (e) {
    saveMsg.value = '保存失败: ' + (e.message || e)
  } finally {
    saving.value = false
  }
}

async function loadStatus() {
  try {
    status.value = await apiGet('/api/sg/status')
    building.value = status.value.status === 'running'
  } catch (e) {
    console.error('Failed to load sg status:', e)
  }
}

async function loadLatest() {
  try {
    latest.value = await apiGet('/api/sg/latest')
  } catch (e) {
    // 404 = 尚无产物，正常情况
    latest.value = null
  }
}

async function startBuild() {
  building.value = true
  status.value = { status: 'running', progress: 0, message: '启动构建...' }
  try {
    await apiPost('/api/sg/build')
    startPolling()
  } catch (e) {
    building.value = false
    status.value = { status: 'error', progress: 0, message: '启动失败: ' + (e.message || e) }
  }
}

async function cancelBuild() {
  try {
    await apiPost('/api/sg/cancel')
  } catch (e) {
    console.error('cancel failed:', e)
  }
}

function startPolling() {
  stopPolling()
  pollTimer = setInterval(async () => {
    await loadStatus()
    if (status.value.status === 'done' || status.value.status === 'error' || status.value.status === 'idle') {
      stopPolling()
      building.value = false
      if (status.value.status === 'done') {
        await loadLatest()
      }
    }
  }, 2000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function goToSphere() {
  router.push('/doc/KGraph')
}

function goToDoc() {
  router.push('/doc')
}

onMounted(() => {
  loadConfig()
  loadStatus()
  loadLatest()
})

onUnmounted(() => {
  stopPolling()
})
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div class="header-left">
        <h1>语义图</h1>
        <p class="page-sub">基于你配置的 Embed / LLM 构建文档语义图，复用同一向量模型保证 RAG 与图谱一致</p>
      </div>
    </header>

    <!-- 配置概览 -->
    <div class="setting-card">
      <div class="card-title">当前后端</div>
      <div v-if="loadingConfig" class="muted">加载中...</div>
      <div v-else-if="config" class="config-grid">
        <div class="config-item">
          <span class="config-label">向量模型</span>
          <span class="config-value" :class="{ unset: !config.embed_model }">
            {{ config.embed_model || '未配置（请在「高级」页面添加 type=embed 的 key）' }}
          </span>
        </div>
        <div class="config-item">
          <span class="config-label">LLM 模型</span>
          <span class="config-value" :class="{ unset: !config.chat_model }">
            {{ config.chat_model || '未配置（请在「高级」页面添加 type=chat 的 key）' }}
          </span>
        </div>
        <div class="config-item">
          <span class="config-label">就绪状态</span>
          <span class="config-value" :class="config.ready ? 'ok' : 'unset'">
            {{ config.ready ? '✓ 可构建' : '✗ 缺少 key' }}
          </span>
        </div>
      </div>
    </div>

    <!-- 构建参数（可编辑） -->
    <div v-if="config" class="setting-card">
      <div class="card-title">构建参数 <span class="card-hint">修改后下次构建生效</span></div>
      <div class="param-grid">
        <label class="param-item">
          <span class="param-label">相似度阈值 threshold</span>
          <input class="param-input" type="number" step="0.05" min="0" max="1" v-model.number="form.threshold" />
          <span class="param-desc">越高越严格，送 LLM 分析的文档对越少</span>
        </label>
        <label class="param-item">
          <span class="param-label">并发线程 max_workers</span>
          <input class="param-input" type="number" step="1" min="1" max="32" v-model.number="form.max_workers" />
          <span class="param-desc">LLM 实体抽取/关系分析的并发数</span>
        </label>
        <label class="param-item">
          <span class="param-label">UMAP 邻居 n_neighbors</span>
          <input class="param-input" type="number" step="1" min="2" max="100" v-model.number="form.umap_n_neighbors" />
          <span class="param-desc">大值偏全局结构，小值偏局部簇</span>
        </label>
        <label class="param-item">
          <span class="param-label">UMAP 最小距离 min_dist</span>
          <input class="param-input" type="number" step="0.01" min="0" max="0.99" v-model.number="form.umap_min_dist" />
          <span class="param-desc">越小同簇点越挤，越大越散开</span>
        </label>
      </div>
      <div class="param-actions">
        <button class="btn-save" :disabled="saving" @click="saveConfig">
          {{ saving ? '保存中...' : '保存参数' }}
        </button>
        <span v-if="saveMsg" class="save-msg">{{ saveMsg }}</span>
      </div>
      <div class="locked-params">
        <span class="locked-title">固定参数（无需调整）：</span>
        <span class="locked-chip">PCA {{ config.pca_dim }} 维</span>
        <span class="locked-chip">UMAP {{ config.umap_n_components }}D</span>
        <span class="locked-chip">{{ config.umap_n_epochs }} 轮</span>
        <span class="locked-chip">段落 {{ config.max_paragraph_chars }} 字</span>
      </div>
    </div>

    <!-- 构建控制 -->
    <div class="setting-card">
      <div class="card-title">构建</div>
      <div class="build-actions">
        <button
          class="btn-build"
          :disabled="building || (config && !config.ready)"
          @click="startBuild"
        >
          {{ building ? '构建中...' : '构建语义图' }}
        </button>
        <button
          v-if="building"
          class="btn-cancel"
          @click="cancelBuild"
        >
          取消
        </button>
      </div>

      <!-- 进度 -->
      <div v-if="status.status !== 'idle'" class="progress-wrap">
        <div class="progress-bar">
          <div class="progress-fill" :style="{ width: status.progress + '%' }"></div>
        </div>
        <div class="progress-meta">
          <span class="progress-status" :class="'st-' + status.status">{{ statusText }}</span>
          <span class="progress-pct">{{ status.progress }}%</span>
        </div>
        <div v-if="status.message" class="progress-msg">{{ status.message }}</div>
      </div>
    </div>

    <!-- 最新产物 -->
    <div class="setting-card">
      <div class="card-title">最新产物</div>
      <div v-if="latest" class="latest-grid">
        <div class="config-item">
          <span class="config-label">节点数</span>
          <span class="config-value">{{ latest.node_count }}</span>
        </div>
        <div class="config-item">
          <span class="config-label">边数</span>
          <span class="config-value">{{ latest.edge_count }}</span>
        </div>
        <div class="config-item">
          <span class="config-label">构建批次</span>
          <span class="config-value">{{ latest.task_dir }}</span>
        </div>
        <div class="latest-actions">
          <button class="btn-link" @click="goToSphere">查看 3D 球 →</button>
          <button class="btn-link" @click="goToDoc">文档对话 →</button>
        </div>
      </div>
      <div v-else class="muted">尚无产物，点击上方「构建语义图」生成。</div>
    </div>
  </div>
</template>

<style scoped>
.page {
  padding: var(--space-20) var(--space-24);
  max-width: 860px;
  margin: 0 auto;
}

.page-header {
  margin-bottom: var(--space-16);
}

.page-header--split {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.page-header h1 {
  font-size: var(--text-2xl);
  font-weight: var(--weight-bold);
  color: var(--color-text);
  margin: 0 0 var(--space-2);
}

.page-sub {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  margin: 0;
}

.setting-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-16);
  margin-bottom: var(--space-12);
}

.card-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin-bottom: var(--space-10);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.muted {
  font-size: var(--text-sm);
  color: var(--color-text-muted);
}

.config-grid,
.latest-grid {
  display: flex;
  flex-direction: column;
  gap: var(--space-8);
}

.config-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-8);
}

.config-label {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.config-value {
  font-size: var(--text-sm);
  color: var(--color-text);
  font-weight: var(--weight-medium);
  text-align: right;
  word-break: break-all;
}

.config-value.unset {
  color: var(--color-text-muted);
  font-weight: var(--weight-normal);
}

.config-value.ok {
  color: var(--color-success);
}

.card-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  font-weight: var(--weight-normal);
  text-transform: none;
  letter-spacing: 0;
  margin-left: var(--space-6);
}

.param-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--space-12);
  margin-bottom: var(--space-12);
}

.param-item {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.param-label {
  font-size: var(--text-sm);
  color: var(--color-text-tertiary);
}

.param-input {
  background: var(--color-surface-hover);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-6) var(--space-10);
  font-size: var(--text-sm);
  color: var(--color-text);
  font-variant-numeric: tabular-nums;
  transition: border-color var(--duration-normal) var(--ease-out);
}

.param-input:focus {
  outline: none;
  border-color: var(--color-primary);
}

.param-desc {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.param-actions {
  display: flex;
  align-items: center;
  gap: var(--space-10);
  margin-bottom: var(--space-12);
}

.btn-save {
  background: var(--color-primary);
  color: #fff;
  border: none;
  padding: var(--space-6) var(--space-14);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.btn-save:hover:not(:disabled) {
  background: var(--color-primary-dark);
}

.btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.save-msg {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.locked-params {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-6);
  padding-top: var(--space-10);
  border-top: 1px solid var(--color-border);
}

.locked-title {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.locked-chip {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  background: var(--color-surface-hover);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-full);
  padding: var(--space-2) var(--space-8);
}

.build-actions {
  display: flex;
  gap: var(--space-8);
  margin-bottom: var(--space-8);
}

.btn-build {
  background: linear-gradient(135deg, var(--color-primary), var(--color-primary-dark));
  color: #fff;
  border: none;
  padding: var(--space-8) var(--space-16);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.btn-build:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 4px 16px rgba(74, 124, 112, 0.25);
}

.btn-build:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-cancel {
  background: transparent;
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-border);
  padding: var(--space-8) var(--space-16);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.btn-cancel:hover {
  color: var(--color-danger);
  border-color: rgba(231, 76, 60, 0.3);
  background: rgba(231, 76, 60, 0.08);
}

.progress-wrap {
  margin-top: var(--space-10);
}

.progress-bar {
  height: 6px;
  background: var(--color-border);
  border-radius: var(--radius-full);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--color-primary), var(--color-primary-dark));
  border-radius: var(--radius-full);
  transition: width 0.4s var(--ease-out);
}

.progress-meta {
  display: flex;
  justify-content: space-between;
  margin-top: var(--space-4);
  font-size: var(--text-xs);
}

.progress-status {
  font-weight: var(--weight-medium);
}

.progress-status.st-running { color: var(--color-primary); }
.progress-status.st-done { color: var(--color-success); }
.progress-status.st-error { color: var(--color-danger); }

.progress-pct {
  color: var(--color-text-muted);
  font-variant-numeric: tabular-nums;
}

.progress-msg {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  margin-top: var(--space-2);
}

.latest-actions {
  display: flex;
  gap: var(--space-10);
  margin-top: var(--space-6);
  flex-wrap: wrap;
}

.btn-link {
  background: transparent;
  color: var(--color-primary);
  border: 1px solid var(--color-primary);
  padding: var(--space-5) var(--space-12);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.btn-link:hover {
  background: var(--color-primary-light);
}
</style>
