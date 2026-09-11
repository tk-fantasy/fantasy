<script setup>
/**
 * 设备消歧选择弹窗 —— 用户指令匹配到多个设备时，让用户勾选要操作哪些。
 *
 * 触发方：聊天工具 call_service 的消歧闸门返回 status="need_selection"
 * （app/tools.py），ChatView 捕获后在**本轮 Dialog.Finish 之后**才挂载本组件。
 * 轮末才弹是硬约束不是体验取舍：dispatcher 在轮末才把 user/assistant 消息
 * append 进 model_messages，而确认端点会往同一个列表追加「我已通过界面选择…」，
 * 轮中确认会让它排在原始请求之前，下一轮模型读到的是乱序历史
 * （与 ReviseChatModal 的 pending 模式同一条约束）。
 *
 * 确认走 REST 直接执行、不再回模型：草稿里存着模型已解析好的 domain/service/data，
 * 后端 /api/ha/pending/{id}/select 用它下发，并复查 entity_operable 黑名单。
 *
 * 关闭（X / 点遮罩）**不取消**后端草稿：TTL 内用户仍可在聊天里直接说设备名走
 * 口头路径（语音渠道就是这么用的）。「取消」按钮才显式作废草稿。
 */
import { computed, ref } from 'vue'
import { apiPost } from '../utils/api'
import AdvancedModal from './AdvancedModal.vue'

const props = defineProps({
  pendingId: { type: String, required: true },
  sessionId: { type: String, default: '' },
  query: { type: String, default: '' },            // 用户原话，用于文案
  reason: { type: String, default: 'ambiguous' },  // ambiguous | category_miss
  service: { type: String, default: '' },          // turn_on / turn_off / open_cover ...
  candidates: { type: Array, default: () => [] },
})
const emit = defineEmits(['confirmed', 'cancelled', 'expired', 'close'])

// 预勾「状态会真的改变」的项，免得用户说「开灯」时把已经亮着的灯再勾一遍。
// 开/关态口径与 app/tools.py 的 _ON_STATES/_OFF_STATES 一致。
const ON_SERVICES = new Set(['turn_on', 'open_cover', 'open_valve'])
const OFF_SERVICES = new Set(['turn_off', 'close_cover', 'close_valve'])
const ON_STATES = new Set(['on', 'open', 'opening'])
const OFF_STATES = new Set(['off', 'closed', 'closing'])
// 状态不可信时无法预判，一律当作「会变」→ 预勾
const UNRELIABLE_STATES = new Set(['', 'unavailable', 'unknown', 'none'])

function wouldChange(c) {
  const state = String(c?.state ?? '').toLowerCase()
  if (UNRELIABLE_STATES.has(state)) return true
  if (ON_SERVICES.has(props.service)) return !ON_STATES.has(state)
  if (OFF_SERVICES.has(props.service)) return !OFF_STATES.has(state)
  return true // 调光/设温/播放这类无法预判，默认全勾
}

// 用对象而非 Set：v-model 直接绑 selected[entity_id]，模板与测试都更直白
const selected = ref(Object.fromEntries(
  props.candidates.filter(wouldChange).map((c) => [c.entity_id, true]),
))
const busy = ref(false)
const actionError = ref('')

const selectedIds = computed(() => props.candidates
  .filter((c) => selected.value[c.entity_id])
  .map((c) => c.entity_id))
const allSelected = computed(
  () => props.candidates.length > 0 && selectedIds.value.length === props.candidates.length)

function toggleAll() {
  selected.value = allSelected.value
    ? {}
    : Object.fromEntries(props.candidates.map((c) => [c.entity_id, true]))
}

const isCategoryMiss = computed(() => props.reason === 'category_miss')
const title = computed(() => (isCategoryMiss.value ? `没找到「${props.query}」` : '你要操作哪个设备？'))
const notice = computed(() => (isCategoryMiss.value
  ? '没有这个名字的设备。下面列出的是同类设备——如果你指的是其中一个，勾选后继续；都不是就点取消。'
  : `「${props.query}」匹配到 ${props.candidates.length} 个设备，勾选要操作的：`))

