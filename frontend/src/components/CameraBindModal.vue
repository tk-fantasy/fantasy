<script setup>
/**
 * 摄像头绑定弹窗 —— TaskView 建视觉规则时用。
 *
 * 为什么需要它：TaskView 的「当前作用范围」下拉是**打字之前**就要定的，但这条规则
 * 到底是不是视觉类型，要等后端 LLM 解析完才知道。在「全局(定时/天气)」范围下输入
 * 「有人就开灯」，解析出来是 vision + camera_id=''，也就是 ruleMismatch.js 标红的
 * 危险态（automation_service 对未绑定的规则在**所有**摄像头上评估）。
 *
 * 所以创建流程改成两段：先 POST /api/rules/preview 只解析不落库，是视觉规则就弹这个
 * 框让用户显式选一路，再 POST /api/rules 落库。
 *
 * 单一职责，不复用 ReviseChatModal —— 那个已经背了 rule/task/pending 三种形态。
 */
import { ref, computed } from 'vue'

const props = defineProps({
  cameras: { type: Array, default: () => [] },
  ruleName: { type: String, default: '' },
  condition: { type: String, default: '' },
  actionText: { type: String, default: '' },
  // TaskView header 上选的「当前作用范围」；'' = 全局(定时/天气)
  scopeCameraId: { type: String, default: '' },
})
const emit = defineEmits(['confirm', 'close'])

const enabledCameras = computed(() =>
  (props.cameras || []).filter((c) => c && c.enabled !== false))

// 没有可选摄像头时「全部摄像头」也没意义（没有画面源，规则永不触发），
// 后端 confirm 同样会挡，这里提前说清楚，别让用户点了才报错
const noCameras = computed(() => enabledCameras.value.length === 0)

// 作用范围选了「全局」但规则是视觉类型 —— 正是这个弹窗要拦的错配
const scopeMismatch = computed(() => !props.scopeCameraId)

// 只有作用范围本来就指着某一路真实摄像头时才预选；'' 不预选，逼用户显式选一次
const selected = ref(
  enabledCameras.value.some((c) => c.id === props.scopeCameraId)
    ? props.scopeCameraId
    : null,
)

function pick(id) {
  if (noCameras.value) return
  selected.value = id
}

function onConfirm() {
  if (noCameras.value || selected.value === null) return
  emit('confirm', selected.value)
}

function onKeydown(e) {
  if (e.key === 'Escape') emit('close')
}
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div class="bind-overlay" @click.self="emit('close')">
        <div class="bind-container aurora-before" @keydown="onKeydown">
          <div class="bind-header">
            <h2>选择摄像头</h2>
            <button class="btn-close" @click="emit('close')">&times;</button>
          </div>

          <div class="bind-body">
            <p class="bind-lead">
              这条规则要靠<strong>摄像头画面</strong>判断，得指定看哪一路。
            </p>

            <div v-if="ruleName || condition || actionText" class="bind-rule">
              <div v-if="ruleName" class="rule-name">{{ ruleName }}</div>
              <div v-if="condition" class="rule-row">
                <span class="rule-label">如果</span><span>{{ condition }}</span>
              </div>
              <div v-if="actionText" class="rule-row">
                <span class="rule-label">则</span><span>{{ actionText }}</span>
              </div>
            </div>

            <p v-if="scopeMismatch" class="bind-warn">
              ⚠️ 你当前的作用范围选的是「全局(定时/天气)」，但这条规则是视觉规则 ——
              不绑摄像头的话，<strong>每一路</strong>画面里出现目标都会触发它。
            </p>

            <template v-if="noCameras">
              <p class="bind-blocked">
                当前没有可用摄像头，视觉规则不会触发。请先到摄像头设置里添加并启用一路。
              </p>
            </template>
            <template v-else>
              <div class="bind-options">
                <button
                  v-for="c in enabledCameras"
                  :key="c.id"
                  type="button"
                  class="bind-chip"
                  :class="{ active: selected === c.id }"
                  @click="pick(c.id)"
                >{{ c.name || c.id }}</button>
                <button
                  type="button"
                  class="bind-chip global"
                  :class="{ active: selected === '' }"
                  @click="pick('')"
                >全部摄像头（全局）</button>
              </div>
              <p v-if="selected === ''" class="bind-hint danger">
                ⚠️ 每一路画面里出现目标都会触发这条规则。确认这是你要的。
              </p>
              <p v-else-if="selected === null" class="bind-hint">
                选一路摄像头，或显式选择「全部摄像头」。
              </p>
              <p v-else class="bind-hint">只有这一路的画面会触发这条规则。</p>
            </template>
          </div>

          <div class="bind-footer">
            <button class="btn-cancel" @click="emit('close')">取消</button>
            <button
              class="btn-confirm"
              :disabled="noCameras || selected === null"
              @click="onConfirm"
            >创建规则</button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.bind-overlay {
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

.bind-container {
  position: relative;
  isolation: isolate;
  background: var(--dialog-bg-glass);
  -webkit-backdrop-filter: blur(12px);
  backdrop-filter: blur(12px);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-3xl);
  width: 100%;
  max-width: 520px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xl);
  overflow: hidden;
}

