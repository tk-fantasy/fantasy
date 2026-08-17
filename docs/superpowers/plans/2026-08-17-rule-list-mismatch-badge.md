# 规则列表错配徽标 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 自动化规则列表中，视觉规则未绑定摄像头标红、定时/天气规则绑定摄像头标橙，各挂一个徽标（无说明文案）。

**Architecture:** 检测函数抽到 `frontend/src/utils/ruleMismatch.js`（纯函数，便于 vitest 单测）；`TaskView.vue` 导入并在规则卡片头部渲染徽标 + 卡片配色 class。不改过滤逻辑、创建链路、后端。

**Tech Stack:** Vue 3 `<script setup>`、vitest + @vue/test-utils（jsdom）。

**Spec:** `docs/superpowers/specs/2026-08-17-rule-list-mismatch-badge-design.md`

## Global Constraints

- 检测语义与后端兜底一致：`type` 缺失/非法按 vision 处理（`rule_service.py:273-275` 同语义）
- 红级条件：`type === 'vision' && !camera_id`，徽标文案「⚠️ 视觉规则未绑定摄像头」
- 橙级条件：`(type === 'time' || type === 'weather') && camera_id`，徽标文案「💡 定时/天气规则不依赖摄像头」
- 仅徽标，**不加说明文案行**
- 不改：列表过滤（按 camera_id 显示）、创建表单、后端 API、评估引擎
- type 归一化：`String(rule.type || 'vision').toLowerCase()`

---

### Task 1: `getRuleMismatch` 检测函数（TDD）

**Files:**
- Create: `frontend/src/utils/ruleMismatch.js`
- Test: `frontend/tests/utils/ruleMismatch.test.js`

**Interfaces:**
- Produces: `getRuleMismatch(rule: object) → 'red' | 'orange' | ''`
  - `red`: vision（含 type 缺失兜底）且 `camera_id` 为空串
  - `orange`: time/weather 且 `camera_id` 非空
  - `''`: 正常规则

- [ ] **Step 1: 写失败测试**

创建 `frontend/tests/utils/ruleMismatch.test.js`：

```js
import { describe, it, expect } from 'vitest'
import { getRuleMismatch } from '../../src/utils/ruleMismatch'

describe('getRuleMismatch', () => {
  // —— 红级：全局视觉规则 ——
  it('vision + 无 camera_id → red', () => {
    expect(getRuleMismatch({ type: 'vision', camera_id: '' })).toBe('red')
  })
  it('type 缺失按 vision 兜底 + 无 camera_id → red', () => {
    expect(getRuleMismatch({ camera_id: '' })).toBe('red')
    expect(getRuleMismatch({ type: null, camera_id: '' })).toBe('red')
  })
  it('type 非法值按 vision 兜底 + 无 camera_id → red', () => {
    expect(getRuleMismatch({ type: 'unknown', camera_id: '' })).toBe('red')
  })
  it('type 大小写归一化：VISION + 无 camera_id → red', () => {
    expect(getRuleMismatch({ type: 'VISION', camera_id: '' })).toBe('red')
  })

  // —— 橙级：绑定摄像头的定时/天气规则 ——
  it('time + camera_id → orange', () => {
    expect(getRuleMismatch({ type: 'time', camera_id: 'cam1' })).toBe('orange')
  })
  it('weather + camera_id → orange', () => {
    expect(getRuleMismatch({ type: 'weather', camera_id: 'cam1' })).toBe('orange')
  })
  it('camera_id 缺失按空串处理：time → 无标记', () => {
    expect(getRuleMismatch({ type: 'time' })).toBe('')
  })

  // —— 正常规则：无标记 ——
  it('vision + camera_id → 无标记', () => {
    expect(getRuleMismatch({ type: 'vision', camera_id: 'cam1' })).toBe('')
  })
  it('time + 无 camera_id → 无标记', () => {
    expect(getRuleMismatch({ type: 'time', camera_id: '' })).toBe('')
  })
  it('weather + 无 camera_id → 无标记', () => {
    expect(getRuleMismatch({ type: 'weather', camera_id: '' })).toBe('')
  })
})
```

- [ ] **Step 2: 运行确认失败**

```bash
cd frontend && npx vitest run tests/utils/ruleMismatch.test.js
```

预期：FAIL，报 `Failed to resolve import "../../src/utils/ruleMismatch"`（模块不存在）。

- [ ] **Step 3: 写最小实现**

创建 `frontend/src/utils/ruleMismatch.js`：

