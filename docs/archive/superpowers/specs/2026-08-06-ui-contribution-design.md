# Aether 前端 UI 贡献机制 设计文档

**日期**: 2026-08-06
**状态**: 待评审
**作者**: brainstorming 协作产出

---

## 1. 背景与动机

### 1.1 问题

Phase 1 实现了后端插件系统（子进程隔离 + manifest + SDK），但**前端 UI 仍硬编码**。具体表现：小爱广播的开关按钮直接写在 `ChatView.vue` 里，导致：

- 没装小爱插件时，`/chat` 仍显示喇叭按钮（八竿子打不着）
- 小爱的 UI 逻辑散落在主代码，违反"插件所有东西在一个文件夹"的解耦原则
- 加新插件要改 ChatView 才能加新 UI 元素

已回滚 `ChatView` 的硬编码按钮（commit `7bc48d1`），现状是 `/chat` 干净、无音媒体 UI。本设计让插件 UI 以**正确的方式**回来。

### 1.2 目标

- 插件在 `manifest.json` 声明它要贡献的 UI 元素（不写 Vue 代码）
- Aether 通用渲染器按声明渲染
- 没装插件 → 没有 UI 元素 → 主代码八竿子打不着
- 插件所有东西（后端逻辑 + UI 声明）在 `integrations/<plugin>/` 一个文件夹

---

## 2. 决策记录（brainstorming 已确认）

| 决策项 | 选择 | 理由 |
|--------|------|------|
| UI 类型范围 | **预定义类型**（toggle_button / icon_button / status_badge） | 插件声明意图，Aether 通用组件渲染，不写 Vue 代码 |
| 状态/动作协议 | **Aether 通用路由** | 插件后端不动，声明 state_key + action，Aether 提供 GET state / POST action 通用路由 |
| 渲染机制 | **Vue `<IntegrationSlot>` 组件** | ChatView 放占位 slot，渲染器读贡献列表动态渲染 |

---

## 3. 架构

### 3.1 数据流

```
插件 manifest 声明 ui_contribution
       │
       ▼
GET /api/integrations/ui_contributions  ← Aether 扫描所有插件，合并返回
       │
       ▼
<IntegrationSlot slot="chat_input_toolbar" />  ← ChatView 占位组件
       │
       ▼
按 type 渲染通用组件（toggle_button 等）
       │
   ├─ 读状态：GET /api/integrations/state/{state_key}
   └─ 触发动作：POST /api/integrations/action/{action}
       │
       ▼
Aether 通用路由 → IntegrationLayer / SinkManager（不认得小爱）
```

### 3.2 关键洞察：state/action 归属框架，不属插件

广播开关是 `SinkManager.broadcast_enabled`——**这是插件系统框架的通用状态，不属于小爱插件**。小爱插件只是"声明：我要在 UI 上放个按钮，控制广播开关"。小爱插件连开关逻辑都不持有。

这强化了三层解耦：
- **小爱插件**：只声明 UI 意图（"在 chat_input_toolbar 放个 toggle_button，控制 broadcast 开关"）
- **Aether 框架**：持有 state/action 实现（SinkManager 的开关）
- **Aether 前端**：通用渲染器 + 通用路由

### 3.3 层级职责

| 层 | 职责 | 是否认得小爱 |
|----|------|------------|
| 小爱插件 manifest | 声明 ui_contribution（slot/type/state/action） | 是（自己的） |
| Aether `ui_contribution_loader` | 扫描所有插件，合并 ui_contribution 列表 | 否 |
| Aether `integration_routes` | GET state / POST action 通用路由，按 key 路由到框架能力 | 否 |
| Aether `IntegrationSlot.vue` | 按 type 渲染通用组件，读 state，触发 action | 否 |
| `ChatView.vue` | 放 `<IntegrationSlot slot="chat_input_toolbar" />` 占位 | 否 |

---

## 4. 详细设计

### 4.1 manifest ui_contribution 规范

```json
{
  "id": "xiaoai",
  "ui_contributions": [
    {
      "slot": "chat_input_toolbar",
      "type": "toggle_button",
      "props": {
        "icon_on": "🔊",
        "icon_off": "🔇",
        "title_on": "小爱广播已开启（点击关闭）",
        "title_off": "小爱广播已关闭（点击开启）"
      },
      "state_key": "broadcast_enabled",
      "action": "toggle_broadcast"
    }
  ]
}
```

字段说明：
- `slot`：UI 槽位标识（如 `chat_input_toolbar`）。Aether 前端在对应位置放 `<IntegrationSlot>`，贡献元素渲染到这里
- `type`：预定义 UI 类型。V1 支持：
  - `toggle_button`：开/关切换按钮（如广播开关）
  - `icon_button`：单次动作图标按钮（如"触发播报"）
  - `status_badge`：只读状态徽标（如"插件在线"）
- `props`：UI 展示参数（icon/title/label 等），按 type 不同
- `state_key`：状态从哪个 key 读（POST `/api/integrations/state/{state_key}`）。toggle_button 必填，icon_button 可选
- `action`：点击触发的 action 名（POST `/api/integrations/action/{action}`）。toggle_button/icon_button 必填

### 4.2 后端：ui_contribution 加载 + 通用路由

#### 4.2.1 ui_contribution schema 扩展

