<script setup>
defineProps({
  modelValue: Boolean,
})
const emit = defineEmits(['update:modelValue'])
</script>

<template>
  <button
    class="base-toggle"
    :class="{ on: modelValue }"
    role="switch"
    :aria-checked="modelValue"
    @click="$emit('update:modelValue', !modelValue)"
  >
    <span class="base-toggle-knob"></span>
  </button>
</template>

<style scoped>
.base-toggle {
  width: 40px;
  height: 22px;
  border-radius: var(--radius-full);
  background: rgba(255, 255, 255, 0.08);
  position: relative;
  cursor: pointer;
  transition: background var(--duration-normal) var(--ease-out);
  flex-shrink: 0;
  border: none;
  padding: 0;
  appearance: none;
}

.base-toggle.on {
  background: var(--color-primary);
  box-shadow: var(--shadow-glow);
}

.base-toggle-knob {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  transition: transform var(--duration-normal) var(--ease-spring);
  box-shadow: var(--shadow-sm);
}

.base-toggle.on .base-toggle-knob {
  transform: translateX(18px);
}
</style>
