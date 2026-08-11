/**
 * 实体别名 + 备注管理 composable（从 HAListView 拆出）。
 *
 * - 别名：用户自定义实体显示名，覆盖 HA 生成的难看名字
 * - 备注：用户自定义，注入 AI 认知，影响调用决策（如继电器反转语义）
 *
 * 依赖外部传入的 selectedEntity / selectedDevice（设备详情弹窗当前选中），
 * 因为别名/备注的编辑是针对弹窗内选中的实体操作的。
 */
import { ref } from 'vue'

export function useEntityMeta(selectedEntity, selectedDevice) {
  const entityAliases = ref({})         // {entity_id: alias}
  const editingName = ref(false)
  const nameInput = ref('')

  const entityNotes = ref({})          // {entity_id: note}
  const noteInput = ref('')
  const editingNote = ref(false)

  // ======================== 别名 ========================

  async function loadEntityAliases() {
    try {
      const res = await fetch('/api/ha/entity-aliases', { credentials: 'include' })
      const json = await res.json()
      entityAliases.value = json.data?.aliases || {}
    } catch (e) {
      console.error('Failed to load entity aliases:', e)
    }
  }

  function startEditName() {
    if (!selectedEntity.value) return
    nameInput.value = selectedEntity.value.name || selectedEntity.value.entity_id
    editingName.value = true
  }

  async function saveName() {
    if (!selectedEntity.value) return
    const eid = selectedEntity.value.entity_id
    const alias = nameInput.value.trim()
    try {
      await fetch('/api/ha/entity-aliases', {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: eid, alias }),
      })
      entityAliases.value[eid] = alias
      // 立即更新当前实体和卡片里的显示名
      selectedEntity.value.name = alias || selectedEntity.value.attributes?.friendly_name || eid
      refreshDeviceEntityName(eid, selectedEntity.value.name)
    } catch (e) {
      console.error('Failed to save entity alias:', e)
    }
    editingName.value = false
  }

  function resetName() {
    if (!selectedEntity.value) return
    const eid = selectedEntity.value.entity_id
    const original = selectedEntity.value.attributes?.friendly_name || eid
    nameInput.value = original
  }

  // ======================== 备注 ========================

  async function loadEntityNotes() {
    try {
      const res = await fetch('/api/ha/entity-notes', { credentials: 'include' })
      const json = await res.json()
      entityNotes.value = json.data?.notes || {}
    } catch (e) {
      console.error('Failed to load entity notes:', e)
    }
  }

  function startEditNote() {
    if (!selectedEntity.value) return
    noteInput.value = entityNotes.value[selectedEntity.value.entity_id] || ''
    editingNote.value = true
  }

  async function saveNote() {
    if (!selectedEntity.value) return
    const eid = selectedEntity.value.entity_id
    const note = noteInput.value.trim()
    try {
      await fetch('/api/ha/entity-notes', {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: eid, note }),
      })
      if (note) {
        entityNotes.value[eid] = note
      } else {
        delete entityNotes.value[eid]
      }
    } catch (e) {
      console.error('Failed to save entity note:', e)
    }
    editingNote.value = false
  }

  function resetNote() {
    if (!selectedEntity.value) return
    noteInput.value = entityNotes.value[selectedEntity.value.entity_id] || ''
  }

  /** 同步更新 selectedDevice.entities 里同名实体的 name（卡片即时刷新） */
  function refreshDeviceEntityName(entityId, newName) {
    if (!selectedDevice.value) return
    const ent = (selectedDevice.value.entities || []).find(e => e.entity_id === entityId)
    if (ent) ent.name = newName
  }

  return {
    entityAliases,
    editingName,
    nameInput,
    entityNotes,
    noteInput,
    editingNote,
    loadEntityAliases,
    startEditName,
    saveName,
    resetName,
    loadEntityNotes,
    startEditNote,
    saveNote,
    resetNote,
  }
}
