/**
 * 摄像头管理 composable(Task 11)。
 *
 * 封装 /api/cameras 全套 REST + /api/ha/areas + focuses + discovery。
 * 复用 utils/api 的 apiGet/apiPost/apiPut(命名导出);DELETE 走原生 fetch。
 */
import { ref } from 'vue'
import { apiGet, apiPost, apiPut } from '../utils/api'

export function useCamera() {
  const cameras = ref([])
  const areas = ref([])
  const loading = ref(false)

  async function loadCameras() {
    loading.value = true
    try {
      cameras.value = await apiGet('/api/cameras')
    } finally {
      loading.value = false
    }
  }

  async function loadAreas() {
    areas.value = await apiGet('/api/ha/areas')
  }

  async function createCamera(data) {
    const created = await apiPost('/api/cameras', data)
    await loadCameras()
    return created
  }

  async function updateCamera(id, fields) {
    const updated = await apiPut(`/api/cameras/${id}`, fields)
    await loadCameras()
    return updated
  }

  async function deleteCamera(id) {
    const res = await fetch(`/api/cameras/${id}`, { method: 'DELETE', credentials: 'include' })
    if (!res.ok) throw new Error(`删除失败:HTTP ${res.status}`)
    await loadCameras()
  }

  async function testStream(id, config) {
    // 后端 test-stream 收 body(临时配置,不落库)
    return await apiPost(`/api/cameras/${id}/test-stream`, config)
  }

  // AI 预览单例切换(D4)
  async function enableDisplay(id) {
    return await apiPost(`/api/cameras/${id}/display/enable`, {})
  }
  async function disableDisplay(id) {
    return await apiPost(`/api/cameras/${id}/display/disable`, {})
  }

  // 关注项(per-camera)
  async function loadFocuses(id) {
    return await apiGet(`/api/cameras/${id}/focuses`)
  }
  async function addFocus(id, text) {
    return await apiPost(`/api/cameras/${id}/focuses`, { text })
  }
  async function updateFocus(id, focusId, fields) {
    return await apiPut(`/api/cameras/${id}/focuses/${focusId}`, fields)
  }
  async function deleteFocus(id, focusId) {
    const res = await fetch(`/api/cameras/${id}/focuses/${focusId}`, {
      method: 'DELETE', credentials: 'include',
    })
    if (!res.ok) throw new Error(`删除关注项失败:HTTP ${res.status}`)
  }

  // ONVIF 发现
  async function findDevice(id) {
    return await apiPost(`/api/cameras/${id}/discovery/find`, {})
  }
  async function manualIp(id, ip) {
    return await apiPost(`/api/cameras/${id}/discovery/manual-ip`, { ip })
  }

  // 自动化规则(per-camera):后端 /api/rules 返回全部,前端按 camera_id 过滤
  async function loadRules(cameraId) {
    const all = await apiGet('/api/rules')
    return (all || []).filter(r => (r.camera_id || '') === cameraId)
  }
  async function createRule(cameraId, text) {
    const res = await fetch('/api/task/rule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, camera_id: cameraId }),
    })
    const json = await res.json()
    if (!res.ok) throw new Error(json.message || `创建失败:HTTP ${res.status}`)
    return json.data
  }
  async function toggleRule(id, enabled) {
    await fetch(`/api/rules/${id}/enabled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    })
  }
  async function deleteRule(id) {
    await fetch(`/api/rules/${id}`, { method: 'DELETE' })
  }

  return {
    cameras, areas, loading,
    loadCameras, loadAreas, createCamera, updateCamera, deleteCamera,
    testStream, enableDisplay, disableDisplay,
    loadFocuses, addFocus, updateFocus, deleteFocus, findDevice, manualIp,
    loadRules, createRule, toggleRule, deleteRule,
  }
}
