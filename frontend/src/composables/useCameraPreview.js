/**
 * 摄像头预览模态框 composable（从 ChatView 拆出）。
 *
 * 封装"实时预览弹窗"的全部状态机：
 *   1. feed 重连状态机（video_feed URL 强制刷新 + 指数退避）
 *   2. 设备在线状态（camera_opened）与流断状态的协调
 *   3. 多路切换（D4 AI 预览单例：旧路 disable / 新路 enable）
 *   4. 状态轮询（2s 一次 /api/cameras/{id}/state）
 *
 * 与 useCamera 的区别：useCamera 是 REST CRUD（增删改查摄像头配置），
 * 本 composable 是"实时预览"运行时——两者职责正交。
 *
 * 依赖外部传入：
 *   - activeCameraId / cameras：多路选择状态（与 usePtz 共享）
 *   - loadCameras：useCamera 的拉列表方法
 *
 * 注：切路后需同步 PTZ 配置是编排关注点，不在此 composable 内——
 * 由调用方在 openCamera/switchCamera 之后自行调 fetchPtzStatus。
 */
import { ref } from 'vue'
import { onScopeDispose } from 'vue'
import { apiPost } from '../utils/api'

const FEED_MAX_RETRIES = 10  // 设备掉线时最多重连 10 次（指数退避后约 5 分钟）

