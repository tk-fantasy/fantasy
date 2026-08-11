<script setup>
/**
 * 摄像头管理页(Task 11)。
 *
 * 卡片列表 + 编辑弹窗,字段对应 cameras 表列(spec §3.1)。
 * AI 预览单例切换走 display/enable|disable(D4);试连走 test-stream;
 * 关注项 per-camera;ONVIF 发现 per-camera。区域下拉来自 /api/ha/areas。
 */
import { ref, onMounted, computed } from 'vue'
import { useCamera } from '../composables/useCamera'
import BaseToggle from '../components/BaseToggle.vue'
import FlowSelect from '../components/FlowSelect.vue'

const {
  cameras, areas, loading,
  loadCameras, loadAreas, createCamera, updateCamera, deleteCamera,
  testStream, enableDisplay, disableDisplay,
  loadFocuses, addFocus, updateFocus, deleteFocus, findDevice, manualIp,
} = useCamera()

// —— 编辑态 ——
const editing = ref(null)        // 当前编辑对象(null=列表态)
const editingFocuses = ref([])   // 当前编辑摄像头的关注项
const newFocusText = ref('')
const saving = ref(false)
const testing = ref(false)
const testResult = ref(null)     // null | { ok, error }
const discovering = ref(false)
const discoverResult = ref(null) // null | { new_ip }

// 默认新摄像头(cameras 表列默认值,见 database.py DDL)
function blankCamera() {
  return {
    name: '', enabled: 1, sort_order: 0, source_type: 'rtsp',
    usb_index: 0, rtsp_url: '', rtsp_username: '', rtsp_password: '',
    area: '', device_mac: '', discovery_enabled: 1,
    ptz_enabled: 0, ptz_ip: '', ptz_port: 80, ptz_username: '', ptz_password: '',
    ptz_speed: 0.5, ptz_step_ms: 300,
    motion_hash_size: 16, motion_threshold: 15, motion_check_interval: 1.0,
    vision_min_infer_interval: 8.0, vision_max_idle_interval: 120.0,
    vision_use_img_count: 3, frame_interval_ms: 2000, display_enabled: 1,
  }
}

onMounted(async () => {
  await Promise.all([loadCameras(), loadAreas()])
})

function startCreate() {
  editing.value = blankCamera()
  editingFocuses.value = []
  newFocusText.value = ''
  testResult.value = null
  discoverResult.value = null
}

async function startEdit(cam) {
  // 拷贝一份,避免直接改列表数据
  editing.value = { ...cam }
  testResult.value = null
  discoverResult.value = null
  try {
    editingFocuses.value = await loadFocuses(cam.id) || []
  } catch (e) {
    console.error('loadFocuses failed:', e)
    editingFocuses.value = []
  }
}

function cancelEdit() {
  editing.value = null
  editingFocuses.value = []
}

async function save() {
  if (!editing.value.name.trim()) {
    alert('请填写摄像头名称')
    return
  }
  saving.value = true
  try {
    if (editing.value.id) {
      // 更新:rtsp_password/ptz_password 留空表示不改,后端 cameras_update 直接覆盖
      // 前端若留空则不传密码字段(避免清空已有密码)
      const fields = { ...editing.value }
      if (!fields.rtsp_password) delete fields.rtsp_password
      if (!fields.ptz_password) delete fields.ptz_password
      delete fields.created_at
      delete fields.updated_at
      await updateCamera(editing.value.id, fields)
    } else {
      await createCamera(editing.value)
    }
    editing.value = null
  } catch (e) {
    console.error('save failed:', e)
    alert('保存失败: ' + (e?.message || String(e)))
  } finally {
    saving.value = false
  }
}

async function remove(id) {
  if (!confirm('删除该摄像头?关联规则将解绑,关注项将删除。')) return
  try {
    await deleteCamera(id)
  } catch (e) {
    console.error('delete failed:', e)
    alert('删除失败: ' + (e?.message || String(e)))
  }
}

