<script setup>
import { ref, computed, onMounted } from 'vue'
import { apiGet, apiPut } from '../utils/api'

// 已配置的映射：{ entity_id: { mappings: { svc: {target, description} } } }
const actionMaps = ref({})
// 全部实体列表（取 name / entity_id）
const entities = ref([])
// 按域的可用 services：{ domain: [svc_name, ...] }
const domainServices = ref({})
const loading = ref(true)
const saving = ref(false)
const error = ref('')

// Modal 状态
const showModal = ref(false)
const selectedEntityId = ref('')
const searchKeyword = ref('')
// 当前编辑的映射草稿：{ svc: { target, description } }
const draftMappings = ref({})

// 实体名称查找
const entityNameMap = computed(() => {
  const m = {}
  for (const e of entities.value) {
    const name = e.attributes?.friendly_name || e.entity_id
    m[e.entity_id] = name
  }
  return m
})

// 搜索过滤后的实体列表
const filteredEntities = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return entities.value
  return entities.value.filter((e) => {
    const name = (e.attributes?.friendly_name || '').toLowerCase()
    return name.includes(kw) || e.entity_id.toLowerCase().includes(kw)
  })
})

// 选中实体的可用 services（按域）
const selectedServices = computed(() => {
  if (!selectedEntityId.value) return []
  const domain = selectedEntityId.value.split('.')[0]
  return domainServices.value[domain] || []
})

// 已配置映射的实体卡片列表
const configuredList = computed(() => {
  return Object.entries(actionMaps.value).map(([eid, cfg]) => {
    const mappings = cfg.mappings || {}
    const summary = Object.entries(mappings)
      .map(([svc, e]) => `${svc}→${e.target}`)
      .join('、')
    return { entityId: eid, name: entityNameMap.value[eid] || eid, mappings, summary }
  })
})

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [mapsData, entData, svcData] = await Promise.all([
      apiGet('/api/ha/action-maps'),
      apiGet('/api/ha/entities'),
      apiGet('/api/ha/entity-services'),
    ])
    actionMaps.value = mapsData.maps || {}
    entities.value = entData.entities || []
    domainServices.value = svcData.services || {}
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function openAddModal() {
  selectedEntityId.value = ''
  searchKeyword.value = ''
  draftMappings.value = {}
  showModal.value = true
}

function openEditModal(entityId) {
  selectedEntityId.value = entityId
  searchKeyword.value = ''
  const existing = actionMaps.value[entityId]?.mappings || {}
  // 深拷贝到草稿
  draftMappings.value = JSON.parse(JSON.stringify(existing))
  showModal.value = true
}

function selectEntity(eid) {
  selectedEntityId.value = eid
  const existing = actionMaps.value[eid]?.mappings || {}
  draftMappings.value = JSON.parse(JSON.stringify(existing))
}

// 草稿里某 service 的 target（默认=自身）
function targetOf(svc) {
  return draftMappings.value[svc]?.target || svc
}
function descOf(svc) {
  return draftMappings.value[svc]?.description || ''
}
function setTarget(svc, target) {
  if (!draftMappings.value[svc]) draftMappings.value[svc] = { target: svc, description: '' }
  draftMappings.value[svc].target = target
}
function setDesc(svc, desc) {
  if (!draftMappings.value[svc]) draftMappings.value[svc] = { target: svc, description: '' }
  draftMappings.value[svc].description = desc
}
function isMapped(svc) {
  return targetOf(svc) !== svc
}

async function saveMappings() {
  if (!selectedEntityId.value) return
  saving.value = true
  error.value = ''
  // 只收集 target≠自身 的
  const cleaned = {}
  for (const [svc, entry] of Object.entries(draftMappings.value)) {
    if (entry.target && entry.target !== svc) {
      cleaned[svc] = { target: entry.target, description: entry.description || '' }
    }
  }
  try {
    await apiPut('/api/ha/action-maps', { entity_id: selectedEntityId.value, mappings: cleaned })
    // 更新本地
    if (Object.keys(cleaned).length) {
      actionMaps.value[selectedEntityId.value] = { mappings: cleaned }
    } else {
      delete actionMaps.value[selectedEntityId.value]
    }
    showModal.value = false
  } catch (e) {
    error.value = e.message || '保存失败'
  } finally {
    saving.value = false
  }
}

