<template>
  <button
    class="mode-option-btn"
    :class="{ active: isActive }"
    @click="selectMode"
    :title="contribution.props?.label"
  >
    <span v-if="contribution.props?.icon" class="mode-icon">{{ contribution.props.icon }}</span>
    <span class="mode-label">{{ contribution.props?.label }}</span>
  </button>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '../../utils/api'

const props = defineProps({ contribution: Object })
const isActive = ref(false)

async function refreshState() {
  if (!props.contribution.state_key) return
  try {
    const resp = await apiGet(`/api/integrations/state/${props.contribution.state_key}`)
    const current = resp?.value || 'aether'
    isActive.value = current === props.contribution.props?.mode
  } catch {
    isActive.value = false
  }
}

async function selectMode() {
  const mode = props.contribution.props?.mode
  if (!mode) return
  try {
    await apiPost(`/api/integrations/action/${props.contribution.action}`, { mode })
    isActive.value = true
    // 通知 ChatView 更新当前模式（插件贡献的按钮点击后同步）
    window.dispatchEvent(new CustomEvent('mode-changed', { detail: { mode } }))
  } catch {
    // 忽略，保持当前状态
  }
}

onMounted(refreshState)
</script>

<style scoped>
.mode-option-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-radius: 8px;
  background: transparent;
  color: rgba(255, 255, 255, 0.6);
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s;
}
.mode-option-btn:hover {
  background: rgba(255, 255, 255, 0.06);
  color: rgba(255, 255, 255, 0.9);
}
.mode-option-btn.active {
  background: rgba(88, 166, 255, 0.15);
  border-color: rgba(88, 166, 255, 0.4);
  color: #58a6ff;
}
.mode-icon {
  font-size: 14px;
}
</style>
