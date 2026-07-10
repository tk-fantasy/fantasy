<script setup>
import { ref, onMounted } from 'vue'
import BaseToggle from '../components/BaseToggle.vue'
import { apiGet } from '../utils/api'

const focuses = ref([])
const newItem = ref('')
const loading = ref(true)
const adding = ref(false)

async function loadFocuses() {
  try {
    loading.value = true
    focuses.value = ((await apiGet('/api/vision/focuses')) || []).map((f) => ({
      ...f,
      enabled: f.enabled !== false,
    }))
  } catch (e) {
    console.error('Failed to load focuses:', e)
  } finally {
    loading.value = false
  }
}

async function addFocus() {
  const text = newItem.value.trim()
  if (!text) return
  try {
    adding.value = true
    const res = await fetch('/api/vision/focuses', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, enabled: true }),
    })
    const json = await res.json()
    focuses.value.push(json.data || { id: Date.now(), text, enabled: true })
    newItem.value = ''
  } catch (e) {
    console.error('Failed to add focus:', e)
  } finally {
    adding.value = false
  }
}

async function toggleFocus(id) {
  const focus = focuses.value.find((f) => f.id === id)
  if (!focus) return
  const newVal = !focus.enabled
  try {
    await fetch(`/api/vision/focuses/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: newVal }),
    })
    focus.enabled = newVal
  } catch (e) {
    console.error('Failed to toggle focus:', e)
  }
}

async function deleteFocus(id) {
  try {
    await fetch(`/api/vision/focuses/${id}`, { method: 'DELETE' })
    focuses.value = focuses.value.filter((f) => f.id !== id)
  } catch (e) {
    console.error('Failed to delete focus:', e)
  }
}

function handleKeydown(e) {
  if (e.key === 'Enter') addFocus()
}

onMounted(loadFocuses)
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div>
        <h1>视觉关注</h1>
        <p class="page-sub">{{ focuses.filter((f) => f.enabled).length }} 项关注启用中</p>
      </div>
    </header>

    <div class="add-bar">
      <input
        v-model="newItem"
        class="add-input"
        placeholder="输入新的关注项..."
        @keydown="handleKeydown"
      />
      <button class="btn-add" :disabled="adding || !newItem.trim()" @click="addFocus">
        {{ adding ? '添加中...' : '+ 添加' }}
      </button>
    </div>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="focus-list">
      <div v-for="focus in focuses" :key="focus.id" class="focus-card">
        <div class="focus-content">
          <span class="focus-text">{{ focus.text }}</span>
          <span class="focus-status" :class="{ active: focus.enabled }">
            {{ focus.enabled ? '启用' : '停用' }}
          </span>
        </div>
        <div class="focus-actions">
          <BaseToggle :modelValue="focus.enabled" @update:modelValue="toggleFocus(focus.id)" />
          <button class="btn-delete" @click="deleteFocus(focus.id)" title="删除">&#10005;</button>
        </div>
      </div>

      <div v-if="focuses.length === 0" class="empty-state empty-state--card">
        暂无关注项，请添加需要视觉识别关注的内容。
      </div>
    </div>
  </div>
</template>

<style scoped>
.loading-state {
  color: var(--color-text-muted);
}

.add-bar {
  display: flex;
  gap: var(--space-6);
  margin-bottom: var(--space-16);
}

.add-input {
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

.add-input:focus {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.1);
}

.add-input::placeholder {
  color: var(--color-text-muted);
}

.focus-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.focus-card {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  padding: var(--space-10) var(--space-14);
  border: 1px solid var(--color-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  transition: all var(--duration-normal) var(--ease-out);
}

.focus-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-active);
}

.focus-content {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  flex: 1;
  min-width: 0;
}

.focus-text {
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.focus-status {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-sm);
  font-weight: var(--weight-medium);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-tertiary);
  flex-shrink: 0;
}

.focus-status.active {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.focus-actions {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  flex-shrink: 0;
}

.btn-delete {
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: var(--text-base);
  cursor: pointer;
  padding: var(--space-1) var(--space-2);
  transition: color var(--duration-fast) var(--ease-out);
  border-radius: var(--radius-sm);
}

.btn-delete:hover {
  color: var(--color-danger);
  background: var(--color-danger-bg);
}

@media (max-width: 768px) {
  .add-bar {
    flex-direction: column;
  }

  .focus-card {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-6);
  }

  .focus-actions {
    align-self: flex-end;
  }
}
</style>
