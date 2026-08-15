/**
 * 数据出网模式 composable（09 清单条目 4）。
 *
 * 从 /api/egress 读当前模式（cloud/hybrid/local），供聊天页徽标、
 * 引导页声明步骤与高级设置弹窗共享。进页面加载一次，切换后由调用方 reload。
 */
import { ref } from 'vue'
import { apiGet } from '../utils/api'

/** 三档模式静态描述（引导页/高级设置共用，与后端 egress_service.MODES 对应） */
export const EGRESS_MODES = [
  {
    key: 'cloud',
    icon: '☁️',
    label: '云端对话',
    desc: '对话文本经加密 HTTPS 发往模型厂商；摄像头画面仅在使用云端视觉模型时发送；设备控制指令不出局域网。',
  },
  {
    key: 'hybrid',
    icon: '🔀',
    label: '混合模式',
    desc: '对话走云端，视觉/语音转写等敏感角色建议指向内网模型端点，兼顾效果与隐私。',
  },
  {
    key: 'local',
    icon: '🏠',
    label: '纯内网',
    desc: '模型端点全部在内网（如 Mac 上的 Ollama / LM Studio、自建 vLLM，以 OpenAI 兼容端点接入），无任何数据出网，断网可用。',
  },
]

export const EGRESS_MODE_LABELS = Object.fromEntries(EGRESS_MODES.map(m => [m.key, m.label]))

export function useEgressMode() {
  const egressMode = ref('cloud')
  const egressLabel = ref(EGRESS_MODE_LABELS.cloud)
  const egressWarnings = ref([])
  const egressConfirmed = ref(false)
  const egressLoading = ref(false)

  async function loadEgressMode() {
    egressLoading.value = true
    try {
      const data = await apiGet('/api/egress')
      egressMode.value = data?.mode || 'cloud'
      egressLabel.value = data?.mode_label || EGRESS_MODE_LABELS[egressMode.value]
      egressWarnings.value = [...(data?.warnings || []), ...(data?.notes || [])]
      egressConfirmed.value = !!data?.confirmed
    } catch (e) {
      console.error('Failed to load egress mode:', e)
    } finally {
      egressLoading.value = false
    }
  }

  return { egressMode, egressLabel, egressWarnings, egressConfirmed, egressLoading, loadEgressMode }
}