```js
/**
 * 规则 type ↔ 摄像头绑定错配检测（纯函数，TaskView 卡片标色用）。
 *
 * type 缺失/非法按 vision 兜底 —— 与后端 rule_service 的落库兜底语义一致
 * （automation_service 路由同样把未知 type 归入 vision 管道评估）。
 *
 * @param {object} rule 规则对象（含 type、camera_id 字段）
 * @returns {'red' | 'orange' | ''} red=全局视觉规则（危险）；orange=绑定摄像头的定时/天气规则（无害提示）
 */
export function getRuleMismatch(rule) {
  if (!rule) return ''
  const type = String(rule.type || 'vision').toLowerCase()
  const cam = rule.camera_id || ''
  if (type === 'vision' && !cam) return 'red'
  if ((type === 'time' || type === 'weather') && cam) return 'orange'
  return ''
}
```

- [ ] **Step 4: 运行确认通过**

```bash
cd frontend && npx vitest run tests/utils/ruleMismatch.test.js
```

预期：PASS，10 个用例全绿。

- [ ] **Step 5: 提交**

```bash
git add frontend/src/utils/ruleMismatch.js frontend/tests/utils/ruleMismatch.test.js
git commit -m "feat(rules): getRuleMismatch 错配检测——全局视觉/绑定定时天气识别"
```

---

### Task 2: TaskView 卡片徽标 + 配色（TDD）

**Files:**
- Modify: `frontend/src/views/TaskView.vue`（script 区新增 import + helper 绑定；template 规则卡片头部行加徽标 span、卡片根节点加动态 class；style 区追加红/橙配色）
- Test: `frontend/tests/views/TaskView.test.js`（新建）

**Interfaces:**
- Consumes: `getRuleMismatch(rule)` from `../../src/utils/ruleMismatch`（Task 1 产出）
- Produces: DOM 结构——
  - 错配卡片根节点 class 含 `rule-card--red` 或 `rule-card--orange`（正常卡片不含）
  - 徽标 `<span class="rule-mismatch-badge rule-mismatch-badge--red|rule-mismatch-badge--orange">⚠️ 视觉规则未绑定摄像头</span>`（或 💡 橙文案），位于 `.rule-header-row` 内、摄像头标签之前；正常卡片无此节点

- [ ] **Step 1: 写失败测试**

创建 `frontend/tests/views/TaskView.test.js`（mock 方式参照 `tests/views/HAListView.test.js`：TaskView 走 `apiGet('/api/rules')`（fetch）+ `useCamera`（fetch）+ emoji prefs（fetch），统一 mock fetch 返回 `{ data: ... }`）：

```js
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import TaskView from '../../src/views/TaskView.vue'

// TaskView onMounted 拉三类数据，全走 fetch：
// /api/rules（规则列表）、emoji prefs、摄像头列表（useCamera）
function mockFetch(rules = []) {
  global.fetch = vi.fn((url) => {
    if (url === '/api/rules') {
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: rules }) })
    }
    // useCamera / emoji prefs 等其余请求兜底空数据
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ data: [] }) })
  })
}

describe('TaskView 规则错配徽标', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('全局视觉规则：卡片标红 + 红徽标', async () => {
    mockFetch([
      { id: 'r1', name: '有人比耶关研发部灯', type: 'vision', camera_id: '', enabled: true, condition: '检测到有人比个耶', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    const card = wrapper.find('.rule-card')
    expect(card.classes()).toContain('rule-card--red')
    const badge = wrapper.find('.rule-mismatch-badge')
    expect(badge.exists()).toBe(true)
    expect(badge.classes()).toContain('rule-mismatch-badge--red')
    expect(badge.text()).toBe('⚠️ 视觉规则未绑定摄像头')
  })

  it('绑定摄像头的定时规则：卡片标橙 + 橙徽标', async () => {
    mockFetch([
      { id: 'r2', name: '早八点开灯', type: 'time', camera_id: 'cam1', enabled: true, condition: '每天 8 点', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    const card = wrapper.find('.rule-card')
    expect(card.classes()).toContain('rule-card--orange')
    const badge = wrapper.find('.rule-mismatch-badge')
    expect(badge.classes()).toContain('rule-mismatch-badge--orange')
    expect(badge.text()).toBe('💡 定时/天气规则不依赖摄像头')
  })

  it('正常视觉规则（已绑定摄像头）：无徽标无配色', async () => {
    mockFetch([
      { id: 'r3', name: '比耶关灯', type: 'vision', camera_id: 'cam1', enabled: true, condition: '检测到有人比个耶', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    expect(wrapper.find('.rule-card').classes()).not.toContain('rule-card--red')
    expect(wrapper.find('.rule-card').classes()).not.toContain('rule-card--orange')
    expect(wrapper.find('.rule-mismatch-badge').exists()).toBe(false)
  })

  it('正常定时规则（全局）：无徽标无配色', async () => {
    mockFetch([
      { id: 'r4', name: '日落开客厅灯', type: 'time', camera_id: '', enabled: true, condition: '日落时', actions: [] },
    ])
    const wrapper = mount(TaskView)
    await flushPromises()
    expect(wrapper.find('.rule-mismatch-badge').exists()).toBe(false)
    expect(wrapper.find('.rule-card').classes()).not.toContain('rule-card--orange')
  })
})
```