.bind-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-10) var(--space-16);
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}
.bind-header h2 {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
  color: var(--color-text);
  margin: 0;
}
.btn-close {
  background: none;
  border: none;
  font-size: var(--text-2xl);
  color: var(--color-text-muted);
  cursor: pointer;
  line-height: 1;
  padding: 0 var(--space-2);
}
.btn-close:hover { color: var(--color-text); }

.bind-body {
  padding: var(--space-12) var(--space-16);
  overflow-y: auto;
}
.bind-lead {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  margin: 0 0 var(--space-10);
  line-height: 1.7;
}
.bind-lead strong { color: var(--color-text); }

.bind-rule {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-lg);
  padding: var(--space-8) var(--space-10);
  margin-bottom: var(--space-10);
}
.rule-name {
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--color-text);
  margin-bottom: var(--space-4);
}
.rule-row {
  display: flex;
  gap: var(--space-6);
  align-items: baseline;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  line-height: 1.7;
}
.rule-label {
  color: var(--color-text-muted);
  min-width: 32px;
  flex-shrink: 0;
}

.bind-warn {
  font-size: var(--text-xs);
  line-height: 1.7;
  color: var(--color-text-secondary);
  background: var(--color-warning-bg, rgba(255, 176, 32, 0.12));
  border: 1px solid var(--color-warning-border, rgba(255, 176, 32, 0.35));
  border-radius: var(--radius-lg);
  padding: var(--space-6) var(--space-10);
  margin: 0 0 var(--space-10);
}
.bind-warn strong { color: var(--color-text); }

.bind-blocked {
  font-size: var(--text-sm);
  color: var(--color-error, #ff6b6b);
  line-height: 1.7;
  margin: 0;
}

.bind-options {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6);
}
.bind-chip {
  padding: var(--space-5) var(--space-10);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: all var(--duration-fast) var(--ease-out);
}
.bind-chip:hover { background: var(--color-surface-hover); }
.bind-chip.active {
  background: var(--color-primary);
  border-color: var(--color-primary);
  color: #fff;
}
/* 「全部摄像头（全局）」选中时用警示色 —— 合法但危险的选择 */
.bind-chip.global.active {
  background: var(--color-warning, #b07000);
  border-color: var(--color-warning, #b07000);
}

.bind-hint {
  font-size: var(--text-xs);
  color: var(--color-text-muted);
  margin: var(--space-8) 0 0;
  line-height: 1.6;
}
.bind-hint.danger { color: var(--color-warning, #b07000); }

.bind-footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-6);
  padding: var(--space-10) var(--space-16);
  border-top: 1px solid var(--color-border);
  flex-shrink: 0;
}
.btn-cancel {
  padding: var(--space-5) var(--space-12);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
}
.btn-cancel:hover { background: var(--color-surface-hover); }
.btn-confirm {
  padding: var(--space-5) var(--space-12);
  border-radius: var(--radius-lg);
  border: none;
  background: var(--color-success, #34c759);
  color: #fff;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
}
.btn-confirm:disabled { opacity: 0.4; cursor: not-allowed; }

.modal-enter-active, .modal-leave-active { transition: opacity 0.3s var(--ease-out); }
.modal-enter-active .bind-container, .modal-leave-active .bind-container { transition: all 0.3s var(--ease-out); }
.modal-enter-from, .modal-leave-to { opacity: 0; }
.modal-enter-from .bind-container, .modal-leave-to .bind-container { transform: scale(0.95) translateY(20px); }
</style>
