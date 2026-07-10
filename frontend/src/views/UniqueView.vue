<script setup>
import { ref, onMounted } from 'vue'
import { apiGet } from '../utils/api'

const loading = ref(true)
const saving = ref(false)
const saved = ref(false)

const persona = ref('')

const customized = ref({
  persona: false,
})

async function loadUnique() {
  try {
    loading.value = true
    const data = await apiGet('/api/unique') || {}

    persona.value = data.persona || ''

    customized.value.persona = data.persona_custom || false
  } catch (e) {
    console.error('Failed to load unique config:', e)
  } finally {
    loading.value = false
  }
}

async function saveUnique() {
  try {
    saving.value = true
    saved.value = false
    const res = await fetch('/api/unique', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        persona: persona.value,
      }),
    })
    const json = await res.json()
    const data = json.data || json || {}
    
    customized.value.persona = data.persona_custom || false

    saved.value = true
    setTimeout(() => { saved.value = false }, 2000)
  } catch (e) {
    console.error('Failed to save unique config:', e)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadUnique()
})
</script>

<template>
  <div class="page">
    <header class="page-header">
      <h1>助手人格</h1>
      <p class="page-sub">自定义助手的角色设定、能力和行为原则</p>
    </header>

    <div v-if="loading" class="empty-state empty-state--column">
      <span class="empty-icon">&#9203;</span>
      <span class="empty-text">加载中...</span>
    </div>

    <div v-else class="settings-sections">
      <section class="setting-section">
        <h2 class="section-title">
          <span class="section-icon">&#127917;</span>
          角色设定
          <span v-if="customized.persona" class="custom-badge">已自定义</span>
        </h2>
        <div class="setting-card">
          <textarea
            v-model="persona"
            class="setting-textarea"
            rows="6"
            placeholder="描述助手的角色身份、性格特征和交互风格..."
          ></textarea>
        </div>
      </section>

      <div class="save-bar">
        <button class="btn-save" :disabled="saving" @click="saveUnique">
          {{ saving ? '保存中...' : saved ? '已保存' : '保存设置' }}
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-sections {
  gap: var(--space-16);
}

.custom-badge {
  font-size: 10px;
  font-weight: var(--weight-semibold);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-full);
  background: var(--color-primary-light);
  color: var(--color-primary);
  letter-spacing: 0;
  text-transform: none;
}

.setting-card {
  padding: var(--space-10) var(--space-14);
  transition: border-color var(--duration-normal) var(--ease-out);
}

.setting-card:focus-within {
  border-color: var(--color-border-active);
}

.setting-textarea {
  width: 100%;
  border: none;
  outline: none;
  background: transparent;
  color: var(--color-text);
  font-family: inherit;
  font-size: var(--text-base);
  line-height: 1.6;
  resize: vertical;
  min-height: 120px;
}

.setting-textarea::placeholder {
  color: var(--color-text-muted);
}

.btn-save {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid rgba(74, 124, 112, 0.25);
  padding: var(--space-5) var(--space-14);
  border-radius: var(--radius-full);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  transition: all var(--duration-normal) var(--ease-out);
  white-space: nowrap;
  font-family: inherit;
}

.btn-save:hover:not(:disabled) {
  background: var(--color-primary-hover);
  border-color: rgba(74, 124, 112, 0.35);
  transform: translateY(-1px);
  box-shadow: var(--shadow-glow);
}

.btn-save:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .setting-card {
    padding: var(--space-8) var(--space-10);
  }

  .setting-textarea {
    font-size: var(--text-sm);
  }
}
</style>
