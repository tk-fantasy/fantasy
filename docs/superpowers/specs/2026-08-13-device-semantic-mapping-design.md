# 设备语义映射（`/semantics`）设计

- 日期：2026-08-13
- 状态：已确认，待实施

## 1. 背景与目标

部分设备的**物理状态/操作语义**与 HA 上报的信号语义相反。典型场景：门禁继电器（mesh 版通断器）——通电（HA `state=on`，`turn_on`）物理上是**关门**，断电（`state=off`，`turn_off`）物理上是**开门**。

`glm-4-flash` 这类模型的直觉很强且一致（"开" → `turn_on`，"关" → `turn_off`）。之前用 `entity_note`（自由文本备注）注入提示词尝试纠正，效果不稳定（提示词位于 prompt 中段，lost-in-the-middle；模型直觉压过备注）。

**目标**：提供代码级、确定性的**动作映射过滤层**，让 AI 凭直觉调用即可，系统无条件纠正 service，确保物理结果正确。同时通过**触发时动态反馈**让 AI 准确理解发生了什么，正确汇报给用户。

**非目标**：
- 不替代 `entity_note`（自由文本备注保留，两者并存）。
- 不做意图关键词判断（之前删掉的代码级翻转依赖"开/关"词判断，脆弱）。
- 不暴露具体映射规则到提示词（避免 AI 聪明反被聪明明 → 双重错误）。

### 双重错误陷阱（设计必须规避）

若提示词告诉 AI "该设备 on=关门"，聪明模型会尝试**反向调用**（用户要开门 → AI 直接调 `turn_off`）。但过滤器是**无条件**翻转（`turn_off`→`turn_on`），结果门关了，与用户意图相反。

**规避方式**：提示词零暴露具体规则；真实解释放在**动作执行后的结果反馈**里。AI 始终凭直觉调用（一致、可预测），过滤器无条件纠正，结果反馈事后解释。

## 2. 需求确认（已与用户对齐）

| 决策点 | 结论 |
|--------|------|
| 生效方式 | **代码级过滤**：call_service 执行前无条件替换 service，不判断意图 |
| 配置粒度 | **每条服务映射独立**（turn_on→turn_off 是一条，各自带描述） |
| 描述归属 | **每条映射一段描述**（非设备级），在映射触发时动态带给 AI |
| 提示词暴露 | **零暴露**：controls_to_text 不插任何映射字样 |
| 结果反馈 | call_service 返回附加 `semantic_mapping` 字段（requested/executed/description） |
| state 翻转 | **隐含跟随**：检测到对称翻转对（turn_on↔turn_off）自动翻 state，不单独配 |
| 页面结构 | **两层**：一级实体列表，二级该实体服务列表，可搜索 |
| 命名 | 路由 `/semantics`，视图 `SemanticsView.vue`，侧边栏标签"语义" |
| 存储 | 复用 `emoji_preferences` 表，新 scope `entity_action_map` |

## 3. 总体架构

```
用户在 /semantics 页配置动作映射（每条带描述）
        │  PUT /ha/action-maps {entity_id, mappings}
        ▼
Aether DB (emoji_preferences, scope=entity_action_map)
        │
        ├──► call_service 工具：
        │      1. 执行前查映射，无条件替换 service（turn_on→turn_off）
        │      2. 执行后若发生映射 → 返回 semantic_mapping{requested,executed,description}
        │      3. 对称翻转对 → new_state.state on↔off 反转
        │
        ├──► 状态读取（get_entities / controls current / 提示词）：
        │      统一经 flip_state_value() 翻转（对称对设备）
        │
        └──► 提示词 controls_to_text：不动，零暴露
```

## 4. 数据模型

复用 `emoji_preferences` 表（通用 KV-by-scope），新 scope `entity_action_map`。

- **key**：`entity_id`（如 `switch.gate`）
- **value**：JSON 字符串

```json
{
  "mappings": {
    "turn_on":  {"target": "turn_off", "description": "继电器反转：用户说开门时，断电(off)才是物理开门"},
    "turn_off": {"target": "turn_on",  "description": "继电器反转：用户说关门时，通电(on)才是物理关门"}
  }
}
```

**字段语义**：
- mappings 的 key = AI 调用的原始 service
- value.target = 实际执行的 service（target==key 无意义，不存）
- value.description = 该条映射触发时带给 AI 的解释（每条独立）

**对称翻转对的定义**（用于 state 隐含跟随）：
mappings 同时存在 `turn_on→turn_off` 与 `turn_off→turn_on`，视为对称翻转对 → state on/off 自动反转。

### 与其他 scope 的关系