async function confirm() {
  if (busy.value || !selectedIds.value.length) return
  busy.value = true
  actionError.value = ''
  try {
    const data = await apiPost(`/api/ha/pending/${props.pendingId}/select`, {
      session_id: props.sessionId,
      entity_ids: selectedIds.value,
    })
    emit('confirmed', data)
  } catch (e) {
    // 草稿过期/后端重启（草稿不持久化）→ 让上层提示重说指令并关掉弹窗；
    // 其它失败（如设备刚被设为禁止 AI 操作）留在弹窗内，用户还能改选。
    if (e?.status === 404) {
      emit('expired', e.message || '待选设备不存在或已过期')
      return
    }
    actionError.value = e.message || String(e)
  } finally {
    busy.value = false
  }
}

async function cancel() {
  if (busy.value) return
  busy.value = true
  actionError.value = ''
  try {
    await apiPost(`/api/ha/pending/${props.pendingId}/cancel`, { session_id: props.sessionId })
    emit('cancelled')
  } catch (e) {
    if (e?.status === 404) {
      emit('expired', e.message || '待选设备不存在或已过期')
      return
    }
    actionError.value = e.message || String(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <AdvancedModal :title="title" @close="emit('close')">
    <div class="device-select">
      <p class="notice">{{ notice }}</p>

      <div class="toolbar">
        <button type="button" class="link-btn" @click="toggleAll">
          {{ allSelected ? '清空' : '全选' }}
        </button>
        <span class="count">已选 {{ selectedIds.length }} / {{ candidates.length }}</span>
      </div>

      <ul class="cand-list">
        <li v-for="c in candidates" :key="c.entity_id" class="cand"
            :class="{ checked: !!selected[c.entity_id] }">
          <label class="cand-row">
            <input type="checkbox" v-model="selected[c.entity_id]" :data-eid="c.entity_id" />
            <span class="cand-name">{{ c.label || c.name || c.entity_id }}</span>
            <span v-if="c.area_name" class="cand-area">{{ c.area_name }}</span>
            <span v-if="c.state" class="cand-state">{{ c.state }}</span>
          </label>
        </li>
      </ul>

      <div v-if="actionError" class="action-error">{{ actionError }}</div>

      <div class="footer">
        <button type="button" class="btn ghost" :disabled="busy" @click="cancel">取消</button>
        <button type="button" class="btn primary" :disabled="busy || !selectedIds.length"
                @click="confirm">
          {{ busy ? '执行中…' : `执行（${selectedIds.length}）` }}
        </button>
      </div>
    </div>
  </AdvancedModal>
</template>

<style scoped>
.device-select {
  display: flex;
  flex-direction: column;
  gap: var(--space-10);
}

.notice {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-8);
}

.link-btn {
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  font-size: var(--text-sm);
  color: var(--color-primary);
}

.link-btn:hover {
  text-decoration: underline;
}

.count {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.cand-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  max-height: 46vh;
  overflow-y: auto;
}

.cand-row {
  display: flex;
  align-items: center;
  gap: var(--space-8);
  padding: var(--space-8) var(--space-10);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-md);
  background: var(--color-surface);
  cursor: pointer;
  transition: border-color var(--duration-fast) var(--ease-out),
              background var(--duration-fast) var(--ease-out);
}

.cand-row:hover {
  background: var(--color-surface-hover);
  border-color: var(--color-border-hover);
}

.cand.checked .cand-row {
  border-color: var(--color-primary);
  background: var(--color-primary-light);
}

.cand-row input[type='checkbox'] {
  flex-shrink: 0;
  accent-color: var(--color-primary);
  cursor: pointer;
}

.cand-name {
  font-size: var(--text-sm);
  color: var(--color-text);
  font-weight: var(--weight-medium);
}

.cand-area,
.cand-state {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
}

.cand-state {
  margin-left: auto;
}

.action-error {
  font-size: var(--text-sm);
  color: var(--color-danger);
}

.footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-8);
}

.btn {
  padding: var(--space-6) var(--space-14);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  cursor: pointer;
  border: 1px solid var(--color-border);
  transition: all var(--duration-fast) var(--ease-out);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn.ghost {
  background: var(--color-surface);
  color: var(--color-text-secondary);
}

.btn.ghost:hover:not(:disabled) {
  background: var(--color-surface-hover);
}

.btn.primary {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}

.btn.primary:hover:not(:disabled) {
  background: var(--color-primary-dark);
  border-color: var(--color-primary-dark);
}
</style>