async function tryTestStream() {
  testing.value = true
  testResult.value = null
  try {
    const res = await testStream(editing.value.id || 'preview', {
      rtsp_url: editing.value.rtsp_url,
      rtsp_username: editing.value.rtsp_username,
      rtsp_password: editing.value.rtsp_password,
    })
    testResult.value = res
  } catch (e) {
    testResult.value = { ok: false, error: String(e) }
  } finally {
    testing.value = false
  }
}

// AI 预览单例切换(D4):列表态直接切,切到新的会自动停旧路
async function toggleDisplay(cam) {
  // optimistic:立即视觉切换,失败再回滚
  cam.display_enabled = cam.display_enabled ? 0 : 1
  try {
    if (cam.display_enabled) {
      await enableDisplay(cam.id)
    } else {
      await disableDisplay(cam.id)
    }
    await loadCameras()
  } catch (e) {
    cam.display_enabled = cam.display_enabled ? 0 : 1  // 回滚
    console.error('toggleDisplay failed:', e)
    alert('切换预览失败: ' + (e?.message || String(e)))
  }
}

async function toggleEnabled(cam) {
  // optimistic:立即视觉切换,失败再回滚
  cam.enabled = cam.enabled ? 0 : 1
  try {
    await updateCamera(cam.id, { enabled: cam.enabled })
    await loadCameras()
  } catch (e) {
    cam.enabled = cam.enabled ? 0 : 1  // 回滚
    console.error('toggleEnabled failed:', e)
    alert('切换启用失败: ' + (e?.message || String(e)))
  }
}

// 关注项(per-camera)
async function doAddFocus() {
  const text = newFocusText.value.trim()
  if (!text || !editing.value?.id) return
  try {
    const f = await addFocus(editing.value.id, text)
    editingFocuses.value.push(f)
    newFocusText.value = ''
  } catch (e) {
    console.error('addFocus failed:', e)
  }
}

async function doDeleteFocus(fid) {
  try {
    await deleteFocus(editing.value.id, fid)
    editingFocuses.value = editingFocuses.value.filter(f => f.id !== fid)
  } catch (e) {
    console.error('deleteFocus failed:', e)
  }
}

async function doToggleFocus(fid) {
  const focus = editingFocuses.value.find(f => f.id === fid)
  if (!focus) return
  const newVal = !focus.enabled
  try {
    await updateFocus(editing.value.id, fid, { enabled: newVal })
    focus.enabled = newVal
  } catch (e) {
    console.error('toggleFocus failed:', e)
  }
}

// ONVIF 发现
async function doFindDevice() {
  if (!editing.value?.id) return
  discovering.value = true
  discoverResult.value = null
  try {
    discoverResult.value = await findDevice(editing.value.id)
    await loadCameras()
    // 刷新当前编辑对象的 device_mac/ip
    const fresh = cameras.value.find(c => c.id === editing.value.id)
    if (fresh) {
      editing.value.device_mac = fresh.device_mac
      if (fresh.ptz_ip && !editing.value.ptz_ip) editing.value.ptz_ip = fresh.ptz_ip
    }
  } catch (e) {
    discoverResult.value = { new_ip: '', error: String(e) }
  } finally {
    discovering.value = false
  }
}

async function doManualIp() {
  if (!editing.value?.id) return
  const ip = prompt('输入摄像头 IP 地址:', editing.value.ptz_ip || '')
  if (!ip) return
  try {
    await manualIp(editing.value.id, ip)
    editing.value.ptz_ip = ip
    await loadCameras()
  } catch (e) {
    alert('设置 IP 失败: ' + (e?.message || String(e)))
  }
}

const isEdit = computed(() => !!editing.value?.id)
const sourceOptions = [
  { value: 'rtsp', label: 'RTSP 网络摄像头' },
  { value: 'usb', label: 'USB 本地摄像头' },
]
const areaOptions = computed(() => [
  { value: '', label: '未分配' },
  ...areas.value.map(a => ({ value: a.name, label: a.name })),
])
</script>