- [ ] **Step 2: 运行确认失败**

```bash
cd frontend && npx vitest run tests/views/TaskView.test.js
```

预期：FAIL——徽标节点 `.rule-mismatch-badge` 不存在、卡片无 `rule-card--red` class。

- [ ] **Step 3: 修改 TaskView.vue**

script 区（`import { apiGet } ...` 之后加一行；`filteredRules` 定义之后加 helper）：

```js
import { getRuleMismatch } from '../utils/ruleMismatch'
```

```js
// 错配标色：red=全局视觉规则(任意摄像头触发,危险)；orange=定时/天气绑定摄像头(绑定无效,无害)
const mismatch = (rule) => getRuleMismatch(rule)
const mismatchBadgeText = {
  red: '⚠️ 视觉规则未绑定摄像头',
  orange: '💡 定时/天气规则不依赖摄像头',
}
```

template 区规则卡片根节点（`TaskView.vue:344` 附近）改为：

```html
<div
  v-for="rule in filteredRules"
  :key="rule.id"
  class="rule-card"
  :class="mismatch(rule) ? `rule-card--${mismatch(rule)}` : ''"
  @click="openRuleDetail(rule)"
>
```

卡片头部行（`.rule-header-row` 内，摄像头标签 `<span class="rule-camera-tag" ...>` 之前）插入徽标：

```html
<span
  v-if="mismatch(rule)"
  class="rule-mismatch-badge"
  :class="`rule-mismatch-badge--${mismatch(rule)}`"
>{{ mismatchBadgeText[mismatch(rule)] }}</span>
```

style 区（scoped）追加：

```css
/* 错配徽标 + 卡片配色 */
.rule-card--red {
  border-color: var(--color-danger, #e74c3c);
  background: rgba(231, 76, 60, 0.06);
}

.rule-card--orange {
  border-color: #e67e22;
  background: rgba(230, 126, 34, 0.06);
}

.rule-mismatch-badge {
  font-size: var(--text-xs);
  padding: var(--space-1) var(--space-5);
  border-radius: var(--radius-sm);
  font-weight: var(--weight-medium);
  flex-shrink: 0;
  white-space: nowrap;
}

.rule-mismatch-badge--red {
  background: rgba(231, 76, 60, 0.15);
  color: var(--color-danger, #e74c3c);
}

.rule-mismatch-badge--orange {
  background: rgba(230, 126, 34, 0.15);
  color: #e67e22;
}
```

（注：`--color-danger` 若主题已有则直接生效，兜底值 #e74c3c 保证未定义时也可用。）

- [ ] **Step 4: 运行确认通过**

```bash
cd frontend && npx vitest run tests/views/TaskView.test.js
```

预期：PASS，4 个用例全绿。

- [ ] **Step 5: 跑全量前端测试防回归**

```bash
cd frontend && npx vitest run
```

预期：全部 PASS（含既有组件/视图/工具测试）。若有既有用例失败，先确认是否本次改动引入（git stash 对照），非本次引入的失败单独上报不修。

- [ ] **Step 6: 构建验证**

```bash
cd frontend && npm run build
```

预期：vite build 成功（build 脚本内含 `sync-to-backend.js`，会把产物同步到 `app/static/frontend`）。

- [ ] **Step 7: 提交**

```bash
git add frontend/src/views/TaskView.vue frontend/tests/views/TaskView.test.js app/static/frontend
git commit -m "feat(rules): 规则列表错配徽标——全局视觉标红/绑定定时天气标橙"
```

---

## Self-Review 记录

- **Spec 覆盖**：检测语义（Task 1）、红/橙两级徽标与配色（Task 2）、「无说明文案」（徽标仅一行 span，无额外文案行）、「不改过滤/创建/后端」（两任务均未触碰）——全覆盖。
- **占位符扫描**：无 TBD/TODO；所有代码步骤含完整代码。
- **类型一致性**：`getRuleMismatch` 在 Task 1 定义、Task 2 导入，签名一致；DOM class 名测试断言与模板/CSS 三处一致（`rule-card--red|orange`、`rule-mismatch-badge--red|orange`）。
