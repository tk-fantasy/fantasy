<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'

const props = defineProps({
  modelValue: { type: String, default: '' },
  options: { type: Array, default: () => [] },
  placeholder: { type: String, default: '-- 未选择 --' },
  disabled: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'change'])

const open = ref(false)
const triggerRef = ref(null)
const dropdownRef = ref(null)

const selectedLabel = computed(() => {
  const found = props.options.find(o => o.value === props.modelValue)
  return found ? found.label : props.placeholder
})

function toggle() {
  if (props.disabled) return
  open.value = !open.value
}

function select(opt) {
  emit('update:modelValue', opt.value)
  emit('change', opt.value)
  open.value = false
}

function onClickOutside(e) {
  if (!triggerRef.value?.contains(e.target) && !dropdownRef.value?.contains(e.target)) {
    open.value = false
  }
}

onMounted(() => {
  document.addEventListener('mousedown', onClickOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onClickOutside)
})
</script>

<template>
  <div class="flow-select" :class="{ open, disabled }">
    <div ref="triggerRef" class="trigger" @click="toggle">
      <span class="trigger-text">{{ selectedLabel }}</span>
      <svg class="chevron" :class="{ rotated: open }" width="10" height="6" viewBox="0 0 10 6">
        <path d="M0 0l5 6 5-6z" fill="currentColor"/>
      </svg>
    </div>

    <Transition name="dropdown">
      <div v-if="open" ref="dropdownRef" class="dropdown">
        <div class="dropdown-glow"></div>
        <div class="options">
          <div
            v-for="opt in options"
            :key="opt.value"
            class="option"
            :class="{ active: opt.value === modelValue }"
            @click="select(opt)"
          >
            {{ opt.label }}
          </div>
          <div v-if="options.length === 0" class="option empty">无可用选项</div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.flow-select {
  position: relative;
  width: 240px;
}

.trigger {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  font-size: var(--text-base);
  font-family: inherit;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  cursor: pointer;
  transition: border-color var(--duration-normal) var(--ease-out);
  -webkit-user-select: none;
  user-select: none;
}

.trigger:hover {
  border-color: var(--color-border-active);
}

.flow-select.open .trigger {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.1);
}

.flow-select.disabled .trigger {
  opacity: 0.35;
  cursor: not-allowed;
}

.trigger-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  text-align: right;
}

.chevron {
  color: var(--color-text-tertiary);
  flex-shrink: 0;
  transition: transform var(--duration-fast) var(--ease-out);
}

.chevron.rotated {
  transform: rotate(180deg);
}

/* Dropdown */
.dropdown {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  right: 0;
  z-index: 100;
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--color-border-hover);
  background: #1a1a22;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.5), 0 4px 12px rgba(0, 0, 0, 0.3);
}

/* 流光层 */
.dropdown-glow {
  position: absolute;
  inset: 0;
  pointer-events: none;
  z-index: 0;
  background:
    radial-gradient(ellipse 60% 50% at 25% 30%, rgba(45, 90, 78, 0.3) 0%, transparent 50%),
    radial-gradient(ellipse 50% 45% at 70% 25%, rgba(74, 124, 112, 0.25) 0%, transparent 45%),
    radial-gradient(ellipse 55% 50% at 45% 70%, rgba(30, 60, 110, 0.25) 0%, transparent 45%),
    radial-gradient(ellipse 50% 45% at 60% 50%, rgba(232, 168, 124, 0.3) 0%, transparent 45%),
    radial-gradient(ellipse 45% 40% at 35% 55%, rgba(183, 128, 176, 0.25) 0%, transparent 40%);
  background-size: 300% 300%, 300% 300%, 300% 300%, 300% 300%, 300% 300%;
  animation: dropdownFlow 12s cubic-bezier(0.45, 0, 0.55, 1) infinite;
  opacity: 0.6;
}

@keyframes dropdownFlow {
  0% {
    background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%;
  }
  25% {
    background-position: 40% 35%, 55% 50%, 60% 45%, 30% 60%, 50% 35%;
  }
  50% {
    background-position: 25% 15%, 65% 55%, 45% 65%, 55% 30%, 20% 50%;
  }
  75% {
    background-position: 50% 30%, 45% 20%, 30% 55%, 65% 45%, 40% 55%;
  }
  100% {
    background-position: 10% 20%, 80% 25%, 35% 80%, 50% 40%, 30% 60%;
  }
}

.options {
  position: relative;
  z-index: 1;
  max-height: 260px;
  overflow-y: auto;
  padding: 4px;
}

.option {
  padding: 10px 14px;
  font-size: var(--text-base);
  color: var(--color-text);
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-out);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.option:hover {
  background: rgba(255, 255, 255, 0.08);
}

.option.active {
  background: rgba(74, 124, 112, 0.2);
  color: #fff;
  font-weight: var(--weight-medium);
}

.option.empty {
  color: var(--color-text-muted);
  cursor: default;
  text-align: center;
}

.option.empty:hover {
  background: transparent;
}

/* Transition */
.dropdown-enter-active,
.dropdown-leave-active {
  transition: opacity var(--duration-fast) var(--ease-out), transform var(--duration-fast) var(--ease-out);
}

.dropdown-enter-from,
.dropdown-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}

/* Light mode */
.light-mode .dropdown {
  background: #ffffff;
  border-color: rgba(0, 0, 0, 0.1);
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12), 0 4px 12px rgba(0, 0, 0, 0.08);
}

.light-mode .dropdown-glow {
  opacity: 0.35;
}

.light-mode .option:hover {
  background: rgba(0, 0, 0, 0.04);
}

.light-mode .option.active {
  background: rgba(74, 124, 112, 0.12);
  color: var(--color-primary-dark);
}

/* Scrollbar */
.options::-webkit-scrollbar {
  width: 4px;
}

.options::-webkit-scrollbar-track {
  background: transparent;
}

.options::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.1);
  border-radius: 2px;
}

.light-mode .options::-webkit-scrollbar-thumb {
  background: rgba(0, 0, 0, 0.1);
}

@media (max-width: 768px) {
  .flow-select {
    width: 100%;
  }
}
</style>
