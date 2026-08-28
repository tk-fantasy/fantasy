<script setup>
/**
 * 摄像头预览弹窗的多路切换器(Task 12,D4 AI 预览单例)。
 * 恒用 FlowSelect 下拉:标签按钮在窄屏/多路时会换行,把弹窗撑得忽高忽低;
 * 下拉高度恒定,弹窗尺寸稳定。
 */
import { computed } from 'vue'
import FlowSelect from './FlowSelect.vue'

const props = defineProps({
  cameras: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
})
const emit = defineEmits(['change'])

const options = computed(() =>
  props.cameras.map(c => ({ value: c.id, label: c.name || c.id })))

function onSelect(id) {
  if (id !== props.modelValue) emit('change', id)
}
</script>

<template>
  <div v-if="cameras.length > 1" class="camera-switcher">
    <FlowSelect
      :modelValue="modelValue"
      :options="options"
      @change="onSelect"
    />
  </div>
</template>

<style scoped>
.camera-switcher {
  display: flex;
  padding: var(--space-6) var(--space-16) 0;
}
</style>
