<template>
  <div
    v-if="hasContributions"
    class="integration-dropdown"
    :class="{ open }"
    ref="rootRef"
  >
    <!-- Trigger（仿 FlowSelect） -->
    <div class="trigger" @click="toggle">
      <span class="trigger-icon">{{ currentModeIcon }}</span>
      <span class="trigger-text">{{ currentModeLabel }}</span>
      <svg class="chevron" :class="{ rotated: open }" width="10" height="6" viewBox="0 0 10 6">
        <path d="M0 0l5 6 5-6z" fill="currentColor" />
      </svg>
    </div>

    <!-- Dropdown Panel -->
    <Transition name="dropdown">
      <div v-if="open" class="dropdown">
        <div class="aurora-layer"></div>
        <div class="panel">
          <!-- 广播开关行 -->
          <div
            v-for="c in toolbarContributions"
            :key="c.plugin_id + '-toggle'"
            class="panel-row toggle-row"
            @click="toggleBroadcast(c)"
          >
            <span class="row-icon">{{ broadcastOn ? c.props?.icon_on : c.props?.icon_off }}</span>
            <span class="row-label">{{ broadcastLabel(c) }}</span>
            <span class="toggle-pill" :class="{ on: broadcastOn }">
              <span class="toggle-dot"></span>
            </span>
          </div>

          <!-- 分隔线（有广播开关 + 有模式选项时才显示） -->
          <div
            v-if="toolbarContributions.length > 0 && modeOptions.length > 0"
            class="panel-divider"
          ></div>

          <!-- 模式选择行 -->
          <!-- 框架默认 Aether -->
          <div
            class="panel-row mode-row"
            :class="{ active: currentMode === 'aether' }"
            @click="selectMode('aether', 'Aether')"
          >
            <span class="radio" :class="{ checked: currentMode === 'aether' }"></span>
            <span class="row-label">Aether</span>
          </div>
          <!-- 插件贡献的模式 -->
          <div
            v-for="c in modeOptions"
            :key="c.plugin_id + '-mode'"
            class="panel-row mode-row"
            :class="{ active: currentMode === c.props?.mode }"
            @click="selectMode(c.props?.mode, c.props?.label)"
          >
            <span class="radio" :class="{ checked: currentMode === c.props?.mode }"></span>
            <span v-if="c.props?.icon" class="row-icon">{{ c.props.icon }}</span>
            <span class="row-label">{{ c.props?.label }}</span>
          </div>
        </div>
      </div>
    </Transition>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { apiGet, apiPost } from '../../utils/api'

const open = ref(false)
const rootRef = ref(null)
const contributions = ref([])
const currentMode = ref('aether')
const broadcastOn = ref(true)

// 按槽位分类
const toolbarContributions = computed(() =>
  contributions.value.filter(c => c.slot === 'chat_input_toolbar')
)
const modeOptions = computed(() =>
  contributions.value.filter(c => c.slot === 'chat_mode_selector')
)
const hasContributions = computed(() => contributions.value.length > 0)

// trigger 显示当前模式
const currentModeLabel = computed(() => {
  if (currentMode.value === 'aether') return 'Aether'
  const found = modeOptions.value.find(c => c.props?.mode === currentMode.value)
  return found?.props?.label || 'Aether'
})
const currentModeIcon = computed(() => {
  if (currentMode.value === 'aether') return '✦'
  const found = modeOptions.value.find(c => c.props?.mode === currentMode.value)
  return found?.props?.icon || '✦'
})

// 广播开关文案
function broadcastLabel(c) {
  if (broadcastOn.value) return c.props?.title_on || '广播已开启'
  return c.props?.title_off || '广播已关闭'
}

function toggle() {
  open.value = !open.value
}

async function toggleBroadcast(c) {
  try {
    await apiPost(`/api/integrations/action/${c.action}`, {})
    broadcastOn.value = !broadcastOn.value
    await refreshBroadcastState()
  } catch (e) {
    console.warn('toggle broadcast failed:', e)
  }
}

async function selectMode(mode, label) {
  try {
    await apiPost('/api/integrations/action/set_mode', { mode })
    currentMode.value = mode
    open.value = false
    window.dispatchEvent(new CustomEvent('mode-changed', { detail: { mode } }))
  } catch (e) {
    console.warn('select mode failed:', e)
  }
}