<template>
  <div class="page">
    <header class="page-header page-header--split">
      <div>
        <h1>摄像头管理</h1>
        <p class="page-sub">{{ cameras.length }} 路摄像头 · AI 预览同一时刻仅 1 路(D4)</p>
      </div>
      <button class="btn-add" @click="startCreate">+ 添加摄像头</button>
    </header>

    <div v-if="loading" class="loading-state">加载中...</div>

    <div v-else class="cameras-list">
      <div v-for="cam in cameras" :key="cam.id" class="cam-card">
        <div class="cam-card-main">
          <div class="cam-card-header">
            <h3>{{ cam.name || cam.id }}</h3>
            <span class="cam-badge" :class="{ on: cam.display_enabled }">
              {{ cam.display_enabled ? 'AI 预览' : '休眠' }}
            </span>
            <span class="cam-source">{{ cam.source_type === 'usb' ? 'USB' : 'RTSP' }}</span>
          </div>
          <div class="cam-card-meta">
            <span v-if="cam.area">📍 {{ cam.area }}</span>
            <span v-if="cam.device_mac">MAC {{ cam.device_mac }}</span>
            <span v-if="cam.source_type === 'rtsp'">{{ cam.rtsp_url }}</span>
            <span v-else>USB #{{ cam.usb_index }}</span>
          </div>
        </div>
        <div class="cam-card-actions">
          <div class="cam-toggle">
            <span class="cam-toggle-label">启用</span>
            <BaseToggle :modelValue="!!cam.enabled" @update:modelValue="toggleEnabled(cam)" />
          </div>
          <div class="cam-toggle">
            <span class="cam-toggle-label">预览</span>
            <BaseToggle :modelValue="!!cam.display_enabled" @update:modelValue="toggleDisplay(cam)" />
          </div>
          <button class="btn-config" @click="startEdit(cam)">配置</button>
          <button class="btn-del" @click="remove(cam.id)">删除</button>
        </div>
      </div>

      <div v-if="cameras.length === 0" class="empty-state empty-state--card">
        暂无摄像头,点击右上角添加第一路
      </div>
    </div>

    <!-- 编辑/新建弹窗 -->
    <Teleport to="body">
      <Transition name="modal">
        <div v-if="editing" class="cam-modal-overlay" @click.self="cancelEdit">
          <div class="cam-modal aurora-before">
            <div class="cam-modal-header">
              <h2>{{ isEdit ? '编辑摄像头' : '添加摄像头' }}</h2>
              <button class="modal-close" @click="cancelEdit">关闭</button>
            </div>
            <div class="cam-modal-body">
              <!-- 基本信息 -->
              <section class="cam-section">
                <h3 class="cam-section-title">基本信息</h3>
                <div class="cam-field">
                  <label>名称</label>
                  <input v-model="editing.name" class="cam-input" placeholder="如:客厅、门口" />
                </div>
                <div class="cam-field">
                  <label>区域</label>
                  <FlowSelect v-model="editing.area" :options="areaOptions" width="100%" />
                </div>
                <div class="cam-field">
                  <label>来源</label>
                  <FlowSelect v-model="editing.source_type" :options="sourceOptions" width="100%" />
                </div>
                <div class="cam-field-row">
                  <label class="cam-check">
                    <input type="checkbox" v-model="editing.enabled" :true-value="1" :false-value="0" /> 启用采集
                  </label>
                  <label class="cam-check">
                    <input type="checkbox" v-model="editing.display_enabled" :true-value="1" :false-value="0" /> AI 预览
                  </label>
                </div>
              </section>

              <!-- RTSP -->
              <section v-if="editing.source_type === 'rtsp'" class="cam-section">
                <h3 class="cam-section-title">RTSP 配置</h3>
                <div class="cam-field">
                  <label>RTSP 地址</label>
                  <input v-model="editing.rtsp_url" class="cam-input" placeholder="rtsp://192.168.1.100:554/stream" />
                </div>
                <div class="cam-field">
                  <label>用户名</label>
                  <input v-model="editing.rtsp_username" class="cam-input" placeholder="admin" />
                </div>
                <div class="cam-field">
                  <label>密码</label>
                  <input v-model="editing.rtsp_password" type="password" class="cam-input" :placeholder="isEdit ? '留空不改' : '摄像头密码'" />
                </div>
                <div class="cam-field test-row">
                  <button class="btn-test" :disabled="testing || !editing.rtsp_url" @click="tryTestStream">
                    {{ testing ? '测试中...' : '试连' }}
                  </button>
                  <span v-if="testResult?.ok" class="test-ok">✅ 连接成功</span>
                  <span v-else-if="testResult && !testResult.ok" class="test-fail">❌ {{ testResult.error }}</span>
                </div>
              </section>

              <!-- USB -->
              <section v-else class="cam-section">
                <h3 class="cam-section-title">USB 配置</h3>
                <div class="cam-field">
                  <label>设备序号</label>
                  <input v-model.number="editing.usb_index" type="number" class="cam-input narrow" placeholder="0" />
                </div>
              </section>

              <!-- PTZ 云台 -->
              <section class="cam-section">
                <h3 class="cam-section-title">云台(PTZ)</h3>
                <label class="cam-check">
                  <input type="checkbox" v-model="editing.ptz_enabled" :true-value="1" :false-value="0" /> 启用云台
                </label>
                <div class="cam-field-row">
                  <div class="cam-field">
                    <label>IP</label>
                    <input v-model="editing.ptz_ip" class="cam-input" placeholder="192.168.1.100" />
                  </div>
                  <div class="cam-field">
                    <label>端口</label>
                    <input v-model.number="editing.ptz_port" type="number" class="cam-input narrow" />
                  </div>
                </div>
                <div class="cam-field-row">
                  <div class="cam-field">
                    <label>用户名</label>
                    <input v-model="editing.ptz_username" class="cam-input" placeholder="admin" />
                  </div>
                  <div class="cam-field">
                    <label>密码</label>
                    <input v-model="editing.ptz_password" type="password" class="cam-input" :placeholder="isEdit ? '留空不改' : 'ONVIF 密码'" />
                  </div>
                </div>
              </section>

              <!-- 关注项 -->
              <section v-if="isEdit" class="cam-section">
                <h3 class="cam-section-title">视觉关注项</h3>
                <div class="focus-add-row">
                  <input v-model="newFocusText" class="cam-input" placeholder="关注什么?如:是否有人、是否有快递" @keydown.enter="doAddFocus" />
                  <button class="btn-test" @click="doAddFocus">添加</button>
                </div>
                <div class="focus-list">
                  <div v-for="f in editingFocuses" :key="f.id" class="focus-item">
                    <BaseToggle :modelValue="f.enabled !== false" @update:modelValue="doToggleFocus(f.id)" />
                    <span :class="{ 'focus-disabled': f.enabled === false }">{{ f.text }}</span>
                    <button class="btn-del-sm" @click="doDeleteFocus(f.id)">✕</button>
                  </div>
                  <div v-if="editingFocuses.length === 0" class="focus-empty">暂无关注项,请添加</div>
                </div>
              </section>
            </div>

            <div class="cam-modal-footer">
              <button class="btn-cancel" @click="cancelEdit">取消</button>
              <button class="btn-save" :disabled="saving" @click="save">
                {{ saving ? '保存中...' : '保存' }}
              </button>
            </div>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<style scoped>
