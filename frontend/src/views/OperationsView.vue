<script setup>
/**
 * 运维页（/operations）—— 09 清单的运维能力全部按钮化。
 *
 * 入口：聊天框输入 /operations 回车。
 * 六个区块：系统体检 / 诊断包导出 / 备份与恢复 / 版本与升级 / 升级历史 / 操作审计。
 * 脚本（scripts/*.sh|py）保留为"应用起不来时"的兜底，日常操作全在本页完成。
 */
import { ref, computed, onMounted } from 'vue'
import { apiGet, apiPost, apiDelete } from '../utils/api'

const STATUS_META = {
  pass: { label: '✅ 通过', cls: 'pass' },
  warn: { label: '⚠️ 警告', cls: 'warn' },
  fail: { label: '❌ 失败', cls: 'fail' },
}

// ============ 系统体检 ============
const diagnosing = ref(false)
const diagReport = ref(null)
const diagError = ref('')

async function runDiagnose() {
  diagnosing.value = true
  diagError.value = ''
  diagReport.value = null
  try {
    diagReport.value = await apiPost('/api/ops/diagnose', {})
  } catch (e) {
    diagError.value = e?.message || '体检失败'
  } finally {
    diagnosing.value = false
  }
}

// ============ 诊断包导出 ============
const diagExporting = ref(false)
const diagExportMessage = ref('')

async function exportDiagnostics() {
  diagExporting.value = true
  diagExportMessage.value = ''
  try {
    const res = await fetch('/api/ops/diagnostics')
    if (!res.ok) throw new Error(`导出失败（${res.status}）`)
    const blob = await res.blob()
    const disposition = res.headers.get('Content-Disposition') || ''
    const match = disposition.match(/filename="?([^";]+)"?/)
    const filename = match?.[1] || `aether-diag-${Date.now()}.zip`
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    a.click()
    URL.revokeObjectURL(url)
    diagExportMessage.value = `已生成 ${filename}（${(blob.size / 1024).toFixed(0)} KB），密钥与个人信息已脱敏`
  } catch (e) {
    diagExportMessage.value = e?.message || '导出失败'
  } finally {
    diagExporting.value = false
  }
}

// ============ 备份与恢复 ============
const backups = ref([])
const backupsLoading = ref(false)
const backingUp = ref(false)
const backupMessage = ref('')
// 恢复流程：点恢复 → 预检（显示包内容）→ 二次确认 → 执行 → 等重启
const restoreTarget = ref(null)     // { name, ...validate 结果 }
const restoreValidating = ref(false)
const restoring = ref(false)
const restoreMessage = ref('')
const waitingRestart = ref(false)

async function loadBackups() {
  backupsLoading.value = true
  try {
    backups.value = await apiGet('/api/ops/backups')
  } catch (e) {
    console.error('Failed to load backups:', e)
  } finally {
    backupsLoading.value = false
  }
}

async function createBackup() {
  backingUp.value = true
  backupMessage.value = ''
  try {
    const r = await apiPost('/api/ops/backups', {})
    backupMessage.value = `备份完成：${r.name}（${(r.size_bytes / 1024 / 1024).toFixed(1)} MB），保留最近 3 份`
    await loadBackups()
  } catch (e) {
    backupMessage.value = e?.message || '备份失败'
  } finally {
    backingUp.value = false
  }
}

async function deleteBackup(name) {
  if (!window.confirm(`删除备份 ${name}？不可恢复。`)) return
  try {
    await apiDelete(`/api/ops/backups/${encodeURIComponent(name)}`)
    await loadBackups()
  } catch (e) {
    window.alert(e?.message || '删除失败')
  }
}

async function startRestore(name) {
  restoreValidating.value = true
  restoreTarget.value = null
  restoreMessage.value = ''
  try {
    restoreTarget.value = await apiGet(`/api/ops/backups/${encodeURIComponent(name)}/validate`)
  } catch (e) {
    restoreMessage.value = e?.message || '备份校验失败'
  } finally {
    restoreValidating.value = false
  }
}

function cancelRestore() {
  restoreTarget.value = null
  restoreMessage.value = ''
}

async function confirmRestore() {
  const name = restoreTarget.value?.name
  if (!name) return
  restoring.value = true
  restoreMessage.value = ''
  try {
    await apiPost(`/api/ops/backups/${encodeURIComponent(name)}/restore`, { confirm: true })
    restoreTarget.value = null
    waitingRestart.value = true
    pollUntilBack()
  } catch (e) {
    restoreMessage.value = e?.message || '恢复失败'
  } finally {
    restoring.value = false
  }
}

