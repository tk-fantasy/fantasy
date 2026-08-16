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
    loadAudit()
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
    loadAudit()
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
    loadAudit()
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
    backupMessage.value = `已删除 ${name}`
    await loadBackups()
    loadAudit()
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
const upgradeMessage = ref('')
const upgradeResult = ref(null)

async function loadVersion() {
  try {
    versionInfo.value = await apiGet('/api/ops/version')
  } catch (e) {
    console.error('Failed to load version:', e)
  }
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
    loadAudit()
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
    upgradeMessage.value = e?.message || '在线升级失败'
  } finally {
    updateApplying.value = false
  }
}

// ============ git 一键升级（Gitee） ============
const gitInfo = ref(null)          // GET /ops/update/git
const gitToken = ref('')
const gitRepoPath = ref('')
const gitSaving = ref(false)
const gitChecking = ref(false)
const gitCheckInfo = ref(null)     // check 结果
const gitApplying = ref(false)
const gitLog = ref('')

async function loadGitSettings() {
  try {
    gitInfo.value = await apiGet('/api/ops/update/git')
    gitRepoPath.value = gitInfo.value?.repo_path || ''
  } catch (e) {
    console.error('Failed to load git settings:', e)
  }
}

async function saveGitSettings() {
  gitSaving.value = true
  try {
    await apiPost('/api/ops/update/git', { token: gitToken.value.trim(), repo_path: gitRepoPath.value.trim() })
    gitToken.value = ''   // 不留在输入框
    gitCheckInfo.value = null
    await loadGitSettings()
    loadAudit()
  } catch (e) {
    upgradeMessage.value = e?.message || '保存失败'
  } finally {
    gitSaving.value = false
  }
}

async function checkGitUpdate() {
  gitChecking.value = true
  gitCheckInfo.value = null
  upgradeMessage.value = ''
  try {
    gitCheckInfo.value = await apiPost('/api/ops/update/git/check', {})
  } catch (e) {
    upgradeMessage.value = e?.message || '检查更新失败'
    const s = await apiGet('/api/ops/update/git/status').catch(() => null)
    if (s?.log_tail) gitLog.value = s.log_tail
  } finally {
    gitChecking.value = false
  }
}

/** git 升级是同步长请求（重建可达数分钟），期间服务会断连：
 *  请求一发出就盖上重启遮罩，响应/断连都交给 pollUntilBack 处理 */
async function applyGitUpdate() {
  const info = gitCheckInfo.value
  const tip = info?.status === 'available'
    ? `确定从 Gitee 拉取最新代码并升级？\n当前 ${info.current_commit} → 远程 ${info.remote_commit}（落后 ${info.behind} 个提交）。\n升级会在主机上重新构建镜像（约数分钟），期间服务会重启、页面自动刷新。`
    : '确定从 Gitee 拉取最新代码并升级？\n升级会在主机上重新构建镜像（约数分钟），期间服务会重启、页面自动刷新。'
  if (!window.confirm(tip)) return
  gitApplying.value = true
  waitingRestart.value = true
  pollUntilBack(15 * 60 * 1000)
  try {
    await apiPost('/api/ops/update/git/apply', {})
  } catch (e) {
    // 网络断连（服务重建中）不是失败；真正的失败等 pollUntilBack 后看日志
    if (e?.message && !/network|fetch|Failed to fetch/i.test(e.message)) {
      upgradeMessage.value = e?.message || 'git 升级失败'
      waitingRestart.value = false
    }
  } finally {
    gitApplying.value = false
  }
}


// ============ 升级包分发（导出 / 本地安装） ============
// 发布方：导出当前运行版本为升级包，浏览器下载后微信/网盘发给接收方
const packExport = ref(null)        // /update-pack/export/status
let packExportTimer = null

const packExportPercent = computed(() => {
  const s = packExport.value
  if (!s?.total_bytes) return 0
  return Math.min(100, Math.round((s.staged_bytes / s.total_bytes) * 100))
})