.cameras-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.cam-card {
  background: var(--color-surface);
  border-radius: var(--radius-2xl);
  padding: var(--space-14);
  border: 1px solid var(--color-border);
  display: flex;
  gap: var(--space-10);
  align-items: center;
  transition: all var(--duration-normal) var(--ease-out);
}

.cam-card:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-active);
}

.cam-card-main {
  flex: 1;
  min-width: 0;
}

.cam-card-header {
  display: flex;
  align-items: center;
  gap: var(--space-5);
  margin-bottom: var(--space-3);
}

.cam-card-header h3 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
}

.cam-badge {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-sm);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text-tertiary);
}

.cam-badge.on {
  background: var(--color-primary-light);
  color: var(--color-primary);
}

.cam-source {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.cam-card-meta {
  display: flex;
  gap: var(--space-10);
  flex-wrap: wrap;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.cam-card-actions {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  flex-shrink: 0;
}

.cam-toggle {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-1);
}

.cam-toggle-label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.btn-config,
.btn-del {
  padding: var(--space-3) var(--space-10);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  cursor: pointer;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  transition: all var(--duration-fast);
}

.btn-config:hover {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-del:hover {
  border-color: rgba(231, 76, 60, 0.3);
  color: var(--color-danger);
  background: var(--color-danger-bg);
}

/* —— 弹窗 —— */
.cam-modal-overlay {
  position: fixed;
  inset: 0;
  background: var(--overlay-bg);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  padding: var(--space-12);
}

.cam-modal {
  position: relative;
  isolation: isolate;
  background: var(--dialog-bg-glass);
  -webkit-backdrop-filter: blur(12px);
  backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-3xl);
  width: 100%;
  max-width: 640px;
  max-height: 90vh;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xl);
}