`app/integration/schema.py` 的 `Manifest` 加字段：
```python
class UIContribution(BaseModel):
    slot: str
    type: str  # "toggle_button" | "icon_button" | "status_badge"
    props: dict[str, Any] = Field(default_factory=dict)
    state_key: str = ""
    action: str = ""

class Manifest(BaseModel):
    # ... 现有字段
    ui_contributions: list[UIContribution] = Field(default_factory=list)
```

#### 4.2.2 ui_contribution 加载

`IntegrationLayer.list_ui_contributions()` 扫描所有运行中插件，合并返回 ui_contribution 列表（每个带 plugin_id）：

```python
def list_ui_contributions(self) -> list[dict]:
    result = []
    for manifest in load_manifests(self._plugin_dir, ...):
        for ui in manifest.ui_contributions:
            result.append({
                "plugin_id": manifest.id,
                "slot": ui.slot,
                "type": ui.type,
                "props": ui.props,
                "state_key": ui.state_key,
                "action": ui.action,
            })
    return result
```

#### 4.2.3 通用路由

`integration_routes.py` 加：

```python
@router.get("/integrations/ui_contributions")
async def list_ui_contributions(container=Depends(get_container)):
    layer = container.integration_layer
    if layer is None:
        return {"success": True, "data": []}
    return {"success": True, "data": layer.list_ui_contributions()}

@router.get("/integrations/state/{state_key}")
async def get_state(state_key: str, container=Depends(get_container)):
    """通用状态读取路由。按 state_key 路由到框架能力。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    # state_key → 读取函数的注册表
    handler = STATE_HANDLERS.get(state_key)
    if handler is None:
        return {"success": False, "message": f"未知 state_key: {state_key}"}
    return {"success": True, "data": {"value": handler(layer)}}

@router.post("/integrations/action/{action}")
async def invoke_action(action: str, container=Depends(get_container)):
    """通用动作触发路由。按 action 路由到框架能力。"""
    layer = container.integration_layer
    if layer is None:
        return {"success": False, "message": "集成平台未启用"}
    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        return {"success": False, "message": f"未知 action: {action}"}
    result = await handler(layer)
    return {"success": True, "data": result}
```

#### 4.2.4 state/action 注册表

state_key 和 action 到框架能力的映射，**全部在 Aether 框架层定义，不认得小爱**：

```python
# integration_routes.py 顶部
STATE_HANDLERS = {
    "broadcast_enabled": lambda layer: layer.sink_manager.broadcast_enabled,
}

ACTION_HANDLERS = {
    "toggle_broadcast": _toggle_broadcast,
}

async def _toggle_broadcast(layer):
    new = not layer.sink_manager.broadcast_enabled
    layer.set_broadcast_enabled(new)
    return {"broadcast_enabled": new}
```

V1 只有两个（broadcast_enabled 状态 + toggle_broadcast 动作）。未来框架加新能力时，往这两个表注册即可。插件只能用已注册的 state_key/action——这是安全边界（插件不能任意触发未注册动作）。

### 4.3 前端：通用渲染器

#### 4.3.1 IntegrationSlot.vue 组件

```vue
<template>
  <div class="integration-slot" v-if="contributions.length">
    <component
      v-for="c in contributions"
      :is="componentFor(c.type)"
      :key="c.plugin_id + c.slot"
      :contribution="c"
    />
  </div>
</template>
```

props：`slot`（槽位名）。onMounted 调 `/api/integrations/ui_contributions`，过滤出 `slot === props.slot` 的贡献，按 type 渲染对应通用组件。

#### 4.3.2 通用 UI 组件

每种预定义 type 一个 Vue 组件：
- `ToggleButtonContribution.vue`：读 state_key → 渲染开/关态 → 点击 POST action → 更新态
- `IconButtonContribution.vue`：点击 POST action
- `StatusBadgeContribution.vue`：读 state_key → 显示徽标

这些组件用 Aether 现有 CSS 变量（`--color-primary` 等），不认得小爱。

#### 4.3.3 ChatView 接入

`ChatView.vue` 的 input-row 加占位：
```vue
<IntegrationSlot slot="chat_input_toolbar" />
```
没贡献时这个 slot 渲染空 div → 八竿子打不着。

---

## 5. 解耦验证标准

完成后验证：
1. 删 `integrations/xiaoai/` → `/chat` 无喇叭按钮（ui_contributions 为空）✅
2. ChatView.vue 无"小爱/音箱/broadcast"字眼 ✅
3. 加新插件贡献 UI → 只改 `integrations/newplugin/manifest.json`，不改 ChatView ✅

---

## 6. 实施计划

### Step 1：后端 ui_contribution schema + 加载 + 路由
- Manifest 加 ui_contributions 字段
- IntegrationLayer.list_ui_contributions
- /api/integrations/ui_contributions + /state/{key} + /action/{name} 路由
- state/action 注册表（broadcast_enabled + toggle_broadcast）

### Step 2：前端通用渲染器
- IntegrationSlot.vue（槽位组件）
- ToggleButtonContribution.vue（toggle_button 渲染）
- ChatView input-row 加 `<IntegrationSlot slot="chat_input_toolbar" />`

### Step 3：小爱插件声明 ui_contribution
- xiaoai/manifest.json 加 ui_contributions 字段
- 验证：删 xiaoai 文件夹 → /chat 无按钮

### Step 4：测试 + 文档
- 后端 ui_contribution 加载/路由测试
- 前端手动验证（有/无插件两种态）
- 更新插件系统文档加 ui_contribution 章节