export function useCameraPreview(activeCameraId, cameras, loadCameras) {
  // 计算 video_feed URL（认证通过 cookie 自动处理）
  // Task 12:多路切换 — video_feed 走 /api/cameras/{activeCameraId}/video_feed
  const videoFeedUrl = ref('')
  const videoFeedKey = ref(0)  // 用于强制刷新 img src
  // 流断连状态：'live' 正常 | 'reconnecting' 重连中 | 'disconnected' 放弃
  // 状态来源：'device'（设备掉线，由 /api/state 的 camera_opened 驱动）
  //           'network'（HTTP 流断，由 <img> @error 驱动）
  const feedStatus = ref('live')
  const feedStatusSource = ref('network')
  const feedRetryCount = ref(0)   // 模板读取"第 N 次"，内部函数 ++ / 重置
  let feedRetryTimer = null
  let prevCameraOpened = null  // 上一拍设备状态，用于检测 false→true 翻转

  const showCamera = ref(false)
  const cameraState = ref(null)
  let cameraPollTimer = null

  function refreshVideoFeed() {
    feedRetryCount.value = 0
    feedStatus.value = 'live'
    feedStatusSource.value = 'network'
    videoFeedKey.value++
    const cid = activeCameraId.value
    videoFeedUrl.value = cid
      ? `/api/cameras/${cid}/video_feed?_t=${videoFeedKey.value}`
      : `/api/video_feed?_t=${videoFeedKey.value}`
  }

  function onVideoFeedError() {
    // 防御：模态框已关闭则不再重连
    if (!showCamera.value) return
    if (feedRetryTimer) clearTimeout(feedRetryTimer)

    // 设备仍在线（camera_opened===true）→ 纯流/网络抖动，永不放弃，持续自愈。
    // 仅当设备也掉了（camera_opened===false）且重试耗尽才进入终态。
    if (feedRetryCount.value >= FEED_MAX_RETRIES && cameraState.value?.camera_opened === false) {
      feedStatus.value = 'disconnected'
      return
    }
    feedRetryCount.value++
    feedStatus.value = 'reconnecting'
    feedStatusSource.value = 'network'
    // 指数退避：1s, 2s, 4s, ... 封顶 30s，避免拔掉后狂刷 src
    const delay = Math.min(1000 * (2 ** (feedRetryCount.value - 1)), 30000)
    feedRetryTimer = setTimeout(() => {
      if (!showCamera.value) return
      videoFeedKey.value++
      const cid = activeCameraId.value
      videoFeedUrl.value = cid
        ? `/api/cameras/${cid}/video_feed?_t=${videoFeedKey.value}`
        : `/api/video_feed?_t=${videoFeedKey.value}`
    }, delay)
  }

  // 帧到达说明流恢复了，重置重连计数
  function onVideoFeedLoad() {
    if (feedStatus.value !== 'live') {
      feedStatus.value = 'live'
      feedRetryCount.value = 0
    }
  }

  // Task 12:多路切换 — 弹窗打开时拉摄像头列表,默认选第一路 enabled 的
  async function openCamera() {
    showCamera.value = true
    prevCameraOpened = null
    await loadCameras()
    // 默认选第一路;后端 initialize 已把 display 单例给第一个 display_enabled,
    // 这里前端弹窗直接拉那路的 video_feed(无需再 enable,后端已激活)。
    const first = cameras.value.find(c => c.enabled) || cameras.value[0]
    if (first) {
      activeCameraId.value = first.id
    }
    refreshVideoFeed()
    startCameraPolling()
  }

  async function closeCamera() {
    showCamera.value = false
    stopCameraPolling()
    if (feedRetryTimer) {
      clearTimeout(feedRetryTimer)
      feedRetryTimer = null
    }
    // 不清空 activeCameraId:后端 _active_display_id 保留,vision_chat 工具继续用当前摄像头。
    // 下次打开弹窗恢复到上次选的路。
  }

  // Task 12:切路 — 旧路 disable 预览,新路 enable + 换 video_feed URL(D4 单例)
  async function switchCamera(id) {
    if (activeCameraId.value === id) return
    const oldId = activeCameraId.value
    activeCameraId.value = id
    // 旧路熄(D4:AI 预览单例,同一时刻只 1 路跑展示推理)
    if (oldId) {
      try { await apiPost(`/api/cameras/${oldId}/display/disable`, {}) } catch (e) { console.warn('disable old display:', e) }
    }
    // 新路亮
    try { await apiPost(`/api/cameras/${id}/display/enable`, {}) } catch (e) { console.warn('enable new display:', e) }
    prevCameraOpened = null
    refreshVideoFeed()
  }

  async function fetchCameraState() {
    try {
      const cid = activeCameraId.value
      // 有 activeCameraId 走 per-camera state;无则走主摄像头兼容端点
      const url = cid ? `/api/cameras/${cid}/state` : '/api/state'
      const res = await fetch(url)
      const json = await res.json()
      cameraState.value = json.data || json
      syncFeedStatusWithDevice()
    } catch (e) {
      console.error('Failed to fetch camera state:', e)
    }
  }

  // 把后端设备状态（camera_opened）接入 feedStatus 状态机。
  // 设备掉线（camera_opened===false）：显示"设备重连中"，但不动 <img> src——
  //   缓存的末帧继续沿原 keepalive 连接显示，比反复重建 src 闪烁更稳。
  // 设备恢复（camera_opened 由 false 翻回 true）：强制重建 src 让新帧立刻流入，
  //   替代用户手动刷新；同时清掉任何"流断/断开"状态。
  // 流断但设备在线（feedStatus==='disconnected' 且 camera_opened===true）：
  //   自动 refreshVideoFeed() 自愈，避免卡死在断开态需整页刷新。
  function syncFeedStatusWithDevice() {
    if (!showCamera.value) return
    const opened = cameraState.value?.camera_opened === true
    if (!opened) {
      // 设备掉了：进入"设备重连中"（若已是 disconnected 则保留终态，等设备回来）
      if (feedStatus.value !== 'disconnected' && feedStatus.value !== 'reconnecting') {
        feedStatus.value = 'reconnecting'
        feedStatusSource.value = 'device'
      } else if (feedStatus.value === 'reconnecting' && feedStatusSource.value === 'network') {
        // 网络重连未完时设备也掉了，统一归到"设备重连中"
        feedStatusSource.value = 'device'
      }
    } else if (prevCameraOpened === false) {
      // 设备刚恢复（false→true）：强制重建 src，新帧立刻流入
      refreshVideoFeed()
    } else if (feedStatus.value === 'disconnected') {
      // 设备一直在线，只是流断到了终态 → 自愈
      refreshVideoFeed()
    }
    prevCameraOpened = opened
  }

  function startCameraPolling() {
    fetchCameraState()
    cameraPollTimer = setInterval(fetchCameraState, 2000)
  }

  function stopCameraPolling() {
    if (cameraPollTimer) {
      clearInterval(cameraPollTimer)
      cameraPollTimer = null
    }
  }

  // 组件卸载时清掉残留计时器，避免泄漏 / 离开页面后仍轮询。
  // ChatView 的 onUnmounted 不再需要感知这些内部变量。
  onScopeDispose(() => {
    if (feedRetryTimer) clearTimeout(feedRetryTimer)
    stopCameraPolling()
  })

  return {
    videoFeedUrl,
    videoFeedKey,
    feedStatus,
    feedStatusSource,
    feedRetryCount,
    showCamera,
    cameraState,
    refreshVideoFeed,
    onVideoFeedError,
    onVideoFeedLoad,
    openCamera,
    closeCamera,
    switchCamera,
    fetchCameraState,
    startCameraPolling,
    stopCameraPolling,
  }
}