async function refreshState() {
  try {
    const [modeResp, broadcastResp] = await Promise.all([
      apiGet('/api/integrations/state/current_mode'),
      apiGet('/api/integrations/state/broadcast_enabled'),
    ])
    if (modeResp?.value) currentMode.value = modeResp.value
    if (broadcastResp?.value !== undefined) broadcastOn.value = !!broadcastResp.value
  } catch {
    // 集成平台未启用，保持默认
  }
}

async function refreshBroadcastState() {
  try {
    const resp = await apiGet('/api/integrations/state/broadcast_enabled')
    if (resp?.value !== undefined) broadcastOn.value = !!resp.value
  } catch {}
}

async function loadContributions() {
  try {
    const all = await apiGet('/api/integrations/ui_contributions')
    contributions.value = (all || []).filter(
      c => c.slot === 'chat_input_toolbar' || c.slot === 'chat_mode_selector'
    )
  } catch {
    contributions.value = []
  }
}

function onClickOutside(e) {
  if (rootRef.value && !rootRef.value.contains(e.target)) {
    open.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadContributions(), refreshState()])
  document.addEventListener('mousedown', onClickOutside)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onClickOutside)
})
</script>

<style scoped>
.integration-dropdown {
  position: relative;
}

/* Trigger（仿 FlowSelect） */
.trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
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

.integration-dropdown.open .trigger {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.1);
}

.trigger-icon {
  font-size: 14px;
  flex-shrink: 0;
}

.trigger-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chevron {
  color: var(--color-text-tertiary);
  flex-shrink: 0;
  transition: transform var(--duration-fast) var(--ease-out);
}

.chevron.rotated {
  transform: rotate(180deg);
}

/* Dropdown（仿 FlowSelect） */
.dropdown {
  position: absolute;
  top: calc(100% + 6px);
  left: 0;
  min-width: 200px;
  z-index: 100;
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--color-border-hover);
  background: var(--dialog-bg);
  box-shadow: var(--shadow-xl);
}

.panel {
  position: relative;
  z-index: 1;
  padding: 4px;
}

/* 行 */
.panel-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease-out);
}

.panel-row:hover {
  background: rgba(255, 255, 255, 0.08);
}

.row-icon {
  font-size: 16px;
  flex-shrink: 0;
}

.row-label {
  flex: 1;
  font-size: var(--text-sm);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 分隔线 */
.panel-divider {
  height: 1px;
  background: var(--color-border-hover);
  margin: 4px 8px;
}

/* toggle 开关药丸 */
.toggle-pill {
  width: 36px;
  height: 20px;
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.12);
  display: flex;
  align-items: center;
  padding: 2px;
  transition: background var(--duration-fast) var(--ease-out);
  flex-shrink: 0;
}

.toggle-pill.on {
  background: rgba(74, 124, 112, 0.6);
}

.toggle-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: #fff;
  transition: transform var(--duration-fast) var(--ease-out);
}

.toggle-pill.on .toggle-dot {
  transform: translateX(16px);
}

/* radio 单选圆点 */
.radio {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 2px solid var(--color-text-tertiary);
  flex-shrink: 0;
  transition: all var(--duration-fast) var(--ease-out);
}

.radio.checked {
  border-color: var(--color-primary);
  background: var(--color-primary);
  box-shadow: inset 0 0 0 3px var(--dialog-bg);
}

.mode-row.active {
  background: rgba(74, 124, 112, 0.12);
}

.mode-row.active .row-label {
  font-weight: var(--weight-medium);
  color: #fff;
}

/* Transition（同 FlowSelect） */
.dropdown-enter-active,
.dropdown-leave-active {
  transition: opacity var(--duration-fast) var(--ease-out),
    transform var(--duration-fast) var(--ease-out);
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

.light-mode .panel-row:hover {
  background: rgba(0, 0, 0, 0.04);
}

.light-mode .mode-row.active {
  background: rgba(74, 124, 112, 0.08);
}

.light-mode .mode-row.active .row-label {
  color: var(--color-primary-dark);
}
</style>
