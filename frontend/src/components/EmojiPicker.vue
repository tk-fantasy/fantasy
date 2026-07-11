<script setup>
import { ref, watch, onMounted, onUnmounted } from 'vue'

const props = defineProps({
  visible: Boolean,
})

const emit = defineEmits(['update:visible', 'select'])

const searchQuery = ref('')
const results = ref([])
const loading = ref(false)
const status = ref('') // 'ok' | 'loading' | 'not_loaded'
let debounceTimer = null

// 搜索 emoji
async function searchEmoji(query) {
  if (!query.trim()) {
    results.value = []
    return
  }
  
  loading.value = true
  try {
    const res = await fetch(`/api/emoji/search?q=${encodeURIComponent(query)}&top_k=30`)
    const json = await res.json()
    status.value = json.data?.status || 'ok'
    results.value = json.data?.results || []
  } catch (e) {
    console.error('Emoji search failed:', e)
    results.value = []
  } finally {
    loading.value = false
  }
}

// 防抖搜索
function onInput() {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    searchEmoji(searchQuery.value)
  }, 300)
}

// 选择 emoji
function selectEmoji(item) {
  emit('select', item)
  close()
}

// 关闭
function close() {
  emit('update:visible', false)
  searchQuery.value = ''
  results.value = []
}

// 点击外部关闭
function onClickOutside(e) {
  const picker = document.querySelector('.emoji-picker-overlay')
  if (picker && !picker.contains(e.target)) {
    // 检查是否点击了触发按钮
    const trigger = e.target.closest('.emoji-trigger')
    if (!trigger) {
      close()
    }
  }
}

// ESC 关闭
function onKeydown(e) {
  if (e.key === 'Escape' && props.visible) {
    close()
  }
}

watch(() => props.visible, (val) => {
  if (val) {
    document.addEventListener('click', onClickOutside)
    document.addEventListener('keydown', onKeydown)
  } else {
    document.removeEventListener('click', onClickOutside)
    document.removeEventListener('keydown', onKeydown)
  }
})

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
})

onUnmounted(() => {
  clearTimeout(debounceTimer)
  document.removeEventListener('click', onClickOutside)
  document.removeEventListener('keydown', onKeydown)
})
</script>

<template>
  <Teleport to="body">
    <div v-if="visible" class="emoji-picker-overlay" @click.self="close">
      <div class="emoji-picker">
        <div class="emoji-picker-header">
          <span class="emoji-picker-title">选择 Emoji</span>
          <button class="emoji-picker-close" @click="close">×</button>
        </div>
        
        <div class="emoji-picker-search">
          <input
            v-model="searchQuery"
            type="text"
            placeholder="搜索 emoji（如：下雨、开心、太阳）"
            @input="onInput"
            autofocus
          />
          <span v-if="loading" class="emoji-picker-loading">搜索中...</span>
        </div>
        
        <div v-if="status === 'not_loaded'" class="emoji-picker-status">
          索引加载中，请稍后再试...
        </div>
        
        <div v-else-if="status === 'loading'" class="emoji-picker-status">
          索引正在加载...
        </div>
        
        <div v-else class="emoji-picker-results">
          <div v-if="results.length === 0 && searchQuery" class="emoji-picker-empty">
            未找到匹配的 emoji
          </div>
          <div v-else-if="!searchQuery" class="emoji-picker-hint">
            输入关键词搜索 emoji
          </div>
          <div v-else class="emoji-grid">
            <button
              v-for="item in results"
              :key="item.char"
              class="emoji-item"
              :title="`${item.name} (${item.score})`"
              @click="selectEmoji(item)"
            >
              <span class="emoji-char">{{ item.char }}</span>
              <span class="emoji-name">{{ item.name }}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.emoji-picker-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg);
  -webkit-backdrop-filter: blur(4px);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 9999;
  animation: fadeIn 0.15s ease;
}

@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.emoji-picker {
  width: 420px;
  max-height: 80vh;
  background: var(--color-bg-app);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-xl);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  animation: slideUp 0.2s ease;
}

@keyframes slideUp {
  from { transform: translateY(20px); opacity: 0; }
  to { transform: translateY(0); opacity: 1; }
}

.emoji-picker-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-12) var(--space-16);
  border-bottom: 1px solid var(--color-border);
}

.emoji-picker-title {
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.emoji-picker-close {
  width: 28px;
  height: 28px;
  border: none;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  border-radius: var(--radius-full);
  cursor: pointer;
  font-size: 18px;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all var(--duration-fast);
}

.emoji-picker-close:hover {
  background: var(--color-surface-hover);
  color: var(--color-text);
}

.emoji-picker-search {
  padding: var(--space-12) var(--space-16);
  position: relative;
}

.emoji-picker-search input {
  width: 100%;
  padding: var(--space-8) var(--space-12);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  color: var(--color-text);
  font-size: var(--text-sm);
  outline: none;
  transition: border-color var(--duration-fast);
}

.emoji-picker-search input:focus {
  border-color: var(--color-primary);
}

.emoji-picker-search input::placeholder {
  color: var(--color-text-muted);
}

.emoji-picker-loading {
  position: absolute;
  right: var(--space-20);
  top: 50%;
  transform: translateY(-50%);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
}

.emoji-picker-status {
  padding: var(--space-24);
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}

.emoji-picker-results {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-8) var(--space-16) var(--space-16);
}

.emoji-picker-empty,
.emoji-picker-hint {
  padding: var(--space-24);
  text-align: center;
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
}

.emoji-grid {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: var(--space-8);
}

.emoji-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-8);
  background: var(--color-surface);
  border: 1px solid transparent;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-fast);
}

.emoji-item:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
  transform: translateY(-2px);
}

.emoji-char {
  font-size: 28px;
  line-height: 1;
}

.emoji-name {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  text-align: center;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
</style>
