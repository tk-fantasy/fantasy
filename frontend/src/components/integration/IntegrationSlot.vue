<template>
  <span class="integration-slot" v-if="contributions.length">
    <component
      v-for="c in contributions"
      :is="componentFor(c.type)"
      :key="c.plugin_id + '-' + c.slot"
      :contribution="c"
    />
  </span>
</template>

<script setup>
import { ref, onMounted, shallowRef } from 'vue'
import { apiGet } from '../../utils/api'
import ToggleButtonContribution from './ToggleButtonContribution.vue'

const props = defineProps({
  slot: { type: String, required: true },
})

const contributions = ref([])

// type → 通用组件映射（预定义类型，插件不能贡献任意组件）
const TYPE_COMPONENTS = {
  toggle_button: ToggleButtonContribution,
  // icon_button: IconButtonContribution,  // V1 只做 toggle_button，其余按需加
  // status_badge: StatusBadgeContribution,
}

function componentFor(type) {
  return TYPE_COMPONENTS[type] || null
}

onMounted(async () => {
  try {
    const all = await apiGet('/api/integrations/ui_contributions')
    // 过滤出贡献到本槽位的元素
    contributions.value = (all || []).filter(c => c.slot === props.slot)
  } catch (e) {
    // 集成平台未启用或请求失败 → 无贡献 → 无 UI（八竿子打不着）
    contributions.value = []
  }
})
</script>