async function pollPackExport() {
  try {
    packExport.value = await apiGet('/api/ops/update-pack/export/status')
  } catch (e) {
    console.error('Failed to poll pack export:', e)
  }
  const s = packExport.value
  if (s?.status === 'running') {
    packExportTimer = setTimeout(pollPackExport, 2000)
  } else if (s?.status === 'done' || s?.status === 'error') {
    clearTimeout(packExportTimer)
    loadAudit()
  }
}

async function startPackExport() {
  try {
    await apiPost('/api/ops/update-pack/export', {})
    pollPackExport()
  } catch (e) {
    upgradeMessage.value = e?.message || '导出启动失败'
  }
}

// 接收方：把收到的包放进 Aether/backups/，这里识别并一键安装
const localPacks = ref([])
const localPacksLoading = ref(false)
const installingPack = ref('')

async function loadLocalPacks() {
  localPacksLoading.value = true
  try {
    localPacks.value = await apiGet('/api/ops/update-pack/local')
  } catch (e) {
    console.error('Failed to load local packs:', e)
  } finally {
    localPacksLoading.value = false
  }
}

async function installPack(name) {
  if (!window.confirm(
    `确定安装升级包 ${name}？\n` +
    '系统会自动校验（sha256 / 版本兼容）→ 导入镜像 → 重启服务，安装成功后自动删除该包。'
  )) return
  installingPack.value = name
  upgradeMessage.value = ''
  try {
    await apiPost(`/api/ops/update-pack/local/${encodeURIComponent(name)}/apply`, {})
    waitingRestart.value = true
    pollUntilBack()
  } catch (e) {
    upgradeMessage.value = e?.message || '安装失败（校验不通过或 docker 异常）'
  } finally {
    installingPack.value = ''
  }
}


const auditRows = ref([])
const auditClearing = ref(false)

async function loadAudit() {
  try {
    auditRows.value = await apiGet('/api/ops/audit')
  } catch (e) {
    console.error('Failed to load audit:', e)
  }
}

async function clearAudit() {
  if (!window.confirm('确定清空全部操作审计记录？此操作不可恢复（清空动作本身会留下一条记录）。')) return
  auditClearing.value = true
  try {
    await apiDelete('/api/ops/audit')
    await loadAudit()
  } catch (e) {
    window.alert(e?.message || '清空失败')
  } finally {
    auditClearing.value = false
  }
}

