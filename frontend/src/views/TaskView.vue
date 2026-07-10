<script setup>
import { ref, computed, onMounted } from 'vue'
import BaseToggle from '../components/BaseToggle.vue'
import EmojiPicker from '../components/EmojiPicker.vue'
import { apiGet } from '../utils/api'

const rules = ref([])
const loading = ref(true)
const showCreateForm = ref(false)
const newRuleText = ref('')
const creating = ref(false)
const selectedRule = ref(null)
const showRuleDetail = ref(false)

// Emoji 偏好管理
const emojiPrefs = ref({}) // { "task_condition:xxx": "🚶", "task_action:xxx": "💡" }
const showEmojiPicker = ref(false)
const currentEmojiTarget = ref(null) // { scope, key, defaultEmoji }

async function loadEmojiPrefs() {
  try {
    const res = await fetch('/api/emoji/preferences')
    const json = await res.json()
    const prefs = {}
    for (const item of (json.data || [])) {
      prefs[`${item.scope}:${item.key}`] = item.emoji_char
    }
    emojiPrefs.value = prefs
  } catch (e) {
    console.error('Failed to load emoji prefs:', e)
  }
}

function openEmojiPicker(scope, key, defaultEmoji) {
  currentEmojiTarget.value = { scope, key, defaultEmoji }
  showEmojiPicker.value = true
}

async function onEmojiSelect(item) {
  if (!currentEmojiTarget.value) return
  const { scope, key } = currentEmojiTarget.value
  const prefKey = `${scope}:${key}`
  
  try {
    await fetch('/api/emoji/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope, key, emoji_char: item.char }),
    })
    emojiPrefs.value[prefKey] = item.char
  } catch (e) {
    console.error('Failed to save emoji pref:', e)
  }
}

function openRuleDetail(rule) {
  selectedRule.value = rule
  showRuleDetail.value = true
}

function closeRuleDetail() {
  showRuleDetail.value = false
  selectedRule.value = null
}

async function loadRules() {
  try {
    loading.value = true
    const data = await apiGet('/api/rules')
    rules.value = (data || []).map((r) => ({
      ...r,
      enabled: r.enabled !== false,
    }))
  } catch (e) {
    console.error('Failed to load rules:', e)
  } finally {
    loading.value = false
  }
}

async function toggleRule(id) {
  const rule = rules.value.find((r) => r.id === id)
  if (!rule) return
  const newVal = !rule.enabled
  try {
    const res = await fetch(`/api/rules/${id}/enabled`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: newVal }),
    })
    if (!res.ok) {
      console.error('toggleRule failed:', res.status, await res.text())
      alert('操作失败，请重新登录')
      return
    }
    rule.enabled = newVal
  } catch (e) {
    console.error('Failed to toggle rule:', e)
  }
}

async function deleteRule(id) {
  if (!confirm('确定删除这条规则吗？')) return
  try {
    const res = await fetch(`/api/rules/${id}`, { method: 'DELETE' })
    if (!res.ok) {
      console.error('deleteRule failed:', res.status, await res.text())
      alert('删除失败，请重新登录')
      return
    }
    rules.value = rules.value.filter((r) => r.id !== id)
  } catch (e) {
    console.error('Failed to delete rule:', e)
  }
}

async function createRule() {
  const text = newRuleText.value.trim()
  if (!text) return

  creating.value = true
  try {
    const res = await fetch('/api/task/rule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }),
    })
    if (!res.ok) {
      const errText = await res.text()
      console.error('createRule failed:', res.status, errText)
      alert(res.status === 401 ? '登录已过期，请重新登录' : '创建失败：' + errText)
      creating.value = false
      return
    }
    const json = await res.json()
    if (json.data) {
      rules.value.unshift({
        ...json.data,
        enabled: json.data.enabled !== false,
      })
      newRuleText.value = ''
      showCreateForm.value = false
    }
  } catch (e) {
    console.error('Failed to create rule:', e)
  } finally {
    creating.value = false
  }
}

function formatCondition(condition) {
  if (typeof condition === 'string') return condition
  if (condition?.description) return condition.description
  if (condition?.type) return condition.type
  if (condition?.visual) return `视觉: ${condition.visual}`
  if (condition?.time) return `时间: ${condition.time}`
  if (condition?.weather) return `天气: ${condition.weather}`
  return JSON.stringify(condition)
}

