/**
 * PTZ 云台控制 composable（从 ChatView 拆出）。
 *
 * 点一下方向键 → POST /ptz/step：后端 ContinuousMove 一小段后自动 Stop，
 * 实现"按一下动一下"。停转由后端保证（即使关页面也会停），不依赖 pointerup。
 *
 * 依赖外部传入的 activeCameraId（当前弹窗摄像头）和 cameras（摄像头列表），
 * 以决定走 per-camera PTZ 端点还是旧的全局 /api/ptz/step 兼容端点。
 */
import { ref, onScopeDispose } from 'vue'

export function usePtz(activeCameraId, cameras) {
  const ptzEnabled = ref(false)
  const ptzMoving = ref(false)   // 步进冷却中，忽略连点
  const ptzStepMs = ref(300)     // 单步时长(ms)，与后端 ptz.step_ms 一致
  let ptzCooldownTimer = null

  /** 读 PTZ 配置：有 activeCameraId 读 per-camera，否则走旧全局 /api/ptz/status */
  async function fetchPtzStatus() {
    try {
      const cid = activeCameraId.value
      if (cid) {
        const cam = cameras.value.find(c => c.id === cid)
        if (cam) {
          ptzEnabled.value = !!cam.ptz_enabled
          ptzStepMs.value = cam.ptz_step_ms || 300
          return
        }
      }
      const res = await fetch('/api/ptz/status')
      const json = await res.json()
      const data = json.data || json
      ptzEnabled.value = !!data.enabled
      if (data.step_ms) ptzStepMs.value = Number(data.step_ms) || 300
    } catch (e) {
      console.error('Failed to fetch PTZ status:', e)
    }
  }

  /** 单击步进：发一次 move，后端到点自动 stop。冷却期内忽略新点击 */
  function ptzStep(direction) {
    if (!ptzEnabled.value || ptzMoving.value) return
    ptzMoving.value = true
    if (ptzCooldownTimer) { clearTimeout(ptzCooldownTimer); ptzCooldownTimer = null }
    const cid = activeCameraId.value
    const url = cid ? `/api/cameras/${cid}/ptz/step` : '/api/ptz/step'
    fetch(url, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ direction }),
    }).catch(e => console.error('PTZ step failed:', e))
    ptzCooldownTimer = setTimeout(() => {
      ptzCooldownTimer = null
      ptzMoving.value = false
    }, ptzStepMs.value)
  }

  // 组件卸载时清掉残留冷却计时器，避免 ptzMoving 卡 true / 计时器泄漏。
  // onScopeDispose 在所属组件作用域销毁时触发，与 onUnmounted 同时机。
  onScopeDispose(() => {
    if (ptzCooldownTimer) { clearTimeout(ptzCooldownTimer); ptzCooldownTimer = null }
  })

  return { ptzEnabled, ptzMoving, ptzStepMs, fetchPtzStatus, ptzStep }
}