// ============ 重启等待 ============
/** 恢复/升级后服务会自动重启：轮询 /api/health 直到回来再刷新页面 */
function pollUntilBack(timeoutMs = 180000) {
  const started = Date.now()
  const timer = setInterval(async () => {
    if (Date.now() - started > timeoutMs) {
      clearInterval(timer)
      restoreMessage.value = '等待超时：请手动刷新页面确认服务状态（git 升级可在 logs/git-update.log 查看进度）'
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
  loadGitSettings()
  pollPackExport()      // 恢复上次未完成的导出进度轮询
  loadLocalPacks()
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
              升级方式：在主机执行 ./scripts/update-from-git.sh —— git 拉取最新代码 → 重建容器 → 健康自检，失败自动回退。<br />
              也可配置下方更新源在线升级（需挂载 docker.sock）。
            </span>
          </div>
        </div>

        <!-- git 一键升级（Gitee）：填一次令牌，之后网页上点一下即拉取+重建 -->
        <div class="setting-row update-source-row">
          <div class="setting-label">
            <span class="label-text">git 一键升级（Gitee）
              <template v-if="gitInfo?.token_configured"><span class="git-token-ok">✓ 令牌已配置</span></template>
            </span>
            <span class="label-desc">
              填一次 Gitee 私有令牌（仓库 → 管理 → 私人令牌，勾选 projects 只读权限即可），
              之后「检查更新 → 一键升级」自动完成：拉取代码 → 重建容器 → 健康自检，失败自动回退上一提交。
              <template v-if="gitInfo?.repo_path"><br />仓库：{{ gitInfo.repo_path }}</template>
              <template v-if="gitInfo?.repo_error"><br /><span class="warn-text">{{ gitInfo.repo_error }}</span></template>
            </span>
          </div>
        </div>
        <div class="git-controls">
          <input
            v-model="gitToken"
            type="password"
            class="update-url-input"
            :placeholder="gitInfo?.token_configured ? '令牌已保存（留空保持不变）' : 'Gitee 私人令牌'"
            autocomplete="new-password"
          />
          <input
            v-model="gitRepoPath"
            type="text"
            class="update-url-input"
            placeholder="宿主仓库绝对路径（通常自动探测，无需填）"
          />
          <button class="btn-secondary" :disabled="gitSaving" @click="saveGitSettings">
            {{ gitSaving ? '保存中…' : '保存' }}
          </button>
          <button
            class="btn-primary"
            :disabled="gitChecking || !gitInfo?.token_configured || gitInfo?.docker_socket === 'False'"
            @click="checkGitUpdate"
          >{{ gitChecking ? '检查中…' : '检查更新' }}</button>
          <button
            v-if="gitCheckInfo?.status === 'available'"
            class="btn-danger"
            :disabled="gitApplying || waitingRestart"
            @click="applyGitUpdate"
          >{{ gitApplying ? '升级中…' : '一键升级' }}</button>
        </div>

        <div v-if="gitCheckInfo?.status === 'up_to_date'" class="op-message success">
          已是最新（{{ gitCheckInfo.current_commit }}），无需更新。
        </div>
        <div v-else-if="gitCheckInfo?.status === 'available'" class="update-available">
          <div class="update-available-head">
            <span>发现新提交：<b>{{ gitCheckInfo.current_commit }}</b> → <b>{{ gitCheckInfo.remote_commit }}</b>（落后 {{ gitCheckInfo.behind }} 个提交）</span>
            <button class="btn-danger" :disabled="gitApplying || waitingRestart" @click="applyGitUpdate">
              {{ gitApplying ? '升级中…' : '一键升级' }}
            </button>
          </div>
        </div>
        <div v-else-if="gitCheckInfo?.status === 'error'" class="op-message error">
          {{ gitCheckInfo.message }}
        </div>
        <div v-if="gitLog" class="git-log">
          <div class="git-log-title">最近一次升级日志（logs/git-update.log 尾部）：</div>
          <pre>{{ gitLog }}</pre>
        </div>

        <!-- 在线更新源：配置后可一键检查/升级，地址留空则不启用 -->
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
            docker.sock 未挂载，在线升级不可用：请按部署文档挂载，或在主机执行 scripts/update-from-git.sh 升级。
          </p>
        </div>
        <div v-else-if="updateInfo?.status === 'incompatible'" class="op-message error">
          {{ updateInfo.message }}
        </div>
        <div v-else-if="updateInfo?.status === 'error'" class="op-message error">
          {{ updateInfo.message }}
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

    <!-- 升级包分发：发布方导出 / 接收方本地安装 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#128229;</span> 升级包分发</h2>
      <div class="setting-card">
        <div class="setting-row">
          <div class="setting-label">
            <span class="label-text">导出当前版本（发给别人）</span>
            <span class="label-desc">
              把当前运行的版本打成升级包（含镜像 + 校验信息），下载后微信/网盘发给对方。<br />
              对方收到后放入其服务器 Aether/backups/ 目录，在下方「安装收到的升级包」一键安装。
            </span>
          </div>
          <div class="pack-export-actions">
            <button
              v-if="packExport?.status === 'done'"
              class="btn-secondary"
              @click="loadLocalPacks"
            >刷新列表</button>
            <a
              v-if="packExport?.status === 'done'"
              class="btn-primary pack-download-btn"
              href="/api/ops/update-pack/download"
            >下载 {{ packExport.file }}</a>
            <button
              v-else
              class="btn-primary"
              :disabled="packExport?.status === 'running'"
              @click="startPackExport"
            >{{ packExport?.status === 'running' ? `导出中 ${packExportPercent}%` : '一键导出升级包' }}</button>
          </div>
        </div>
        <div v-if="packExport?.status === 'running'" class="upload-progress">
          <div class="upload-bar"><div class="upload-fill" :style="{ width: packExportPercent + '%' }"></div></div>
          <span class="upload-text">正在导出镜像（{{ fmtSize(packExport.staged_bytes) }}{{ packExport.total_bytes ? ' / ' + fmtSize(packExport.total_bytes) : '' }}），导出+压缩约需数分钟，请勿关闭页面</span>
        </div>
        <div v-if="packExport?.status === 'error'" class="op-message error">
          导出失败：{{ packExport.error }}
        </div>

        <div class="setting-row" style="margin-top:16px">
          <div class="setting-label">
            <span class="label-text">安装收到的升级包</span>
            <span class="label-desc">
              把收到的 aether-update-*.tar.gz 放到本服务器 Aether/backups/ 目录（即备份列表同目录），点刷新识别后安装。<br />
              自动校验 sha256 与版本兼容性 → 导入镜像 → 重启生效，安装成功后自动删除包文件。
            </span>
          </div>
          <button class="btn-secondary" :disabled="localPacksLoading" @click="loadLocalPacks">
            {{ localPacksLoading ? '扫描中…' : '刷新列表' }}
          </button>
        </div>
        <div v-if="localPacks.length" class="diag-table-wrap">
          <table class="diag-table">
            <thead><tr><th>升级包</th><th>大小</th><th>放入时间</th><th style="width:120px">操作</th></tr></thead>
            <tbody>
              <tr v-for="p in localPacks" :key="p.name">
                <td>{{ p.name }}</td>
                <td>{{ fmtSize(p.size_bytes) }}</td>
                <td>{{ p.created_at }}</td>
                <td>
                  <button
                    class="btn-mini"
                    :disabled="installingPack === p.name || waitingRestart"
                    @click="installPack(p.name)"
                  >{{ installingPack === p.name ? '安装中…' : '安装' }}</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="op-muted">backups/ 目录暂无升级包。放入文件后点「刷新列表」。</div>
      </div>
    </section>

    <!-- 操作审计 -->
    <section class="setting-section">
      <h2 class="section-title"><span class="section-icon">&#128220;</span> 操作审计</h2>
      <div class="setting-card">
        <div class="setting-row" style="margin-bottom:8px">
          <div class="op-muted">谁、何时、执行了什么运维操作（最近 50 条）</div>
          <button class="btn-mini danger" :disabled="auditClearing || !auditRows.length" @click="clearAudit">
            {{ auditClearing ? '清理中…' : '一键清空' }}
          </button>
        </div>
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

/* git 一键升级 */
.git-controls {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  flex-wrap: wrap;
  margin-top: var(--space-8);
  padding-top: var(--space-8);
  border-top: 1px dashed var(--color-border);
}
.git-token-ok {
  margin-left: var(--space-8);
  font-size: var(--text-xs);
  font-weight: normal;
  color: var(--color-success);
}
.warn-text { color: var(--color-warning); }

/* 升级包分发 */
.pack-export-actions { display: flex; gap: var(--space-8); align-items: center; }
.pack-download-btn {
  display: inline-block;
  text-decoration: none;
  text-align: center;
  line-height: 1.5;
}
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
.diag-table-wrap { margin-top: var(--space-8); overflow-x: auto; }
.git-log {
  margin-top: var(--space-12);
  padding: var(--space-12);
  background: var(--color-bg);
  border-radius: var(--radius-md);
}
.git-log-title { font-size: var(--text-xs); color: var(--color-text-tertiary); margin-bottom: var(--space-6); }
.git-log pre {
  margin: 0;
  font-size: var(--text-xs);
  font-family: monospace;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow-y: auto;
}

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