function formatActions(actions) {
  if (!actions) return []
  if (typeof actions === 'string') {
    // Try to parse JSON string
    try {
      const parsed = JSON.parse(actions)
      return [formatSingleAction(parsed)]
    } catch {
      return [actions]
    }
  }
  if (Array.isArray(actions)) {
    return actions.map(a => {
      if (typeof a === 'string') {
        // Try to parse JSON string
        try {
          const parsed = JSON.parse(a)
          return formatSingleAction(parsed)
        } catch {
          return a
        }
      }
      return formatSingleAction(a)
    })
  }
  // Single object
  return [formatSingleAction(actions)]
}

function formatSingleAction(action) {
  if (!action) return ''
  if (typeof action === 'string') return action
  if (action?.description) return action.description
  
  // Handle MCP tool format: {"mcp_tool_name":"ha_devices___call_service","mcp_tool_input":{...}}
  if (action?.mcp_tool_name) {
    const toolInput = action.mcp_tool_input || {}
    const entity_id = toolInput.entity_id || ''
    const service = toolInput.service || ''
    
    // Extract device name from entity_id (e.g., "light.chuang_tou_deng" -> "床头灯")
    const deviceName = entity_id.split('.')[1] || entity_id
    const readableName = deviceName.replace(/_/g, ' ')
    
    // Map service to readable action
    const serviceMap = {
      'turn_on': '打开',
      'turn_off': '关闭',
      'open_cover': '打开',
      'close_cover': '关闭',
      'set_temperature': '设置温度',
      'set_brightness': '设置亮度',
    }
    const readableAction = serviceMap[service] || service
    
    return `${readableName} ${readableAction}`
  }
  
  // Handle direct format: {domain, service, entity_id}
  if (action?.service && action?.entity_id) {
    const deviceName = action.entity_id.split('.')[1] || action.entity_id
    const readableName = deviceName.replace(/_/g, ' ')
    const serviceMap = {
      'turn_on': '打开',
      'turn_off': '关闭',
      'open_cover': '打开',
      'close_cover': '关闭',
    }
    const readableAction = serviceMap[action.service] || action.service
    return `${readableName} ${readableAction}`
  }
  
  return JSON.stringify(action)
}

function getDefaultConditionIcon(condition) {
  if (typeof condition === 'string') {
    if (condition.includes('人')) return '🚶'
    if (condition.includes('温度') || condition.includes('热')) return '🌡️'
    if (condition.includes('日落') || condition.includes('晚上')) return '🌅'
    if (condition.includes('门') || condition.includes('窗')) return '🚪'
    if (condition.includes('宠物') || condition.includes('猫') || condition.includes('狗')) return '🐱'
    return '⚙️'
  }
  if (condition?.type) {
    const type = condition.type.toLowerCase()
    if (type.includes('person') || type.includes('human')) return '🚶'
    if (type.includes('temp')) return '🌡️'
    if (type.includes('time') || type.includes('sunset')) return '🌅'
    if (type.includes('door') || type.includes('window')) return '🚪'
    if (type.includes('pet')) return '🐱'
    if (type.includes('visual')) return '👁️'
    if (type.includes('weather')) return '🌤️'
  }
  return '⚙️'
}

function getConditionIcon(condition) {
  const conditionKey = typeof condition === 'string' ? condition : (condition?.description || condition?.type || JSON.stringify(condition))
  const prefKey = `task_condition:${conditionKey}`
  return emojiPrefs.value[prefKey] || getDefaultConditionIcon(condition)
}

function getDefaultActionIcon(action) {
  if (typeof action === 'string') {
    if (action.includes('灯') || action.includes('light')) return '💡'
    if (action.includes('空调') || action.includes('climate')) return '❄️'
    if (action.includes('窗帘') || action.includes('cover')) return '🪟'
    if (action.includes('门锁') || action.includes('lock')) return '🔒'
    if (action.includes('通知') || action.includes('notify')) return '📱'
    if (action.includes('摄像') || action.includes('camera')) return '📹'
    return '⚡'
  }
  return '⚡'
}

function getActionIcon(action) {
  const actionKey = typeof action === 'string' ? action : JSON.stringify(action)
  const prefKey = `task_action:${actionKey}`
  return emojiPrefs.value[prefKey] || getDefaultActionIcon(action)
}

