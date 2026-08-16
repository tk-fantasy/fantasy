/**
 * PTZ 云台控制 composable（从 ChatView 拆出）。
 *
 * 点一下方向键 → POST /api/cameras/{id}/ptz/step：后端 ContinuousMove
 * 一小段后自动 Stop，实现"按一下动一下"。停转由后端保证（即使关页面
 * 也会停），不依赖 pointerup。
 *
 * 配置（ptz_enabled/ptz_step_ms）读自 cameras 列表对应的行——每路摄像头
 * 独立配置，无 activeCameraId 时不启用。
 */
import { ref, onScopeDispose } from 'vue'

export function usePtz(activeCameraId, cameras) {
  const ptzEnabled = ref(false)
  const ptzMoving = ref(false)   // 步进冷却中，忽略连点
  const ptzStepMs = ref(300)     // 单步时长(ms)，读自该路摄像头的 ptz_step_ms
  let ptzCooldownTimer = null

  /** 读当前路摄像头的 PTZ 配置 */
  async function fetchPtzStatus() {
    try {
      const cid = activeCameraId.value
      const cam = cid && cameras.value.find(c => c.id === cid)
      if (cam) {
        ptzEnabled.value = !!cam.ptz_enabled
        ptzStepMs.value = cam.ptz_step_ms || 300
      } else {
        ptzEnabled.value = false
      }
    } catch (e) {
      console.error('Failed to fetch PTZ status:', e)
    }
  }

  /** 单击步进：发一次 move，后端到点自动 stop。冷却期内忽略新点击 */
  function ptzStep(direction) {
    if (!ptzEnabled.value || ptzMoving.value) return
    const cid = activeCameraId.value
    if (!cid) return
    ptzMoving.value = true
    if (ptzCooldownTimer) { clearTimeout(ptzCooldownTimer); ptzCooldownTimer = null }
    fetch(`/api/cameras/${cid}/ptz/step`, {
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
