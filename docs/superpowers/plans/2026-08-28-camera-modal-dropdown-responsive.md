# 摄像头预览弹窗：固定下拉切换 + 小屏滚动适配 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 摄像头预览弹窗内的多路切换器固定用 FlowSelect 下拉（不再用标签按钮）；弹窗改为「固定头部 + 滚动主体」，矮屏下画面只在画面区内缩放、其余内容滚动可达。

**Architecture:** `CameraSwitcher.vue` 删除标签分支、恒渲染 FlowSelect（对外接口不变）；`ChatView.vue` 把 stage/PTZ/统计/反馈/提示/插件面板包进新增 `.camera-modal-body`（`overflow-y:auto; min-height:0`），画面区 `min-height` 改为 `clamp(140px, 32vh, 300px)`，`.camera-feed` 的防溢出约束（`height:100% + object-fit:contain`）保持不变。

**Tech Stack:** Vue 3 `<script setup>`、Vite、Vitest + @vue/test-utils。

**对应设计:** `docs/superpowers/specs/2026-08-28-camera-modal-dropdown-responsive-design.md`

## Global Constraints

- 组件对外 props/events 不变：`cameras: Array` / `modelValue: String` / emit `change(id)`，`ChatView.vue:819` 的引用不调整。
- 单路（`cameras.length <= 1`）仍不渲染切换器。
- `.camera-modal` 保持 `max-height: 90vh; overflow: hidden`（hidden 仅做圆角裁切），滚动只发生在 `.camera-modal-body`。
- `.camera-feed` 保持 `width:100%; height:100%; max-height:400px; object-fit:contain`，不得回退成固有尺寸。
- 注释风格：中文、说明约束原因；文案全部中文。
- 提交信息用仓库既有风格（`feat:` / `fix:` / `refactor:` + 中文摘要）。

---

### Task 1: CameraSwitcher 固定下拉（TDD）

**Files:**
- Modify: `frontend/src/components/CameraSwitcher.vue`
- Test: `frontend/tests/components/CameraSwitcher.test.js`

**Interfaces:**
- Consumes: `FlowSelect`（`modelValue` / `options:[{value,label}]` / `change`，已存在于 `frontend/src/components/FlowSelect.vue`）。
- Produces: `CameraSwitcher` 组件，props `cameras`/`modelValue`，emit `change(cameraId)`——Task 2 依赖其在 ChatView 中的现有引用不变化。

- [ ] **Step 1: 重写测试为纯下拉行为**

用下面内容整体替换 `frontend/tests/components/CameraSwitcher.test.js`：

```js
import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import CameraSwitcher from '../../src/components/CameraSwitcher.vue'

const cams = (n) => Array.from({ length: n }, (_, i) => ({ id: `cam_${i}`, name: `摄像头${i}` }))

describe('CameraSwitcher', () => {
  it('单路时不渲染切换器(无切换可言)', () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(1), modelValue: 'cam_0' } })
    expect(w.find('.camera-switcher').exists()).toBe(false)
  })

  it('多路恒用下拉:触发器展示当前路 label,选中新路 emit change 并收起', async () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(3), modelValue: 'cam_0' } })
    expect(w.findAll('.camera-tab').length).toBe(0)
    expect(w.find('.flow-select').exists()).toBe(true)
    expect(w.find('.trigger-text').text()).toBe('摄像头0')

    await w.find('.trigger').trigger('click')
    const opt = w.findAll('.dropdown .option').find(d => d.text() === '摄像头2')
    await opt.trigger('click')

    expect(w.emitted('change')?.at(-1)).toEqual(['cam_2'])
    expect(w.find('.dropdown').exists()).toBe(false)
  })

  it('重选当前路不 emit(切路是单例切换,同路无动作)', async () => {
    const w = mount(CameraSwitcher, { props: { cameras: cams(2), modelValue: 'cam_0' } })
    await w.find('.trigger').trigger('click')
    const opt = w.findAll('.dropdown .option').find(d => d.text() === '摄像头0')
    await opt.trigger('click')

    expect(w.emitted('change')).toBeUndefined()
  })
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontend && npx vitest run tests/components/CameraSwitcher.test.js`
Expected: FAIL——3 路用例中断言 `.camera-tab` 数量为 0 失败（当前实现 3 路 ≤ `MAX_TABS=4` 渲染的是标签按钮）。

- [ ] **Step 3: 实现——删除标签分支，恒渲染 FlowSelect**

用下面内容整体替换 `frontend/src/components/CameraSwitcher.vue`：