| scope | 用途 | 注入方式 |
|-------|------|---------|
| `entity_note` | 自由文本备注（任意提示） | 提示词 controls_to_text |
| `entity_action_map` | 结构化动作映射 + 描述（本次） | 代码级过滤 + 结果反馈 |
| `entity_operable` | AI 可操作黑名单 | 执行前拦截 |

三者独立，可在同一设备并存。门禁用 action_map，其他需提示的设备继续用 note。

## 5. 核心机制：call_service 过滤层

### 层 1 — 动作过滤（app/tools.py，call_service handler，L343 `call_with_probe` 调用前）

```python
# 语义映射过滤：无条件替换 service（不依赖意图判断，避免双重错误）。
# AI 凭直觉调用（稳定一致），过滤器无条件纠正，结果反馈事后解释。
original_service = service
mapped_description = None
if entity_id:
    try:
        action_map = await get_action_map(str(entity_id).split(",")[0].strip())
        if action_map:
            entry = action_map.get("mappings", {}).get(service)
            if entry and entry.get("target") and entry["target"] != service:
                service = entry["target"]
                mapped_description = entry.get("description", "")
                logger.info("call_service 语义映射: %s.%s → %s",
                            entity_id, original_service, service)
    except Exception:
        logger.warning("call_service: 语义映射查询失败，放行原 service", exc_info=True)

result = await call_with_probe(ha_client, domain, service, entity_id, data)
```

### 层 2 — 结果反馈 + state 翻转（同 handler，L354 return 处）

```python
ret = {"success": True, "result": result, "new_state": new_state}
if service != original_service:
    # 动作被映射 → 带上描述，让 AI 理解实际发生了什么、如何汇报给用户
    ret["semantic_mapping"] = {
        "requested": original_service,
        "executed": service,
        "description": mapped_description or "该设备配置了语义映射",
    }
    # 对称翻转对 → state 隐含跟随翻转（避免 AI 看到相反状态说反话）
    if new_state and new_state.get("state") in ("on", "off"):
        ret["new_state"] = apply_state_flip(new_state, entity_id)
return ret
```

**效果**：用户说"开门" → AI 调 turn_on（直觉）→ 过滤器改 turn_off（物理开门）→ 返回
```json
{"success": true, "new_state": {"state": "on"},
 "semantic_mapping": {"requested": "turn_on", "executed": "turn_off",
   "description": "继电器反转：用户说开门时，断电(off)才是物理开门"}}
```
→ AI 读到描述 → 准确说"门已打开"，不乱说"已关闭"。

### 层 3 — 提示词零暴露（映射规则），但 state 值预翻转

**两条不同的原则，不要混淆：**

| 维度 | 处理 | 原因 |
|------|------|------|
| 映射**规则**（turn_on→turn_off） | **零暴露**，controls_to_text 不插任何映射字样 | 防双重错误（AI 反向调用） |
| **state 值**（on/off） | **预翻转**后进提示词 | 保证查询类对话正确（"门开着吗"） |

state 预翻转在 **`_refresh_ha_catalog`（app/main.py L218）数据源处**完成，不在 controls_to_text 内部——
即 `_refresh_ha_catalog` 读取 `e['state']` 后，对对称翻转对设备调 `flip_state_value`，
把翻转后的 state 同时用于 catalog 行（L285 `状态:{...}`）和传给 `resolve_controls` 的 flat entity。
`controls_to_text` 函数代码**不修改**，它只是渲染收到的（已翻转的）数据。

## 6. 统一 helper：app/services/semantic_map.py

新增独立模块，集中映射逻辑，避免散落到多处：

