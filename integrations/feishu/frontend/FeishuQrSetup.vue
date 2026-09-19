<template>
  <div class="qr-setup">
    <!-- 折叠态：已接入徽标 + 重扫入口 -->
    <div v-if="!expanded" class="qr-done-row">
      <span class="qr-done-text">⚡ 飞书扫码一键接入</span>
      <span v-if="configured" class="qr-done-badge">已接入</span>
      <button class="action-btn" @click="expand">{{ configured ? '重新扫码' : '扫码接入' }}</button>
    </div>

    <template v-else>
      <div class="qr-title">⚡ 扫码一键接入</div>
      <p class="qr-hint">
        打开飞书 App → 扫一扫，确认后机器人自动创建并接入。
        私聊仅限扫码人本人；全员可用请用下方手动配置。
      </p>

      <div v-if="qrDataUrl" class="qr-img-wrap">
        <img :src="qrDataUrl" class="qr-img" alt="飞书扫码二维码" />
        <div v-if="userCode" class="qr-user-code">
          扫不了码？在飞书 App 输入：<b>{{ userCode }}</b>
        </div>
      </div>

      <div class="qr-status" :data-state="status">{{ statusText }}</div>
      <div v-if="remainingText" class="qr-countdown">{{ remainingText }}</div>

      <div class="qr-actions">
        <button
          v-if="['idle', 'denied', 'expired', 'error'].includes(status)"
          class="btn-primary"
          :disabled="starting"
          @click="startSetup"
        >{{ starting ? '获取二维码中…' : '获取二维码' }}</button>
        <button
          v-if="['ready', 'scanned', 'success'].includes(status)"
          class="action-btn"
          @click="cancelSetup"
        >{{ status === 'success' ? '关闭' : '取消' }}</button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { apiGet, apiPost } from '@/utils/api'

// 本文件位于 integrations/feishu/frontend/，由宿主 PluginSlot 的
// import.meta.glob 动态加载；apiGet/apiPost 已自动解包响应 data 字段。
// 二维码由后端渲染成 SVG data URL 返回（qr_svg_data_url），本组件零 npm 依赖。
const METHOD_URL = '/api/integrations/feishu/method'
const POLL_FALLBACK_SEC = 5

const expanded = ref(false)
const configured = ref(false)
const starting = ref(false)
const qrDataUrl = ref('')
const userCode = ref('')
const expiresAt = ref(0)
const remainingText = ref('')
const status = ref('idle') // idle|ready|scanned|success|denied|expired|error
const statusText = ref('')

const STATUS_TEXT = {
  idle: '',
  ready: '等待扫码…',
  scanned: '已扫码，请在手机上确认',
  success: '✓ 已接入',
  denied: '已取消（手机端拒绝）',
  expired: '二维码已过期',
  error: '飞书扫码通道异常，请改用手动配置',
}

let pollTimer = null
let countdownTimer = null

async function checkConfigured() {
  try {
    const values = (await apiGet('/api/integrations/feishu/config'))?.values || {}
    configured.value = !!values.app_id || !!values.app_secret?.is_set
  } catch {
    configured.value = false
  }
}

function expand() {
  expanded.value = true
  if (!configured.value) startSetup()
}

async function startSetup() {
  starting.value = true
  status.value = 'idle'
  statusText.value = ''
  qrDataUrl.value = ''
  userCode.value = ''
  try {
    const data = await apiPost(`${METHOD_URL}/qr_start`, {})
    if (data?.success === false) throw new Error(data.message || '获取二维码失败')
    qrDataUrl.value = data.qr_svg_data_url
    userCode.value = data.user_code || ''
    expiresAt.value = Date.now() + (data.expires_in || 3600) * 1000
    status.value = 'ready'
    statusText.value = STATUS_TEXT.ready
    startPolling(data.interval || POLL_FALLBACK_SEC)
    startCountdown()
  } catch (e) {
    status.value = 'error'
    statusText.value = e?.message || '获取二维码失败，请改用手动配置'
  } finally {
    starting.value = false
  }
}