```vue
<script setup>
/**
 * 摄像头预览弹窗的多路切换器(Task 12,D4 AI 预览单例)。
 * 恒用 FlowSelect 下拉:标签按钮在窄屏/多路时会换行,把弹窗撑得忽高忽低;
 * 下拉高度恒定,弹窗尺寸稳定。
 */
import { computed } from 'vue'
import FlowSelect from './FlowSelect.vue'

const props = defineProps({
  cameras: { type: Array, default: () => [] },
  modelValue: { type: String, default: '' },
})
const emit = defineEmits(['change'])

const options = computed(() =>
  props.cameras.map(c => ({ value: c.id, label: c.name || c.id })))

function onSelect(id) {
  if (id !== props.modelValue) emit('change', id)
}
</script>

<template>
  <div v-if="cameras.length > 1" class="camera-switcher">
    <FlowSelect
      :modelValue="modelValue"
      :options="options"
      @change="onSelect"
    />
  </div>
</template>

<style scoped>
.camera-switcher {
  display: flex;
  padding: var(--space-6) var(--space-16) 0;
}
</style>
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd frontend && npx vitest run tests/components/CameraSwitcher.test.js`
Expected: PASS（3 个用例全绿）。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/CameraSwitcher.vue frontend/tests/components/CameraSwitcher.test.js
git commit -m "refactor: 摄像头弹窗切换器恒用下拉列表,移除标签按钮分支"
```

---

### Task 2: ChatView 弹窗「固定头部 + 滚动主体」+ 文案

**Files:**
- Modify: `frontend/src/views/ChatView.vue`（模板约 804-906 行、样式约 1491-1590 行）

**Interfaces:**
- Consumes: Task 1 产出的 `CameraSwitcher`（引用位置从 stage 上方保持在滚动区外，props/events 不变）。
- Produces: `.camera-modal-body` 滚动容器（无脚本接口，仅 DOM 结构 + 样式）。

- [ ] **Step 1: 模板——把 stage 及以下包进 `.camera-modal-body`**

在 `frontend/src/views/ChatView.vue` 的 Camera Modal 模板中，把 `<div class="camera-stage">` 起到 `<PluginSlot ... />`（含）的全部内容包进新容器，使结构变为：

```html
<div v-if="showCamera" class="camera-modal-overlay" @click.self="closeCamera">
  <div class="camera-modal">
    <div class="camera-modal-header">
      <!-- 内容保持不变 -->
    </div>
    <CameraSwitcher :cameras="cameras" :modelValue="activeCameraId" @change="switchCameraRoute" />
    <div class="camera-modal-body">
      <div class="camera-stage">
        <!-- img 与断连 UI,内容保持不变 -->
      </div>
      <div v-if="ptzEnabled" class="ptz-panel">
        <!-- 内容保持不变 -->
      </div>
      <div class="camera-stats">
        <!-- 内容保持不变 -->
      </div>
      <div class="camera-feedback">
        <!-- 内容保持不变 -->
      </div>
      <div class="camera-hint">
        💡 当前预览的摄像头即 AI 对话中 vision_chat 工具默认使用的摄像头。切换上方的下拉列表可改变 AI 看哪路。
      </div>
      <PluginSlot v-if="activeCameraIsVirtual" slot="camera_preview_panel" />
    </div>
  </div>
</div>
```

同时把 `.camera-hint` 里的提示文案由「切换上方的标签或下拉列表」改为「切换上方的下拉列表」（上面代码块已含）。

- [ ] **Step 2: 样式——新增 body 滚动容器、画面区弹性最小高**

在 `frontend/src/views/ChatView.vue` 的 scoped style 中：

1. 在 `.camera-modal` 规则后新增：

```css
/* 固定头部+滚动主体:矮视口下弹窗内容超高时在 body 内滚动,
   而不是被 modal 的 overflow:hidden 直接裁掉不可达。min-height:0
   是 flex 列布局里允许子项收缩、滚动生效的前提。 */
.camera-modal-body {
  overflow-y: auto;
  min-height: 0;
}
```

2. `.camera-stage` 的 `min-height: 300px` 改为：

```css
  min-height: clamp(140px, 32vh, 300px);
```

3. `.camera-switcher` 已随 Task 1 不再需要 `flex-wrap`（该样式在 CameraSwitcher.vue 内，此处无操作）。

`.camera-feed` 的 `width/height/max-height/object-fit` 保持原样，禁止改动。

- [ ] **Step 3: 全量单测回归**

Run: `cd frontend && npm run test`
Expected: 全部 PASS（含 ChatView.test.js——其无 camera 相关断言，应不受影响）。

- [ ] **Step 4: 提交**

```bash
git add frontend/src/views/ChatView.vue
git commit -m "fix: 摄像头弹窗固定头部+滚动主体,矮屏画面区随视口收缩不遮挡"
```

---

### Task 3: 构建 + 浏览器多视口验证

**Files:**
- Build: `frontend/dist/` → `app/static/frontend/`（由 `npm run build` 内的 `scripts/sync-to-backend.js` 自动同步，产物不入库则按 `.gitignore` 现状处理）

**Interfaces:**
- Consumes: Task 1/2 的全部改动。

- [ ] **Step 1: 构建**

Run: `cd frontend && npm run build`
Expected: vite 构建成功，脚本把 dist 同步到 `app/static/frontend/`。

- [ ] **Step 2: 浏览器验证三种视口**

启动前端（vite dev 或走后端静态页），进入聊天页，输入 `/camera` 打开预览弹窗，分别在约 1280×800、900×600、700×450 视口下检查：
- 切换器为下拉（无标签按钮），展开选择另一路生效并收起；
- 视频画面（或断连占位）始终在画面区内，没有盖住下拉切换器或标题；
- 矮视口下弹窗主体出现滚动条，统计/识别反馈/提示都能滚到看到，无内容被裁死；
- 关闭弹窗正常。

若发现布局问题：修复后重跑 `npm run test` 与 `npm run build`，再验证。

- [ ] **Step 3: 收尾提交（如有修复）**

```bash
git add -A frontend/src frontend/tests
git commit -m "fix: 摄像头弹窗小屏验证修复"
```
（无修复则跳过本步。）
