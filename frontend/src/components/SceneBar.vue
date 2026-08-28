<script setup>
// 场景模式条 — 设备页顶部的场景 chips：一键应用 / 捕获当前状态创建 / 删除。
// 样式全部走全局设计 token（与 area-tab / setting-card / btn-add 同一语言），
// 深浅色主题自动适配，零硬编码色值。
import { ref, onMounted } from 'vue'
import { apiGet, apiPost, apiDelete } from '../utils/api'

const scenes = ref([])
const loading = ref(true)
const applying = ref('')
const capturing = ref(false)
const newSceneName = ref('')
const errorMsg = ref('')
const lastResult = ref('')

async function loadScenes() {
  try {
    const data = await apiGet('/api/scenes')
    scenes.value = data || []
  } catch (e) {
    errorMsg.value = '加载场景失败：' + (e.message || e)
  } finally {
    loading.value = false
  }
}

async function applyScene(scene) {
  applying.value = scene.id
  errorMsg.value = ''
  lastResult.value = ''
  try {
    const r = await apiPost(`/api/scenes/${scene.id}/apply`, {})
    lastResult.value = r.ok === r.total
      ? `✅ 「${r.scene}」已应用`
      : `⚠️ 「${r.scene}」${r.ok}/${r.total} 个设备成功`
  } catch (e) {
    errorMsg.value = '应用失败：' + (e.message || e)
  } finally {
    applying.value = ''
  }
}

async function captureScene() {
  const name = newSceneName.value.trim()
  if (!name) return
  capturing.value = true
  errorMsg.value = ''
  try {
    await apiPost('/api/scenes', { name, capture: true })
    newSceneName.value = ''
    lastResult.value = `✅ 已把当前设备状态保存为「${name}」`
    await loadScenes()
  } catch (e) {
    errorMsg.value = '保存失败：' + (e.message || e)
  } finally {
    capturing.value = false
  }
}

async function removeScene(scene) {
  if (!confirm(`删除场景「${scene.name}」？`)) return
  try {
    await apiDelete(`/api/scenes/${scene.id}`)
    await loadScenes()
  } catch (e) {
    errorMsg.value = '删除失败：' + (e.message || e)
  }
}

onMounted(loadScenes)
</script>

<template>
  <section v-if="!loading" class="scene-bar setting-card">
    <div class="scene-bar-header">
      <span class="scene-bar-title">🏠 场景模式</span>
      <span v-if="lastResult" class="scene-msg ok">{{ lastResult }}</span>
      <span v-else-if="errorMsg" class="scene-msg err">{{ errorMsg }}</span>
    </div>
    <div class="scene-chips">
      <span v-for="s in scenes" :key="s.id" class="scene-chip"
        :class="{ applying: applying === s.id }" :title="`${s.actions.length} 个动作，点击应用`">
        <button class="chip-main" :disabled="applying === s.id" @click="applyScene(s)">
          {{ s.name }}
        </button>
        <button class="chip-del" :disabled="applying === s.id" title="删除场景"
          @click.stop="removeScene(s)">×</button>
      </span>
      <span v-if="!scenes.length" class="scene-empty">
        还没有场景——把设备调到想要的状态，起个名字保存
      </span>
      <span class="scene-new">
        <input v-model="newSceneName" class="scene-input" placeholder="场景名（保存当前状态）"
          @keyup.enter="captureScene" />
        <button class="btn-add scene-add-btn" :disabled="capturing || !newSceneName.trim()"
          @click="captureScene">
          {{ capturing ? '保存中…' : '+ 保存当前状态' }}
        </button>
      </span>
    </div>
  </section>
</template>

<style scoped>
.scene-bar {
  padding: var(--space-10) var(--space-14);
  margin-bottom: var(--space-16);
}

.scene-bar-header {
  display: flex;
  align-items: baseline;
  gap: var(--space-10);
  margin-bottom: var(--space-8);
}

.scene-bar-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-text-secondary);
}

.scene-msg {
  font-size: var(--text-xs);
}

.scene-msg.ok { color: var(--color-success); }
.scene-msg.err { color: var(--color-danger); }

/* 芯片：与设备页 area-tab 同构（胶囊、透明底、主色激活态） */
.scene-chips {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: center;
}

.scene-chip {
  display: inline-flex;
  align-items: center;
  border-radius: var(--radius-full);
  border: 1px solid var(--color-border);
  background: transparent;
  transition: all var(--duration-normal) var(--ease-out);
}

.scene-chip:hover {
  background: var(--color-surface);
  border-color: var(--color-border-hover);
}

.scene-chip.applying {
  background: var(--color-primary-light);
  border-color: var(--color-border-active);
}

.chip-main {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  font-family: inherit;
  padding: var(--space-3) var(--space-2) var(--space-3) var(--space-12);
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease-out);
}

.scene-chip:hover .chip-main { color: var(--color-text); }
.scene-chip.applying .chip-main { color: var(--color-primary); }
.chip-main:disabled { cursor: wait; }

.chip-del {
  border: none;
  background: transparent;
  color: var(--color-text-muted);
  font-size: var(--text-sm);
  line-height: 1;
  font-family: inherit;
  padding: var(--space-3) var(--space-10) var(--space-3) 0;
  cursor: pointer;
  transition: color var(--duration-fast) var(--ease-out);
}

.chip-del:hover { color: var(--color-danger); }
.chip-del:disabled { cursor: wait; }

.scene-empty {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.scene-new {
  display: inline-flex;
  gap: var(--space-2);
  align-items: center;
}

.scene-input {
  width: 180px;
  padding: var(--space-3) var(--space-10);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-family: inherit;
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  outline: none;
  transition: border-color var(--duration-normal) var(--ease-out);
}

.scene-input:focus {
  border-color: var(--color-border-active);
  box-shadow: 0 0 0 3px rgba(74, 124, 112, 0.1);
}

.scene-input::placeholder { color: var(--color-text-muted); }

.scene-add-btn {
  font-size: var(--text-xs);
  padding: var(--space-3) var(--space-10);
}

.scene-add-btn:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

@media (max-width: 768px) {
  .scene-input { width: 100%; }
  .scene-new { width: 100%; }
}
</style>