async function deleteAllMappings(entityId) {
  if (!confirm(`确认删除「${entityNameMap.value[entityId] || entityId}」的全部映射？`)) return
  saving.value = true
  try {
    await apiPut('/api/ha/action-maps', { entity_id: entityId, mappings: {} })
    delete actionMaps.value[entityId]
    if (selectedEntityId.value === entityId) showModal.value = false
  } catch (e) {
    error.value = e.message || '删除失败'
  } finally {
    saving.value = false
  }
}

onMounted(loadData)
</script>

<template>
  <div class="semantics-page">
    <header class="page-header">
      <h1>语义映射</h1>
      <p class="hint">配置设备的动作映射（如门禁继电器 turn_on↔turn_off），系统会自动适配物理操作。</p>
    </header>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="error && !showModal" class="error">{{ error }}</div>

    <section v-if="!loading" class="configured-list">
      <div v-if="configuredList.length === 0" class="empty">
        <p>尚未配置任何映射</p>
      </div>
      <div v-for="item in configuredList" :key="item.entityId" class="map-card">
        <div class="card-main" @click="openEditModal(item.entityId)">
          <div class="card-title">{{ item.name }}</div>
          <div class="card-sub">{{ item.entityId }}</div>
          <div class="card-summary">{{ item.summary }}</div>
        </div>
        <button class="btn-delete" @click.stop="deleteAllMappings(item.entityId)">删除</button>
      </div>
      <button class="btn-add" @click="openAddModal">+ 添加设备</button>
    </section>

    <!-- 配置 Modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="showModal = false">
      <div class="modal-content semantics-modal">
        <div class="modal-header">
          <h2>配置语义映射</h2>
          <button class="btn-close" @click="showModal = false">×</button>
        </div>
        <div v-if="error" class="error">{{ error }}</div>

        <!-- 一级：实体选择（仅未选时显示） -->
        <div v-if="!selectedEntityId" class="entity-picker">
          <input v-model="searchKeyword" class="search-input" placeholder="搜索设备名称或 entity_id…" />
          <div class="entity-list">
            <div
              v-for="e in filteredEntities"
              :key="e.entity_id"
              class="entity-row"
              @click="selectEntity(e.entity_id)"
            >
              <span class="er-name">{{ e.attributes?.friendly_name || e.entity_id }}</span>
              <span class="er-id">{{ e.entity_id }}</span>
            </div>
          </div>
        </div>

        <!-- 二级：服务映射配置（选中实体后显示） -->
        <div v-else class="service-config">
          <div class="selected-entity">
            <button class="btn-back" @click="selectedEntityId = ''">← 返回选择</button>
            <span class="se-name">{{ entityNameMap[selectedEntityId] || selectedEntityId }}</span>
            <span class="se-id">{{ selectedEntityId }}</span>
          </div>
          <div v-if="selectedServices.length === 0" class="empty-services">
            该设备域无可用服务
          </div>
          <div v-else class="service-rows">
            <div v-for="svc in selectedServices" :key="svc" class="service-row">
              <div class="sr-head">
                <span class="sr-svc">{{ svc }}</span>
                <span class="sr-arrow">→</span>
                <select
                  class="sr-select"
                  :value="targetOf(svc)"
                  @change="setTarget(svc, $event.target.value)"
                >
                  <option v-for="t in selectedServices" :key="t" :value="t">{{ t }}</option>
                </select>
              </div>
              <input
                v-if="isMapped(svc)"
                class="sr-desc"
                :value="descOf(svc)"
                @input="setDesc(svc, $event.target.value)"
                placeholder="描述（映射触发时带给 AI，解释实际发生了什么）"
              />
            </div>
          </div>
          <div class="modal-actions">
            <button class="btn-delete" @click="deleteAllMappings(selectedEntityId)">删除全部映射</button>
            <button class="btn-save" :disabled="saving" @click="saveMappings">
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.semantics-page { padding: 20px; max-width: 800px; margin: 0 auto; }
.page-header h1 { font-size: 22px; margin-bottom: 4px; }
.hint { color: var(--color-text-secondary, #888); font-size: 13px; margin-bottom: 16px; }
.loading, .empty, .error { padding: 24px; text-align: center; color: var(--color-text-secondary, #888); }
.error { color: var(--color-danger, #e5484d); }
.configured-list { display: flex; flex-direction: column; gap: 8px; }
.map-card {
  display: flex; align-items: center; gap: 8px;
  background: var(--color-surface, rgba(255,255,255,0.04));
  border: 1px solid var(--color-border, rgba(255,255,255,0.1));
  border-radius: 12px; padding: 12px;
}
.card-main { flex: 1; cursor: pointer; }
.card-title { font-weight: 600; }
.card-sub { font-size: 12px; color: var(--color-text-secondary, #888); }
.card-summary { font-size: 13px; margin-top: 4px; color: var(--color-text-secondary, #aaa); }
.btn-add { margin-top: 8px; }
.btn-delete { color: var(--color-danger, #e5484d); background: transparent; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); border-radius: 8px; padding: 6px 12px; cursor: pointer; }
.btn-save { background: var(--color-primary, #4c6ef5); color: #fff; border: none; border-radius: 8px; padding: 8px 16px; cursor: pointer; }
.btn-save:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-close { background: transparent; border: none; font-size: 22px; cursor: pointer; color: var(--color-text-secondary, #888); }
.semantics-modal { max-width: 640px; }
.entity-picker { padding: 16px; }
.search-input { width: 100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); background: var(--color-surface, rgba(255,255,255,0.04)); color: var(--color-text, #fff); margin-bottom: 12px; box-sizing: border-box; }
.entity-list { max-height: 360px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.entity-row { display: flex; justify-content: space-between; padding: 8px 12px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.entity-row:hover { background: var(--color-surface-hover, rgba(255,255,255,0.08)); border-color: var(--color-border, rgba(255,255,255,0.1)); }
.er-name { font-weight: 500; }
.er-id { font-size: 12px; color: var(--color-text-secondary, #888); }
.service-config { padding: 16px; }
.selected-entity { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
.btn-back { background: transparent; border: none; cursor: pointer; color: var(--color-text-secondary, #888); }
.se-name { font-weight: 600; }
.se-id { font-size: 12px; color: var(--color-text-secondary, #888); }
.empty-services { padding: 24px; text-align: center; color: var(--color-text-secondary, #888); }
.service-rows { display: flex; flex-direction: column; gap: 12px; max-height: 360px; overflow-y: auto; }
.service-row { border: 1px solid var(--color-border, rgba(255,255,255,0.1)); border-radius: 8px; padding: 8px 12px; }
.sr-head { display: flex; align-items: center; gap: 8px; }
.sr-svc { font-weight: 500; min-width: 120px; }
.sr-arrow { color: var(--color-text-secondary, #888); }
.sr-select { flex: 1; padding: 4px 8px; border-radius: 6px; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); background: var(--color-surface, rgba(255,255,255,0.04)); color: var(--color-text, #fff); }
.sr-desc { width: 100%; margin-top: 8px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); background: var(--color-surface, rgba(255,255,255,0.04)); color: var(--color-text, #fff); box-sizing: border-box; font-size: 13px; }
.modal-actions { display: flex; justify-content: space-between; margin-top: 16px; }
</style>