// ============ 版本与升级 ============
const versionInfo = ref(null)
const uploadPercent = ref(-1)   // -1 未上传
const upgrading = ref(false)
const upgradeMessage = ref('')
const upgradeResult = ref(null)
const fileInput = ref(null)

async function loadVersion() {
  try {
    versionInfo.value = await apiGet('/api/ops/version')
  } catch (e) {
    console.error('Failed to load version:', e)
  }
}

function pickFile() {
  if (versionInfo.value?.docker_socket === 'False') {
    upgradeMessage.value = 'docker.sock 不可用：请按部署文档挂载后再升级（或用 scripts/upgrade.sh）'
    return
  }
  fileInput.value?.click()
}

/** XHR 上传（fetch 拿不到进度，升级包可达 GB 级必须有进度条） */
function onFileChosen(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = ''
  if (!file) return
  if (!file.name.startsWith('aether-update-') || !file.name.endsWith('.tar.gz')) {
    upgradeMessage.value = '请选择 build-update-pack.py 产出的升级包（aether-update-<版本>.tar.gz）'
    return
  }
  upgrading.value = true
  upgradeMessage.value = ''
  upgradeResult.value = null
  uploadPercent.value = 0

  const xhr = new XMLHttpRequest()
  xhr.open('POST', '/api/ops/upgrade')
  xhr.withCredentials = true
  xhr.upload.onprogress = (p) => {
    if (p.lengthComputable) uploadPercent.value = Math.round((p.loaded / p.total) * 100)
  }
  xhr.onload = () => {
    upgrading.value = false
    try {
      const json = JSON.parse(xhr.responseText)
      if (xhr.status >= 200 && xhr.status < 300) {
        upgradeResult.value = json.data
        uploadPercent.value = 100
        waitingRestart.value = true
        pollUntilBack()
      } else {
        upgradeMessage.value = json?.message || json?.detail || `升级失败（HTTP ${xhr.status}）`
        uploadPercent.value = -1
      }
    } catch {
      upgradeMessage.value = `升级失败（HTTP ${xhr.status}）`
      uploadPercent.value = -1
    }
    loadVersion()
  }
  xhr.onerror = () => {
    upgrading.value = false
    upgradeMessage.value = '上传中断（网络错误）'
    uploadPercent.value = -1
  }
  const fd = new FormData()
  fd.append('pack', file)
  xhr.send(fd)
}

// ============ 在线检查更新（更新源通道） ============
const updateUrl = ref('')
const updateUrlSaving = ref(false)
const updateUrlSaved = ref(false)
const updateChecking = ref(false)
const updateInfo = ref(null)      // /api/ops/update/check 的返回
const updateApplying = ref(false)

async function loadUpdateSettings() {
  try {
    const d = await apiGet('/api/ops/update/settings')
    updateUrl.value = d?.manifest_url || ''
  } catch (e) {
    console.error('Failed to load update settings:', e)
  }
}

async function saveUpdateUrl() {
  updateUrlSaving.value = true
  updateUrlSaved.value = false
  try {
    const d = await apiPost('/api/ops/update/settings', { manifest_url: updateUrl.value.trim() })
    updateUrl.value = d?.manifest_url || ''
    updateInfo.value = null
    updateUrlSaved.value = true
    setTimeout(() => { updateUrlSaved.value = false }, 2000)
  } catch (e) {
    upgradeMessage.value = e?.message || '更新源保存失败'
  } finally {
    updateUrlSaving.value = false
  }
}

async function checkUpdate() {
  updateChecking.value = true
  updateInfo.value = null
  upgradeMessage.value = ''
  try {
    updateInfo.value = await apiGet('/api/ops/update/check')
  } catch (e) {
    upgradeMessage.value = e?.message || '检查更新失败'
  } finally {
    updateChecking.value = false
  }
}

async function applyUpdate() {
  const latest = updateInfo.value?.latest
  if (!latest) return
  if (!window.confirm(
    `确定下载并升级到 v${latest.version}？\n` +
    `大小约 ${fmtSize(latest.size_bytes) || '未知'}，完成后服务会自动重启，页面将自动刷新。`
  )) return
  updateApplying.value = true
  upgradeMessage.value = ''
  upgradeResult.value = null
  try {
    upgradeResult.value = await apiPost('/api/ops/update/apply', {})
    waitingRestart.value = true
    pollUntilBack()
  } catch (e) {
    upgradeMessage.value = e?.message || '在线升级失败（可改用上传升级包）'
  } finally {
    updateApplying.value = false
  }
}

