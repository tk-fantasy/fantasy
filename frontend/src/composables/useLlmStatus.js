/**
 * LLM 模型状态 composable（从 ChatView 拆出）。
 *
 * 状态条显示 chat 模型名（静态读 settings），悬停时懒加载测试连通性。
 * - chatModelName：从 /api/llm/settings 静态读，不耗 API（进页面就显示）
 * - llmStatus：悬停时才调 /api/llm/status 真实测试（含 4 个角色连通结果）
 */
import { ref } from 'vue'
import { apiGet } from '../utils/api'

// 状态条展示的 4 个角色标签（stt 语音专用，不在此列）
export const ROLE_LABELS = { chat: '对话', summary: '摘要', vision: '视觉', embed: '向量' }

export function useLlmStatus() {
  const chatModelName = ref('')
  const llmStatus = ref(null)        // null=未测 | {roles:{chat:{...},...}}
  const llmStatusLoading = ref(false)
  const showLlmPopover = ref(false)
  let llmStatusLoaded = false        // 悬停过一次后缓存，避免重复测试

  /** 悬停状态条：首次触发连通性测试（懒加载） */
  async function onStatusHover() {
    if (llmStatusLoaded || llmStatusLoading.value) return
    llmStatusLoading.value = true
    try {
      const data = await apiGet('/api/llm/status')
      llmStatus.value = data
      llmStatusLoaded = true
    } catch (e) {
      console.error('Failed to load llm status:', e)
    } finally {
      llmStatusLoading.value = false
    }
  }

  /** 静态读取 chat 模型名（onMounted 时调用，不耗测试 API） */
  async function loadChatModelName() {
    try {
      const settingsData = await apiGet('/api/llm/settings')
      const chatProvider = settingsData?.current?.chat
      if (chatProvider?.key_id) {
        const keysData = await apiGet('/api/llm_keys')
        const matched = (keysData || []).find(k => k.id === chatProvider.key_id)
        chatModelName.value = matched?.model || ''
      } else if (!chatProvider?.use_global) {
        chatModelName.value = ''
      }
    } catch (e) {
      console.error('Failed to load chat model name:', e)
    }
  }

  return {
    chatModelName,
    llmStatus,
    llmStatusLoading,
    showLlmPopover,
    onStatusHover,
    loadChatModelName,
  }
}
