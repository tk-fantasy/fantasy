<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { apiGet } from '../utils/api'

const router = useRouter()
const sessions = ref([])
const loading = ref(true)
const deleting = ref(null)

async function loadSessions() {
  try {
    loading.value = true
    sessions.value = await apiGet('/api/sessions') || []
  } catch (e) {
    console.error('Failed to load sessions:', e)
  } finally {
    loading.value = false
  }
}

async function deleteSession(id) {
  try {
    deleting.value = id
    await fetch(`/api/sessions/${id}`, { method: 'DELETE' })
    sessions.value = sessions.value.filter(s => s.id !== id)
  } catch (e) {
    console.error('Failed to delete session:', e)
  } finally {
    deleting.value = null
  }
}

function openSession(id) {
  router.push({ path: '/chat', query: { session: id } })
}

function formatTime(ts) {
  if (!ts) return '-'
  const d = new Date(ts)
  const pad = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

onMounted(() => {
  loadSessions()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>会话管理</h1>
      <p class="page-sub">浏览和切换历史对话</p>
    </header>

    <div v-if="loading" class="empty-state empty-state--column">
      <span class="empty-icon">&#9203;</span>
      <span class="empty-text">加载中...</span>
    </div>

    <div v-else-if="sessions.length === 0" class="empty-state empty-state--column">
      <span class="empty-icon">&#128172;</span>
      <span class="empty-text">暂无会话记录</span>
    </div>

    <div v-else class="sessions-list">
      <div
        v-for="session in sessions"
        :key="session.id"
        class="session-card"
        @click="openSession(session.id)"
      >
        <div class="session-main">
          <div class="session-id-row">
            <span class="session-id" :title="session.id">{{ session.id }}</span>
            <span class="session-badge">{{ session.message_count || 0 }} 条消息</span>
          </div>
          <div class="session-meta">
            <span class="meta-item">
              <span class="meta-label">创建</span>
              <span class="meta-value">{{ formatTime(session.created_at) }}</span>
            </span>
            <span class="meta-item">
              <span class="meta-label">更新</span>
              <span class="meta-value">{{ formatTime(session.updated_at) }}</span>
            </span>
          </div>
        </div>
        <button
          class="btn-delete"
          :disabled="deleting === session.id"
          @click.stop="deleteSession(session.id)"
          :title="deleting === session.id ? '删除中...' : '删除会话'"
        >
          {{ deleting === session.id ? '&#8987;' : '&#10005;' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sessions-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.session-card {
  display: flex;
  align-items: center;
  gap: var(--space-10);
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  border: 1px solid var(--color-border);
  padding: var(--space-10) var(--space-14);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
}

.session-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-active);
  transform: translateY(-2px);
  box-shadow: var(--elevation-2);
}

.session-main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.session-id-row {
  display: flex;
  align-items: center;
  gap: var(--space-6);
}

.session-id {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 320px;
}

.session-badge {
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
  white-space: nowrap;
  flex-shrink: 0;
}

.session-meta {
  display: flex;
  gap: var(--space-16);
}

.meta-item {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.meta-label {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-weight: var(--weight-medium);
}

.meta-value {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.btn-delete {
  background: none;
  border: none;
  color: var(--color-text-muted);
  font-size: var(--text-base);
  cursor: pointer;
  padding: var(--space-3) var(--space-4);
  transition: color var(--duration-fast) var(--ease-out), background var(--duration-fast) var(--ease-out);
  border-radius: var(--radius-md);
  flex-shrink: 0;
  line-height: 1;
}

.btn-delete:hover {
  color: var(--color-danger);
  background: var(--color-danger-bg);
}

.btn-delete:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .session-card {
    flex-direction: column;
    align-items: flex-start;
    gap: var(--space-6);
  }

  .btn-delete {
    align-self: flex-end;
  }

  .session-id {
    max-width: 200px;
  }

  .session-meta {
    flex-direction: column;
    gap: var(--space-3);
  }
}
</style>