```python
from __future__ import annotations
from typing import Any
import json, logging
logger = logging.getLogger(__name__)

# 进程内缓存（写入时清除），避免每次 call_service 都查 DB
_cache: dict[str, dict] = {}
_cache_loaded = False


async def get_action_map(entity_id: str) -> dict | None:
    """读取某实体的动作映射。带进程内缓存（DB 异常时返回 None 放行）。"""
    global _cache_loaded
    if not _cache_loaded:
        await _reload_cache()
    return _cache.get(entity_id)


async def _reload_cache() -> None:
    global _cache_loaded
    try:
        from ..core.database import Database
        raw = await Database.get().prefs_get_by_scope("entity_action_map")
        parsed = {}
        for eid, val in raw.items():
            try:
                obj = json.loads(val) if isinstance(val, str) else val
                if isinstance(obj, dict) and obj.get("mappings"):
                    parsed[eid] = obj
            except (ValueError, TypeError):
                logger.warning("动作映射解析失败 entity=%s", eid, exc_info=True)
        _cache.clear()
        _cache.update(parsed)
        _cache_loaded = True
    except Exception:
        logger.warning("动作映射缓存加载失败", exc_info=True)


def invalidate_cache() -> None:
    """写入后调用，下次读取时重新加载。"""
    global _cache_loaded
    _cache_loaded = False


def is_flipped_pair(mappings: dict) -> bool:
    """检测对称翻转对：turn_on→turn_off 且 turn_off→turn_on 同时存在。"""
    def target_of(svc):
        e = mappings.get(svc)
        return e.get("target") if isinstance(e, dict) else None
    return (target_of("turn_on") == "turn_off"
            and target_of("turn_off") == "turn_on")


def apply_state_flip(new_state: dict, entity_id: str) -> dict:
    """对称翻转对设备：把 new_state.state on↔off 反转。非翻转设备原样返回。"""
    am = _cache.get(entity_id)
    if not am or not is_flipped_pair(am.get("mappings", {})):
        return new_state
    s = new_state.get("state")
    if s == "on":
        flipped = {**new_state, "state": "off"}
    elif s == "off":
        flipped = {**new_state, "state": "on"}
    else:
        return new_state
    return flipped


async def flip_state_value(entity_id: str, state: str) -> str:
    """供状态读取点调用（get_entities / 提示词 current）。翻转 on/off。"""
    am = await get_action_map(entity_id)
    if am and is_flipped_pair(am.get("mappings", {})):
        if state == "on":
            return "off"
        if state == "off":
            return "on"
    return state
```

**四个状态读取点统一调 `flip_state_value` / `apply_state_flip`**：

| 读取点 | 文件:行 | 用哪个函数 | 说明 |
|--------|---------|-----------|------|
| catalog 行 `状态:{e['state']}` | app/main.py:285 (`_refresh_ha_catalog`) | `flip_state_value`（async） | 设备列表提示词 |
| resolve_controls 的 current | app/main.py:296（同函数内，传给 resolve_controls 的 flat entity） | 同上，预翻转 entity['state'] | 可控项当前值 |
| call_service 返回 new_state | app/tools.py:354 | `apply_state_flip`（sync，已在缓存内） | 操作确认 |
| get_entities / get_device_manual | app/tools.py | `flip_state_value`（async） | 工具查询结果 |

**注意**：前两点在同一函数 `_refresh_ha_catalog` 内，翻转一次 entity['state'] 即同时覆盖 catalog 行和 resolve_controls。

## 7. REST API

### GET /ha/action-maps

返回全部已配置的动作映射。

**响应**：
```json
{"code": 0, "data": {"maps": {
  "switch.gate": {"mappings": {
    "turn_on": {"target": "turn_off", "description": "..."},
    "turn_off": {"target": "turn_on", "description": "..."}
  }}
}}}
```

### PUT /ha/action-maps

设置/更新一个实体的动作映射。空 mappings = 删除。

**请求体**（新 schema `ActionMapRequest`）：
```python
class ActionMapRequest(BaseModel):
    entity_id: str
    mappings: dict  # {svc: {target, description}}
```

**逻辑**：
- `entity_id` 非空校验
- mappings 为空 → `emoji_pref_delete`（删除）
- mappings 非空 → 先校验：
  - 从 `entity_id` 抽取 domain（`split(".")[0]`）
  - 每个 entry 的 `target` 必须 ∈ 该域 services 列表（调 `get_service_defs`），且 ≠ 源 service
  - 拒绝无效/循环 target，返回 400 + 错误明细
  - 校验通过 → 过滤掉 target==源的条目（无意义不存）→ `emoji_pref_upsert("entity_action_map", entity_id, json.dumps({mappings}))`
- 写入后 `semantic_map.invalidate_cache()` + `asyncio.create_task(catalog_refresh_fn())`

### GET /ha/entity-services

返回按域分组的可用服务列表（供前端拉取可配置的 action）。

**响应**：
```json
{"code": 0, "data": {"services": {
  "switch": ["turn_on", "turn_off", "toggle"],
  "media_player": ["toggle", "volume_up", "volume_down", "play_media", ...]
}}}
```

复用现有 `ha_service.get_service_defs()`（返回 `{domain: {svc: {fields, required}}}`），剥离成 `{domain: [svc_name]}`。

## 8. 前端 /semantics 页

### 路由与导航

- `frontend/src/router/index.js` 新增：
  ```js
  { path: '/semantics', name: 'Semantics', component: () => import('../views/SemanticsView.vue') }
  ```
- `frontend/src/components/SidebarNav.vue` 的 `navItems` 新增：
  ```js
  { path: '/semantics', icon: '&#128256;', label: '语义' }  // 🔀
  ```

### 页面结构（SemanticsView.vue）