function startPolling(intervalSec) {
  stopPolling()
  pollTimer = setInterval(pollOnce, Math.max(3, intervalSec) * 1000)
  pollOnce()
}

async function pollOnce() {
  try {
    const data = await apiPost(`${METHOD_URL}/qr_poll`, {})
    if (data?.success === false) {
      // 后端明示故障（HTTP 200 + success:false，apiPost 不抛错）：停轮询报错
      stopPolling(); stopCountdown()
      qrDataUrl.value = ''
      status.value = 'error'
      statusText.value = data.message || STATUS_TEXT.error
      return
    }
    const st = data?.status
    if (st === 'success') {
      stopPolling(); stopCountdown()
      qrDataUrl.value = ''
      status.value = 'success'
      statusText.value = data.applied === 'restarted'
        ? '✓ 已接入，机器人已启动'
        : '✓ 凭证已保存，但插件未在运行（下次启动生效）'
      // 通知管理页刷新列表与存活徽标
      window.dispatchEvent(new CustomEvent('aether:plugins-changed'))
      setTimeout(() => { expanded.value = false; checkConfigured() }, 2500)
    } else if (st === 'scanned') {
      status.value = 'scanned'
      statusText.value = STATUS_TEXT.scanned
    } else if (st === 'denied' || st === 'expired' || st === 'error') {
      stopPolling(); stopCountdown()
      qrDataUrl.value = ''
      status.value = st
      statusText.value = data.message || STATUS_TEXT[st]
    }
    // pending：保持现状继续轮询
  } catch {
    /* 单次网络失败静默重试；连续失败由后端 error 状态回报 */
  }
}

function startCountdown() {
  stopCountdown()
  const tick = () => {
    const ms = expiresAt.value - Date.now()
    if (ms <= 0) { remainingText.value = ''; return }
    const total = Math.floor(ms / 1000)
    remainingText.value =
      `二维码剩余 ${String(Math.floor(total / 60)).padStart(2, '0')}:${String(total % 60).padStart(2, '0')}`
  }
  tick()
  countdownTimer = setInterval(tick, 1000)
}

async function cancelSetup() {
  stopPolling(); stopCountdown()
  try { await apiPost(`${METHOD_URL}/qr_cancel`, {}) } catch { /* 忽略 */ }
  expanded.value = false
  status.value = 'idle'
  statusText.value = ''
}

function stopPolling() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null }
}

function stopCountdown() {
  if (countdownTimer) { clearInterval(countdownTimer); countdownTimer = null }
  remainingText.value = ''
}

onMounted(checkConfigured)
onBeforeUnmount(() => { stopPolling(); stopCountdown() })
</script>

<style scoped>
.qr-setup {
  padding: 12px;
  margin-bottom: 14px;
  border: 1px dashed var(--color-border, #d8dce3);
  border-radius: 10px;
}
.qr-done-row { display: flex; align-items: center; gap: 10px; }
.qr-done-text { font-weight: 600; }
.qr-done-badge {
  font-size: 12px;
  color: #1a7f37;
  background: rgba(26, 127, 55, 0.1);
  border-radius: 999px;
  padding: 1px 8px;
}
.qr-title { font-weight: 600; margin-bottom: 4px; }
.qr-hint { font-size: 12px; color: var(--color-text-secondary, #888); margin: 0 0 10px; }
.qr-img-wrap { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.qr-img { width: 180px; height: 180px; border-radius: 8px; background: #fff; padding: 6px; }
.qr-user-code { font-size: 12px; color: var(--color-text-secondary, #888); }
.qr-status { margin-top: 10px; font-size: 13px; }
.qr-status[data-state='success'] { color: #1a7f37; }
.qr-status[data-state='error'], .qr-status[data-state='denied'] { color: #c0392b; }
.qr-countdown { font-size: 12px; color: var(--color-text-secondary, #888); margin-top: 2px; }
.qr-actions { display: flex; gap: 10px; margin-top: 10px; }
</style>
