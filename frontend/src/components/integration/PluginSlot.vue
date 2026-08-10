<template>
  <div class="plugin-slot" v-if="activeComponents.length">
    <component
      v-for="entry in activeComponents"
      :key="entry.pluginId + '-' + slot"
      :is="entry.component"
    />
  </div>
</template>

<script setup>
import { ref, computed, onMounted, defineAsyncComponent } from 'vue'
import { apiGet } from '@/utils/api'

const props = defineProps({
  slot: { type: String, required: true },
})

// 构建时扫描所有插件前端组件（import.meta.glob）
// 路径：integrations/<plugin_id>/frontend/*.vue
// key 格式：../../../integrations/xiaoai/frontend/XiaoAiPanel.vue
const pluginComponents = import.meta.glob('../../../integrations/*/frontend/*.vue')

// 运行时从 API 知道哪些插件贡献了 custom_component 到本 slot
const contributions = ref([])

const activeComponents = computed(() => {
  // 找出贡献到本 slot 的 custom_component
  const matched = contributions.value.filter(
    c => c.slot === props.slot && c.type === 'custom_component'
  )
  const result = []
  for (const c of matched) {
    // glob key 格式：../../../integrations/<plugin_id>/frontend/<File>.vue
    // 找匹配该 plugin_id 的组件
    const globKey = Object.keys(pluginComponents).find(key =>
      key.includes(`/integrations/${c.plugin_id}/frontend/`)
    )
    if (globKey) {
      result.push({
        pluginId: c.plugin_id,
        component: defineAsyncComponent(pluginComponents[globKey]),
      })
    }
  }
  return result
})

onMounted(async () => {
  try {
    const all = await apiGet('/api/integrations/ui_contributions')
    contributions.value = all || []
  } catch {
    contributions.value = []
  }
})
</script>