function getActionNodeIcon(ruleId) {
  const prefKey = `task_action_node:${ruleId}`
  return emojiPrefs.value[prefKey] || '⚡'
}

const enabledCount = computed(() => rules.value.filter(r => r.enabled).length)

onMounted(() => {
  loadRules()
  loadEmojiPrefs()
})
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div>
        <h1>自动化规则</h1>
        <p class="page-sub">{{ enabledCount }} 条规则启用中</p>
      </div>
      <button class="btn-add" @click="showCreateForm = !showCreateForm">
        {{ showCreateForm ? '取消' : '+ 新建规则' }}
      </button>
    </header>

    <!-- 创建规则表单 -->
    <div v-if="showCreateForm" class="create-form">
      <div class="create-input-row">
        <input
          v-model="newRuleText"
          class="create-input"
          placeholder="用自然语言描述规则，如：当检测到有人时，关闭客厅灯..."
          @keydown.enter="createRule"
        />
        <button class="btn-create" :disabled="creating || !newRuleText.trim()" @click="createRule">
          {{ creating ? '创建中...' : '创建' }}
        </button>
      </div>
      <p class="create-hint">AI 将自动解析你的描述，生成对应的条件和动作</p>
    </div>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="rules-list">
      <div v-for="rule in rules" :key="rule.id" class="rule-card" @click="openRuleDetail(rule)">
        <div class="rule-toggle-col" @click.stop>
          <BaseToggle :modelValue="rule.enabled" @update:modelValue="toggleRule(rule.id)" />
        </div>

        <div class="rule-flow">
          <div class="rule-header-row">
            <h3>{{ rule.name || rule.condition || `规则 #${rule.id}` }}</h3>
            <span class="rule-status" :class="{ active: rule.enabled }">
              {{ rule.enabled ? '启用' : '停用' }}
            </span>
            <button class="btn-delete" @click.stop="deleteRule(rule.id)" title="删除">&#10005;</button>
          </div>
          <div class="flow-row">
            <div class="flow-node condition">
              <div 
                class="flow-icon emoji-trigger" 
                @click.stop="openEmojiPicker('task_condition', typeof rule.condition === 'string' ? rule.condition : (rule.condition?.description || rule.condition?.type || JSON.stringify(rule.condition)), getConditionIcon(rule.condition))"
              >
                {{ getConditionIcon(rule.condition) }}
              </div>
              <div class="flow-content">
                <div class="flow-label">如果</div>
                <div class="flow-value">{{ formatCondition(rule.condition) }}</div>
              </div>
            </div>
            <div class="flow-arrow">&#10132;</div>
            <div class="flow-node action">
              <div
                class="flow-icon emoji-trigger"
                @click.stop="openEmojiPicker('task_action_node', rule.id || 'default', getActionNodeIcon(rule.id))"
              >{{ getActionNodeIcon(rule.id) }}</div>
              <div class="flow-content">
                <div class="flow-label">则</div>
                <div class="flow-tags">
                  <span v-for="(action, idx) in formatActions(rule.actions || rule.action)" :key="idx" class="flow-tag">{{ action }}</span>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <div v-if="rules.length === 0" class="empty-state empty-state--card">
        暂无自动化规则，点击右上角创建第一条规则
      </div>
    </div>

    <!-- Rule Detail Modal -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="showRuleDetail && selectedRule" class="modal-overlay" @click.self="closeRuleDetail">
          <div class="modal-content">
            <div class="modal-header">
              <h2>规则详情</h2>
              <button class="modal-close" @click="closeRuleDetail">&times;</button>
            </div>
            <div class="modal-body">
              <div class="detail-section">
                <h3>原始数据</h3>
                <pre class="json-view">{{ JSON.stringify(selectedRule, null, 2) }}</pre>
              </div>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>

    <EmojiPicker
      :visible="showEmojiPicker"
      @update:visible="showEmojiPicker = $event"
      @select="onEmojiSelect"
    />
  </div>
</template>

<style scoped>
.loading-state {
  color: var(--color-text-muted);
}

/* 创建表单 */
.create-form {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  border: 1px solid var(--color-border);
  padding: var(--space-14);
  margin-bottom: var(--space-20);
}

.create-input-row {
  display: flex;
  gap: var(--space-6);
}

.create-input {
  flex: 1;
  padding: var(--space-5) var(--space-10);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-lg);
  font-size: var(--text-base);
  font-family: inherit;
  outline: none;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  transition: border-color var(--duration-normal) var(--ease-out);
}

