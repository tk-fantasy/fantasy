<script setup>
// 场景模式条 — 设备页顶部的场景 chips：一键应用 / 捕获当前状态创建 / 删除。
// 独立组件零侵入挂进 HAListView。
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
  <section v-if="!loading" class="scene-bar">
    <div class="scene-bar-header">
      <span class="scene-bar-title">🏠 场景模式</span>
      <span v-if="lastResult" class="scene-msg ok">{{ lastResult }}</span>
      <span v-else-if="errorMsg" class="scene-msg err">{{ errorMsg }}</span>
    </div>
    <div class="scene-chips">
      <button v-for="s in scenes" :key="s.id" class="scene-chip"
        :disabled="applying === s.id" @click="applyScene(s)" :title="`${s.actions.length} 个动作`">
        <span class="chip-name">{{ s.name }}</span>
        <span class="chip-del" @click.stop="removeScene(s)">×</span>
      </button>
      <span v-if="!scenes.length" class="scene-empty">还没有场景——把设备调到想要的状态，起个名字保存</span>
      <span class="scene-new">
        <input v-model="newSceneName" class="scene-input" placeholder="场景名（保存当前状态）"
          @keyup.enter="captureScene" />
        <button class="scene-add-btn" :disabled="capturing || !newSceneName.trim()" @click="captureScene">
          {{ capturing ? '保存中…' : '保存当前状态' }}
        </button>
      </span>
    </div>
  </section>
</template>

<style scoped>
.scene-bar { margin-bottom: 14px; padding: 12px 14px; border: 1px solid var(--border, #ddd); border-radius: 12px; background: var(--bg-soft, #fafafa); }
.scene-bar-header { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
.scene-bar-title { font-size: 13px; font-weight: 600; color: var(--text, #333); }
.scene-msg { font-size: 12px; }
.scene-msg.ok { color: #2e7d32; }
.scene-msg.err { color: #c62828; }
.scene-chips { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.scene-chip { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px; border: 1px solid var(--border, #ddd); background: var(--bg, #fff); cursor: pointer; font-size: 13px; }
.scene-chip:hover { border-color: var(--accent, #4a90d9); }
.scene-chip:disabled { opacity: .6; cursor: wait; }
.chip-del { color: #999; font-size: 14px; line-height: 1; padding: 0 2px; }
.chip-del:hover { color: #c62828; }
.scene-empty { font-size: 12px; color: #999; }
.scene-new { display: inline-flex; gap: 6px; align-items: center; }
.scene-input { padding: 5px 10px; border: 1px solid var(--border, #ddd); border-radius: 8px; font-size: 12px; width: 170px; background: var(--bg, #fff); color: var(--text, #333); }
.scene-add-btn { padding: 5px 12px; border-radius: 8px; border: none; background: var(--accent, #4a90d9); color: #fff; font-size: 12px; cursor: pointer; }
.scene-add-btn:disabled { opacity: .5; }
</style>
