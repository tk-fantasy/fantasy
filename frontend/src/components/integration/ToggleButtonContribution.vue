<template>
  <button
    class="mic-btn contribution-toggle"
    :class="{ off: !state }"
    :title="state ? propsData.title_on : propsData.title_off"
    @click="toggle"
    :disabled="loading"
  >
    {{ state ? (propsData.icon_on || '🔘') : (propsData.icon_off || '⭕') }}
  </button>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiGet, apiPost } from '../../utils/api'

const props = defineProps({
  contribution: { type: Object, required: true },
})

const state = ref(true)   // 默认开，onMounted 从后端读
const loading = ref(false)
const propsData = props.contribution.props || {}

async function refresh() {
  if (!props.contribution.state_key) return
  try {
    const data = await apiGet(`/api/integrations/state/${props.contribution.state_key}`)
    state.value = !!data?.value
  } catch (e) {
    // 状态读取失败保持默认（不阻塞 UI）
    console.warn('read state failed:', e)
  }
}

async function toggle() {
  if (loading.value) return
  loading.value = true
  const prev = state.value
  try {
    const data = await apiPost(`/api/integrations/action/${props.contribution.action}`, {})
    state.value = !!data?.broadcast_enabled ?? !prev
  } catch (e) {
    // 触发失败回滚本地态
    state.value = prev
    console.warn('toggle action failed:', e)
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
.contribution-toggle.off {
  opacity: 0.4;
  filter: grayscale(0.8);
}
</style>