.create-input:focus {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.1);
}

.create-input::placeholder {
  color: var(--color-text-muted);
}

.btn-create {
  background: var(--color-primary);
  color: #fff;
  border: none;
  padding: var(--space-5) var(--space-16);
  border-radius: var(--radius-lg);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
  white-space: nowrap;
}

.btn-create:hover:not(:disabled) {
  background: var(--color-primary-hover);
  transform: translateY(-1px);
}

.btn-create:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.create-hint {
  margin-top: var(--space-4);
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.rules-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.rule-card {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  padding: var(--space-14);
  border: 1px solid var(--color-border);
  display: flex;
  gap: var(--space-10);
  transition: all var(--duration-normal) var(--ease-out);
  cursor: pointer;
}

.rule-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-active);
}

.rule-toggle-col {
  padding-top: var(--space-2);
  flex-shrink: 0;
}

.rule-flow {
  flex: 1;
  min-width: 0;
}

.rule-header-row {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  margin-bottom: var(--space-10);
}

.rule-header-row h3 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rule-status {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-sm);
  font-weight: var(--weight-medium);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.rule-status.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.btn-delete {
  margin-left: auto;
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: var(--text-base);
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  transition: color var(--duration-fast) var(--ease-out);
  border-radius: var(--radius-sm);
  flex-shrink: 0;
}

.btn-delete:hover {
  color: var(--color-danger);
  background: var(--color-danger-bg);
}

.flow-row {
  display: flex;
  align-items: flex-start;
  gap: var(--space-10);
}

.flow-node {
  display: flex;
  gap: var(--space-5);
  padding: var(--space-6) var(--space-8);
  border-radius: var(--radius-lg);
  flex: 1;
  min-width: 0;
}

.flow-node.condition {
  background: var(--color-info-bg);
  border: 1px solid rgba(93, 173, 226, 0.12);
}

.flow-node.action {
  background: rgba(74, 124, 112, 0.06);
  border: 1px solid rgba(74, 124, 112, 0.1);
}

.flow-icon {
  font-size: var(--text-xl);
  flex-shrink: 0;
  margin-top: 1px;
}

.flow-icon.emoji-trigger,
.emoji-trigger.action-emoji {
  cursor: pointer;
  transition: transform var(--duration-fast);
  border-radius: var(--radius-sm);
  padding: var(--space-2);
}

.flow-icon.emoji-trigger:hover,
.emoji-trigger.action-emoji:hover {
  transform: scale(1.2);
  background: var(--color-surface-hover);
}

.flow-content {
  flex: 1;
  min-width: 0;
}

.flow-label {
  font-size: 10px;
  font-weight: var(--weight-semibold);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--space-1);
}

.flow-node.condition .flow-label { color: var(--color-info); }
.flow-node.action .flow-label { color: var(--color-primary); }

.flow-value {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  word-break: break-word;
}

.flow-tags {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.flow-tag {
  font-size: var(--text-sm);
  line-height: 1.5;
  color: var(--color-text-secondary);
  word-break: break-word;
}

.flow-arrow {
  font-size: var(--text-xl);
  color: var(--color-text-muted);
  padding-top: var(--space-20);
  flex-shrink: 0;
}

@media (max-width: 768px) {
  .flow-row {
    flex-direction: column;
    gap: var(--space-4);
  }

  .flow-arrow {
    transform: rotate(90deg);
    text-align: center;
    padding-top: 0;
  }

  .create-input-row {
    flex-direction: column;
  }

  .rule-header-row {
    flex-wrap: wrap;
  }

  .rule-header-row h3 {
    min-width: 0;
    width: 100%;
  }
}

.detail-section {
  margin-bottom: var(--space-14);
}

.detail-section h3 {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text-secondary);
  margin-bottom: var(--space-6);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.json-view {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-8);
  font-family: 'Cascadia Code', 'Fira Code', monospace;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}

/* Modal Transition */
.modal-enter-active,
.modal-leave-active {
  transition: all 0.3s var(--ease-out);
}

.modal-enter-active .modal-content,
.modal-leave-active .modal-content {
  transition: all 0.3s var(--ease-out);
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

.modal-enter-from .modal-content,
.modal-leave-to .modal-content {
  transform: scale(0.95) translateY(20px);
}
</style>
