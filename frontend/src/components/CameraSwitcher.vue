<script setup>
/**
 * 摄像头预览弹窗的多路切换器(Task 12,D4 AI 预览单例)。
 * 从 ChatView 拆出:路数少用标签一行排开;路数多时标签换行会把预览弹窗
 * 越顶越高、短屏下直接挤出屏幕,退化为高度恒定的下拉选择(FlowSelect)。
 */
import { computed } from 'vue'
import FlowSelect from './FlowSelect.vue'

const props = defineProps({
  cameras: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
})
const emit = defineEmits(['change'])

// 弹窗内容宽约 590px、单个标签 70~120px:第 5 路起大概率换行,直接切下拉
const MAX_TABS = 4

const options = computed(() =>
  props.cameras.map(c => ({ value: c.id, label: c.name || c.id })))
const useDropdown = computed(() => props.cameras.length > MAX_TABS)

function onSelect(id) {
  if (id !== props.modelValue) emit('change', id)
}
</script>

<template>
  <div v-if="cameras.length > 1" class="camera-switcher">
    <FlowSelect
      v-if="useDropdown"
      :modelValue="modelValue"
      :options="options"
      @change="onSelect"
    />
    <template v-else>
      <button
        v-for="cam in cameras"
        :key="cam.id"
        class="camera-tab"
        :class="{ active: modelValue === cam.id }"
        @click="onSelect(cam.id)"
      >{{ cam.name || cam.id }}</button>
    </template>
  </div>
</template>

<style scoped>
.camera-switcher {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-6) var(--space-16) 0;
  flex-wrap: wrap;
}

.camera-tab {
  padding: var(--space-3) var(--space-12);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}

.camera-tab:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.camera-tab.active {
  background: var(--color-primary-light);
  border-color: var(--color-primary);
  color: var(--color-primary);
  font-weight: var(--weight-semibold);
}
</style>