.cam-modal-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-14) var(--space-16);
  border-bottom: 1px solid var(--color-border);
}

.cam-modal-header h2 {
  font-size: var(--text-xl);
  font-weight: var(--weight-semibold);
}

.modal-close {
  background: none;
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-8);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: var(--text-sm);
}

.cam-modal-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--space-16);
  display: flex;
  flex-direction: column;
  gap: var(--space-16);
}

.cam-section {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.cam-section-title {
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  color: var(--color-primary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.cam-field {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.cam-field label {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
}

.cam-input {
  padding: var(--space-4) var(--space-8);
  border: 1px solid var(--color-border-hover);
  border-radius: var(--radius-md);
  background: rgba(255, 255, 255, 0.04);
  color: var(--color-text);
  font-size: var(--text-sm);
  font-family: inherit;
  outline: none;
  transition: border-color var(--duration-normal);
  box-sizing: border-box;
  width: 100%;
}

.cam-input:focus {
  border-color: var(--color-primary);
}

.cam-input.narrow {
  max-width: 140px;
}

.cam-input[readonly] {
  opacity: 0.6;
  cursor: default;
}

.cam-field-row {
  display: flex;
  gap: var(--space-10);
}

.cam-field-row .cam-field {
  flex: 1;
}

.cam-check {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.test-row {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  flex-direction: row !important;
}

.btn-test {
  background: var(--color-primary-light);
  color: var(--color-primary);
  border: 1px solid rgba(74, 124, 112, 0.25);
  padding: var(--space-3) var(--space-12);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
  white-space: nowrap;
}

.btn-test:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.test-ok { color: var(--color-success); font-size: var(--text-xs); }
.test-fail { color: var(--color-danger); font-size: var(--text-xs); }

.focus-add-row {
  display: flex;
  gap: var(--space-6);
}

.focus-add-row .cam-input {
  flex: 1;
}

.focus-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.focus-item {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  justify-content: space-between;
  padding: var(--space-4) var(--space-8);
  background: rgba(255, 255, 255, 0.02);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.focus-item span {
  flex: 1;
}

.focus-disabled {
  opacity: 0.4;
  text-decoration: line-through;
}

.btn-del-sm {
  background: none;
  border: none;
  color: var(--color-text-muted);
  cursor: pointer;
  font-size: var(--text-sm);
}

.btn-del-sm:hover {
  color: var(--color-danger);
}

.focus-empty {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  padding: var(--space-4);
}

.cam-modal-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-6);
  padding: var(--space-14) var(--space-16);
  border-top: 1px solid var(--color-border);
}

.btn-cancel {
  padding: var(--space-5) var(--space-16);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-border);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}

.btn-save {
  padding: var(--space-5) var(--space-16);
  border-radius: var(--radius-md);
  border: none;
  background: var(--color-primary);
  color: #fff;
  font-size: var(--text-sm);
  font-weight: var(--weight-semibold);
  cursor: pointer;
}

.btn-save:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.2s ease;
}

.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}

@media (max-width: 768px) {
  .cam-card {
    flex-direction: column;
    align-items: stretch;
  }

  .cam-card-actions {
    flex-wrap: wrap;
    justify-content: center;
  }

  .cam-field-row {
    flex-direction: column;
  }

  .cam-input.narrow {
    max-width: 100%;
  }
}
</style>