// ============ 操作审计 ============
const auditRows = ref([])

async function loadAudit() {
  try {
    auditRows.value = await apiGet('/api/ops/audit')
  } catch (e) {
    console.error('Failed to load audit:', e)
  }
}

// ============ 重启等待 ============
/** 恢复/升级后服务会自动重启：轮询 /api/health 直到回来再刷新页面 */
function pollUntilBack() {
  const started = Date.now()
  const timer = setInterval(async () => {
    if (Date.now() - started > 180000) {
      clearInterval(timer)
      restoreMessage.value = '等待超时：请手动刷新页面确认服务状态'
      waitingRestart.value = false
      return
    }
    try {
      const res = await fetch('/api/health', { credentials: 'include' })
      // 401 = 服务已回（未带有效会话也会 401，但能响应就说明活着）
      if (res.status > 0) {
        clearInterval(timer)
        window.location.reload()
      }
    } catch { /* 服务还在重启，继续等 */ }
  }, 3000)
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return ''
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024).toFixed(0)} KB`
}

onMounted(() => {
  loadBackups()
  loadVersion()
  loadAudit()
  loadUpdateSettings()
})
</script>

<template>
  <div class="ops-page">
    <header class="page-header">
      <h1>运维中心</h1>
      <p class="page-sub">体检、诊断、备份恢复与升级 —— 全部一键完成，无需登录主机操作文件</p>
    </header>

    <!-- 重启等待遮罩（恢复/升级后） -->
    <div v-if="waitingRestart" class="restart-overlay">
      <div class="restart-card">
        <div class="restart-spinner"></div>
        <div class="restart-title">服务重启中…</div>
        <div class="restart-desc">恢复/升级已应用，容器正在以新数据/新版本启动，就绪后页面自动刷新。</div>
      </div>
    </div>

    <!-- 系统体检 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#129514;</span> 系统体检</h2>
      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-label">
            <span class="label-text">部署体检</span>
            <span class="label-desc">端口 / HA / 摄像头 RTSP / DNS / 磁盘内存 / 时间同步，约 10 秒</span>
          </div>
          <button class="btn-primary" :disabled="diagnosing" @click="runDiagnose">
            {{ diagnosing ? '体检中...' : diagReport ? '重新体检' : '运行体检' }}
          </button>
        </div>

        <div v-if="diagError" class="op-message error">{{ diagError }}</div>

        <div v-if="diagReport" class="diag-result">
          <div class="diag-summary">
            <span class="sum pass">{{ diagReport.summary.pass }} 通过</span>
            <span class="sum warn">{{ diagReport.summary.warn }} 警告</span>
            <span class="sum fail">{{ diagReport.summary.fail }} 失败</span>
            <span class="sum-meta">{{ diagReport.created_at }}（{{ diagReport.environment === 'container' ? '容器内' : '主机' }}）</span>
          </div>
          <table class="diag-table">
            <thead><tr><th>检查项</th><th>结果</th><th>详情 / 怎么办</th></tr></thead>
            <tbody>
              <tr v-for="(c, i) in diagReport.checks" :key="i" :class="STATUS_META[c.status]?.cls">
                <td>{{ c.name }}</td>
                <td>{{ STATUS_META[c.status]?.label }}</td>
                <td>
                  {{ c.detail }}
                  <div v-if="c.advice" class="advice">↳ {{ c.advice }}</div>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </section>

    <!-- 诊断包导出 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#128230;</span> 诊断包导出</h2>
      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-label">
            <span class="label-text">导出脱敏诊断包</span>
            <span class="label-desc">配置（密钥/个人信息已打码）+ 最近日志 + 系统信息，发给支持人员远程排障</span>
          </div>
          <button class="btn-primary" :disabled="diagExporting" @click="exportDiagnostics">
            {{ diagExporting ? '打包中...' : '导出' }}
          </button>
        </div>
        <div v-if="diagExportMessage" class="op-message">{{ diagExportMessage }}</div>
      </div>
    </section>

    <!-- 备份与恢复 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#128451;</span> 备份与恢复</h2>
      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-label">
            <span class="label-text">应用侧备份</span>
            <span class="label-desc">
              配置 + 密钥 + 数据库/索引（SQLite 一致性快照），自动保留最近 3 份。<br />
              不含 Home Assistant / MQTT 配置（整机备份在主机跑 scripts/backup.sh）。
            </span>
          </div>
          <button class="btn-primary" :disabled="backingUp" @click="createBackup">
            {{ backingUp ? '备份中...' : '立即备份' }}
          </button>
        </div>
        <div v-if="backupMessage" class="op-message">{{ backupMessage }}</div>
        <div v-if="restoreMessage" class="op-message error">{{ restoreMessage }}</div>

        <!-- 恢复确认弹层 -->
        <div v-if="restoreTarget" class="restore-confirm">
          <div class="restore-title">确认恢复 —— {{ restoreTarget.name }}</div>
          <div class="restore-body">
            包内包含：{{ restoreTarget.has_config ? '✅ 系统配置 ' : '' }}{{ restoreTarget.has_env ? '✅ 密钥(.env) ' : '' }}{{ restoreTarget.has_data ? '✅ 数据库与索引' : '' }}（{{ restoreTarget.entry_count }} 个条目）。<br />
            <b>当前的全部聊天记录、账号、个性化设置会回到备份时点</b>，登录会话将失效（JWT 密钥一并恢复），服务随后自动重启。
          </div>
          <div class="restore-actions">
            <button class="btn-ghost" :disabled="restoring" @click="cancelRestore">取消</button>
            <button class="btn-danger" :disabled="restoring" @click="confirmRestore">
              {{ restoring ? '恢复中...' : '确认恢复并重启' }}
            </button>
          </div>
        </div>

        <div v-if="backupsLoading" class="op-muted">加载中…</div>
        <table v-else-if="backups.length" class="diag-table">
          <thead><tr><th>备份</th><th>大小</th><th>时间</th><th style="width:160px">操作</th></tr></thead>
          <tbody>
            <tr v-for="b in backups" :key="b.name">
              <td>{{ b.name }}</td>
              <td>{{ fmtSize(b.size_bytes) }}</td>
              <td>{{ b.created_at }}</td>
              <td>
                <button class="btn-mini" :disabled="restoreValidating || restoring" @click="startRestore(b.name)">恢复</button>
                <button class="btn-mini danger" :disabled="restoring" @click="deleteBackup(b.name)">删除</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="op-muted">暂无备份。建议定期点击「立即备份」，或配置主机 cron。</div>
      </div>
    </section>

    <!-- 版本与升级 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#128640;</span> 版本与升级</h2>
      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-label">
            <span class="label-text">当前版本：v{{ versionInfo?.version || '…' }}</span>
            <span class="label-desc">
              离线升级：上传 build-update-pack.py 产出的升级包，自动校验 → 导入镜像 → 重启生效。<br />
              升级前自动核对 sha256 与版本兼容性；失败不影响当前运行。SSH 场景也可用 scripts/upgrade.sh。
            </span>
          </div>
          <div>
            <input ref="fileInput" type="file" accept=".tar.gz" style="display:none" @change="onFileChosen" />
            <button class="btn-primary" :disabled="upgrading || waitingRestart" @click="pickFile">
              {{ upgrading ? `上传中 ${uploadPercent}%` : '上传升级包' }}
            </button>
          </div>
        </div>

        <!-- 在线更新源：配置后可一键检查/升级，地址留空则只用手动上传 -->
        <div class="setting-row update-source-row">
          <div class="setting-label">
            <span class="label-text">更新源（可选）</span>
            <span class="label-desc">
              任意静态 HTTP 地址，放 build-update-pack.py 产出的 update-channel.json 与升级包（OSS/COS/GitHub Releases 均可）。配置后可在线检查并一键升级。
            </span>
          </div>
          <div class="update-source-controls">
            <input
              v-model="updateUrl"
              type="text"
              class="update-url-input"
              placeholder="https://你的存储/update-channel.json"
            />
            <button class="btn-secondary" :disabled="updateUrlSaving" @click="saveUpdateUrl">
              {{ updateUrlSaved ? '已保存' : '保存' }}
            </button>
            <button class="btn-primary" :disabled="updateChecking || !updateUrl.trim()" @click="checkUpdate">
              {{ updateChecking ? '检查中…' : '检查更新' }}
            </button>
          </div>
        </div>

        <div v-if="updateInfo?.status === 'up_to_date'" class="op-message success">
          已是最新版本 v{{ updateInfo.current }}。
        </div>
        <div v-else-if="updateInfo?.status === 'available'" class="update-available">
          <div class="update-available-head">
            <span>发现新版本 <b>v{{ updateInfo.latest.version }}</b>
              <template v-if="updateInfo.latest.size_bytes">（约 {{ fmtSize(updateInfo.latest.size_bytes) }}）</template>
            </span>
            <button
              class="btn-primary"
              :disabled="updateApplying || waitingRestart || updateInfo.docker_socket === false"
              @click="applyUpdate"
            >{{ updateApplying ? '下载并升级中…' : '下载并升级' }}</button>
          </div>
          <p v-if="updateInfo.latest.notes" class="update-notes">{{ updateInfo.latest.notes }}</p>
          <p v-if="updateInfo.docker_socket === false" class="update-notes warn">
            docker.sock 未挂载，在线升级不可用：请按部署文档挂载，或下载升级包后用「上传升级包」。
          </p>
        </div>
        <div v-else-if="updateInfo?.status === 'incompatible'" class="op-message error">
          {{ updateInfo.message }}
        </div>
        <div v-else-if="updateInfo?.status === 'error'" class="op-message error">
          {{ updateInfo.message }}
        </div>

        <div v-if="upgrading" class="upload-progress">
          <div class="upload-bar"><div class="upload-fill" :style="{ width: uploadPercent + '%' }"></div></div>
          <span class="upload-text">{{ uploadPercent }}% · 校验与导入在传输完成后自动进行，请勿关闭页面</span>
        </div>
        <div v-if="upgradeMessage" class="op-message error">{{ upgradeMessage }}</div>
        <div v-if="upgradeResult" class="op-message success">
          升级包已应用：v{{ upgradeResult.from_version }} → v{{ upgradeResult.to_version }}，服务重启中。
        </div>

        <table v-if="versionInfo?.history?.length" class="diag-table">
          <thead><tr><th>时间</th><th>版本变化</th><th>操作人</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="(h, i) in versionInfo.history" :key="i">
              <td>{{ h.ts }}</td>
              <td>v{{ h.from_version }} → v{{ h.to_version }}</td>
              <td>{{ h.operator }}</td>
              <td>{{ h.notes }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 操作审计 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#128220;</span> 操作审计</h2>
      <div class="setting-card">
        <div class="op-muted" style="margin-bottom:8px">谁、何时、执行了什么运维操作（最近 50 条，交付验收依据）</div>
        <table v-if="auditRows.length" class="diag-table">
          <thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>详情</th></tr></thead>
          <tbody>
            <tr v-for="(a, i) in auditRows" :key="i">
              <td>{{ a.ts }}</td>
              <td>{{ a.operator }}</td>
              <td>{{ a.action }}</td>
              <td class="audit-detail">{{ JSON.stringify(a.detail) }}</td>
            </tr>
          </tbody>
        </table>
        <div v-else class="op-muted">暂无记录</div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.ops-page {
  max-width: 960px;
  margin: 0 auto;
  padding: var(--space-24) var(--space-16) var(--space-48);
}

.page-header { margin-bottom: var(--space-24); }
.page-header h1 { font-size: var(--text-2xl); margin-bottom: var(--space-4); }
.page-sub { color: var(--color-text-secondary); font-size: var(--text-sm); }

.setting-section { margin-bottom: var(--space-24); }
.section-title {
  font-size: var(--text-lg);
  margin-bottom: var(--space-12);
  display: flex;
  align-items: center;
  gap: var(--space-8);
}
.setting-card {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-xl);
  padding: var(--space-16) var(--space-20);
}
.setting-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--space-16);
}
.setting-label { display: flex; flex-direction: column; gap: var(--space-4); }
.label-text { font-weight: var(--weight-medium); }
.label-desc { font-size: var(--text-xs); color: var(--color-text-tertiary); line-height: 1.6; }

.btn-primary {
  padding: var(--space-8) var(--space-16);
  background: var(--color-primary);
  color: white;
  border: none;
  border-radius: var(--radius-lg);
  cursor: pointer;
  white-space: nowrap;
}
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }

.btn-ghost {
  padding: var(--space-6) var(--space-14);
  background: transparent;
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  cursor: pointer;
}

.btn-danger {
  padding: var(--space-6) var(--space-14);
  background: var(--color-danger);
  color: white;
  border: none;
  border-radius: var(--radius-lg);
  cursor: pointer;
}

.btn-mini {
  padding: 2px var(--space-10);
  font-size: var(--text-xs);
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  cursor: pointer;
  margin-right: var(--space-6);
}
.btn-mini.danger { background: var(--color-danger-bg); color: var(--color-danger); }

.op-message {
  margin-top: var(--space-12);
  padding: var(--space-8) var(--space-12);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  background: var(--color-bg);
}
.op-message.error { background: var(--color-danger-bg); color: var(--color-danger); }
.op-message.success { background: var(--color-primary-light); color: var(--color-success); }
.op-muted { color: var(--color-text-tertiary); font-size: var(--text-xs); }

/* 体检结果 */
.diag-result { margin-top: var(--space-16); }
.diag-summary { display: flex; gap: var(--space-12); align-items: baseline; margin-bottom: var(--space-8); }
.sum { font-weight: var(--weight-medium); }
.sum.pass { color: var(--color-success); }
.sum.warn { color: var(--color-warning); }
.sum.fail { color: var(--color-danger); }
.sum-meta { color: var(--color-text-tertiary); font-size: var(--text-xs); margin-left: auto; }

.diag-table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
.diag-table th, .diag-table td {
  padding: var(--space-8) var(--space-10);
  border-bottom: 1px solid var(--color-border);
  text-align: left;
  vertical-align: top;
}
.diag-table th { color: var(--color-text-tertiary); font-weight: var(--weight-medium); font-size: var(--text-xs); }
.diag-table tr.pass td:first-child { color: var(--color-success); }
.diag-table tr.warn td:first-child { color: var(--color-warning); }
.diag-table tr.fail td:first-child { color: var(--color-danger); }
.advice { color: var(--color-text-tertiary); font-size: var(--text-xs); margin-top: var(--space-4); }
.audit-detail { font-family: monospace; font-size: var(--text-xs); color: var(--color-text-secondary); }

/* 恢复确认 */
.restore-confirm {
  margin-top: var(--space-12);
  padding: var(--space-16);
  border: 1px solid var(--color-danger);
  border-radius: var(--radius-lg);
  background: var(--color-danger-bg);
}
.restore-title { font-weight: var(--weight-medium); color: var(--color-danger); margin-bottom: var(--space-8); }
.restore-body { font-size: var(--text-sm); line-height: 1.7; margin-bottom: var(--space-12); }
.restore-actions { display: flex; justify-content: flex-end; gap: var(--space-12); }

/* 上传进度 */
.upload-progress { margin-top: var(--space-12); }
.upload-bar {
  height: 8px;
  background: var(--color-border);
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: var(--space-6);
}
.upload-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width 0.3s;
}
.upload-text { font-size: var(--text-xs); color: var(--color-text-secondary); }

/* 在线更新源 */
.btn-secondary {
  padding: var(--space-8) var(--space-16);
  background: var(--color-bg);
  color: var(--color-text-secondary);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all 0.2s;
}
.btn-secondary:hover:not(:disabled) { border-color: var(--color-border-hover); color: var(--color-text); }
.btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }

.update-source-row { align-items: flex-start; }
.update-source-controls {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  flex-wrap: wrap;
}
.update-url-input {
  flex: 1;
  min-width: 260px;
  padding: var(--space-8) var(--space-10);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  color: var(--color-text);
  font-size: var(--text-sm);
}
.update-url-input:focus { outline: none; border-color: var(--color-primary); }

.update-available {
  margin-top: var(--space-12);
  padding: var(--space-12);
  background: var(--color-primary-light);
  border: 1px solid var(--color-primary);
  border-radius: var(--radius-md);
}
.update-available-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-12);
  font-size: var(--text-sm);
}
.update-notes {
  margin: var(--space-8) 0 0;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  line-height: 1.6;
}
.update-notes.warn { color: var(--color-warning); }

/* 重启遮罩 */
.restart-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.restart-card {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  padding: var(--space-32);
  text-align: center;
  max-width: 360px;
}
.restart-spinner {
  width: 36px;
  height: 36px;
  border: 3px solid var(--color-border);
  border-top-color: var(--color-primary);
  border-radius: 50%;
  margin: 0 auto var(--space-16);
  animation: spin 1s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.restart-title { font-weight: var(--weight-medium); margin-bottom: var(--space-8); }
.restart-desc { font-size: var(--text-xs); color: var(--color-text-secondary); line-height: 1.6; }
</style>