**主视图** — 已配置映射的实体卡片列表：
```
┌─ 语义映射 ──────────────────────────────────────┐
│ 大门 (switch.gate)  turn_on↔turn_off  [编辑][删] │
│ 加湿器 (switch.hum)  无映射（可编辑）            │
│ [+ 添加设备]                                    │
└─────────────────────────────────────────────────┘
```

**配置 Modal** — 两层（实体 → 服务）：
```
┌─ 配置语义映射 ────────────────────────────────────┐
│ [🔍 搜索实体: 大门________]                        │  ← 一级：实体选择
│   ○ 大门 (switch.gate)                            │
│   ○ 童锁 (switch.gate_child_lock)                 │
│   ○ 灵动开关 (switch.gate_dynamic)                │
├───────────────────────────────────────────────────┤
│ 选中：大门 (switch.gate)  可用服务：              │  ← 二级：该实体服务
│   turn_on  实际执行 [turn_on ▼]                   │     (从 /ha/entity-services
│            描述 [继电器反转：开门时断电...______] │      按域实时拉取)
│   turn_off 实际执行 [turn_off ▼]                  │
│            描述 [继电器反转：关门时通电...______] │
│   toggle   实际执行 [toggle ▼]                    │
│            描述 [____________________________]    │
│                                                   │
│ [保存]  [删除全部映射]                            │
└───────────────────────────────────────────────────┘
```

**交互细节**：
- 一级搜索：过滤实体列表（按 friendly_name / entity_id 模糊匹配）
- 二级服务下拉：默认 `target=自身`（无映射），改成其他 service 即建立映射
- 描述框：仅当 target≠自身时启用（无映射无需描述）
- 保存：收集所有 target≠自身 的条目 → PUT /ha/action-maps
- "对称翻转对"由用户自然配置完成（分别把 turn_on→turn_off、turn_off→turn_on），无需系统额外提示

### 数据流
- onMounted：`GET /ha/action-maps` + `GET /ha/entities`（实体列表）
- 选实体后：按 entity 域 `GET /ha/entity-services` 取服务列表
- 保存：`PUT /ha/action-maps`

## 9. 测试要点

### 后端单元测试（pytest）

1. **semantic_map 模块**
   - `get_action_map` 缓存命中/失效后重载
   - `is_flipped_pair`：对称对返回 True，单边返回 False
   - `apply_state_flip`：翻转设备 on↔off，非翻转设备原样
   - `flip_state_value`：异步版同样逻辑
   - DB 异常 → 返回 None / 原值，不抛

2. **call_service 过滤层**
   - 有映射：turn_on 被替换为 turn_off，返回带 `semantic_mapping`
   - 无映射：service 不变，返回无 `semantic_mapping`
   - 对称翻转对：new_state.state 被翻转
   - 非对称：new_state 不变
   - DB 异常：放行原 service

3. **REST API**
   - PUT 后 GET 能读到
   - 空 mappings = 删除
   - 写入触发 cache invalidate + catalog refresh

### 前端（手动）
- 配置门禁继电器 turn_on↔turn_off + 描述
- 聊天说"开门" → 验证实际调 turn_off、AI 回复"门已打开"
- 聊天说"关门" → 验证实际调 turn_on、AI 回复"门已关闭"
- 查询"门开着吗" → AI 基于翻转后 state 正确回答

## 10. 实施顺序建议

1. `app/services/semantic_map.py`（helper + 缓存）
2. `app/schema/api_schemas.py`（ActionMapRequest）
3. `app/routes/ha_routes.py`（GET/PUT action-maps + entity-services）
4. `app/tools.py`（call_service 过滤层 + 结果反馈）
5. 状态读取点接入 `flip_state_value`（get_entities / controls current）
6. 前端 `SemanticsView.vue` + 路由 + 侧边栏
7. 测试 + 端到端验证（虚拟设备门禁继电器）

## 11. 风险与边界

| 风险 | 应对 |
|------|------|
| DB 异常导致映射失效 | 全程 try/except 放行原 service（与现有校验风格一致） |
| 缓存与 DB 不一致 | 写入即 invalidate，下次读取重载 |
| 用户配置循环/无效映射 | PUT 时校验：target 必须∈该域 services 列表且 ≠ 源 service，拒绝循环/无效 target |
| state 翻转误伤非 on/off 设备 | `is_flipped_pair` + state∈{on,off} 双重守卫 |
| toggle 服务 | toggle 不翻转（它是自反操作），映射里一般不配 toggle |
| 批量 entity_id（逗号分隔） | 只查首个 entity 的映射（`split(",")[0]`），批量翻转场景罕见，YAGNI |
