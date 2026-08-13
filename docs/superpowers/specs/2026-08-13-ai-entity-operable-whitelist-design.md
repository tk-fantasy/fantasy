# AI 设备操作白名单（entity_operable）设计

- 日期：2026-08-13
- 状态：已确认，待实施

## 1. 背景与目标

用户在 HA 接入了多个设备，通过 Aether 聊天用自然语言控制时，LLM 会因为语义相近而误操作不相关或危险的设备（如说"关门"时操作到"童锁"）。

**目标**：给每个实体一个"AI 可操作"权限标记，让用户显式决定哪些设备允许 AI 控制。AI 能看到所有设备，但只能操作被授权的设备。

**非目标**：不改 HA 侧任何配置；不替代现有的 entity_id 真实性校验和 `match_devices` 语义校验，而是在它们之上叠加一层用户授权校验。

## 2. 需求确认（已与用户对齐）

| 决策点 | 结论 |
|--------|------|
| 标记粒度 | 按 **entity_id（子实体）**，不是按物理设备 |
| 默认值 | **默认可操作**（黑名单模式：只存被显式禁用的，其余默认允许） |
| UI 位置 | 设备详情弹窗的**子实体行（`.entity-row`）右侧**，紧挨现有控制开关 |
| 拦截策略 | **方案 A：执行硬拦截 + 可见层软标注**（双保险） |
| 优先策略 | **多候选优先**：模糊指令命中多个实体时，AI 优先选 `ai_operable=true` 的去操作，被禁用的自然跳过（仅 prompt 引导，不改架构） |
| 可逆性 | **完全可逆**，随时反复开关，禁用后可一键恢复 |
| DB 故障 | 放行（与现有校验风格一致，且"默认可操作"前提下不改变默认行为） |
| prompt 呈现 | 禁用项仍列进 system prompt 但标注 `⛔AI禁操作`，控制项不列 |
| 标记显示范围 | 只对**可控实体**（有 service 的）显示标记，只读 sensor 不显示 |

## 3. 总体架构

```
用户在设备页切换 [AI 可操作] 开关
        │  PUT /ha/entity-operable {entity_id, operable:bool}
        ▼
Aether DB (emoji_preferences, scope=entity_operable)  ── 黑名单：只存 disabled
        │
        ├──► get_entities 工具：返回每个实体 ai_operable 字段（LLM 可见）
        ├──► system prompt catalog：禁用项标 ⛔，控制项不列（LLM 软引导）
        └──► call_service 工具：执行前查黑名单，命中则拒绝（硬拦截）
```

三层联动：**可见层**（让 LLM 知道并主动避开）+ **拦截层**（LLM 万一强调用时后端拒绝）。两层独立，任一失效另一层兜底。

## 4. 详细设计

### 4.1 数据存储（零 schema 改动）

复用现有 `emoji_preferences` 表，新增 scope = `entity_operable`，完全照搬 `entity_alias`（`ha_routes.py:58`）/`entity_note`（`ha_routes.py:104`）先例。

- **黑名单只存例外**：表中记录形如 `{entity_id: "0"}`，表示该实体被禁用。不在表中的实体 = 默认可操作。
- 复用方法（`app/core/database.py`）：
  - 读：`prefs_get_by_scope("entity_operable")` → `{entity_id: "0", ...}`
  - 禁用：`emoji_pref_upsert("entity_operable", entity_id, "0")`
  - 恢复：`emoji_pref_delete("entity_operable", entity_id)`（删除记录即恢复默认可操作）
- 理由：默认值=可操作 天然成立（不在表即可操作）；DB 只存用户主动关掉的少数几个；"恢复"就是删记录，语义清晰且天然可逆。

### 4.2 后端硬拦截（`app/tools.py` call_service）

在 `_register_ha_call_service`（`tools.py:245`）的 handler 中，**现有 entity_id 真实性校验（`tools.py:269`）之后、`call_with_probe`（`tools.py:315`）之前**，插入授权校验：

```python
# 伪代码
disabled = await Database.get().prefs_get_by_scope("entity_operable")
eid_list = [e.strip() for e in str(entity_id).split(",") if e.strip()]
blocked = [e for e in eid_list if e in disabled]
if blocked:
    names = "、".join(entity_name_map.get(e, e) for e in blocked)
    return {
        "success": False,
        "error": (
            f"设备「{names}」被用户设为禁止 AI 操作。请勿尝试调用，"
            "如实告知用户需手动操作或在设备页解除限制。"
        ),
    }
```

- 支持逗号分隔批量 entity_id，任一被禁用则拒绝并指明具体哪个。
- **兜底**：DB 读取异常时放行（`except: logger.warning(...) ` 放行）。理由：与现有语义校验（`tools.py:313`）风格一致；"默认可操作"前提下读不到黑名单 = 全可操作，不改变默认行为，避免 DB 故障锁死整个家。
- `entity_name_map` 复用 dispatcher 已有的 `{entity_id: friendly_name}`（`dispatcher.py:83`），让错误信息用中文名而非裸 entity_id。

### 4.3 AI 可见性（让 LLM 知道、且别碰）

**A. `get_entities` 工具**（`tools.py:105`）：
- 返回的每个 entity 增加 `ai_operable: bool` 字段（默认 true，在黑名单则 false）。
- `devices`（物理设备 brief）层面不变——权限是 entity 级，物理设备汇总会丢失粒度。

**B. system prompt catalog**（`main.py:_refresh_ha_catalog`，`main.py:218`）：
- 设备目录行：禁用实体行尾加标记，如：
  `- lock.tong_suo (类型:lock, 状态:locked) 名称:童锁 ⛔AI禁操作`
  （行格式 `entity_id (类型, 状态) 名称` 的正则依赖见 `main.py:225`，标记加在行尾不影响 `rule_service._parse_ha_catalog` 抠 entity_id。）
- controls 明细（`controls_text`）：禁用实体**跳过不列控制项**，避免给 LLM 操作引导。
- `_refresh_ha_catalog` 内读一次 `entity_operable` 黑名单（与现有 `entity_note` 读取同位置同模式，`main.py:242`）。

**C. 系统提示约束**（`app/services/prompt_service.py`）—— 含两条规则：
- **多候选优先**：当用户指令能匹配多个实体时，优先选择 `ai_operable=true`（未标 ⛔）的去操作，被禁用的自然跳过——这让白名单不只拦截，还能引导 AI 选对设备（如「关门」同时命中门和童锁时优先操作门）。
- **硬禁令**：标 `⛔AI禁操作` 的设备即使可见也禁止操作；若用户明确要操作它，应告知需手动处理或在设备页解除限制。

### 4.4 API（照搬 entity-notes 模式）

新增于 `app/routes/ha_routes.py`：

- `GET /ha/entity-operable` → `{disabled: {entity_id: "0", ...}}`（禁用列表）
- `PUT /ha/entity-operable`，body `EntityOperableRequest{entity_id: str, operable: bool}`：
  - `operable=False` → `emoji_pref_upsert("entity_operable", entity_id, "0")`
  - `operable=True` → `emoji_pref_delete("entity_operable", entity_id)`（删除=恢复可操作）
  - 写入后触发 `container.catalog_refresh_fn()` 立即刷新 catalog（不等 60 秒，与 `set_entity_note` 同做法，见 `ha_routes.py:136`）。
- `app/schema/api_schemas.py` 新增 `EntityOperableRequest`（字段 `entity_id: str`、`operable: bool`）。

### 4.5 前端 UI（`frontend/src/views/HAListView.vue`）

- 在 modal 的 `.entity-row`（`HAListView.vue:742`）右侧、紧挨现有控制 `BaseToggle`（`HAListView.vue:752`），新增 **AI 权限标记**：
  - 形态：**状态徽章（不用 🤖 图标）**。允许 AI 操作时显示**绿色**标识（如绿点 + 绿色「AI 可操作」），禁止时显示**红色**标识（如红点 + 红色「禁止 AI」），紧挨控制 `BaseToggle` 放在旁边。点击徽章即切换状态。
  - 点击 → 乐观更新本地状态 + `PUT /ha/entity-operable`。
  - **仅对 `isControllable(e)` 为真的实体显示**该标记（`isControllable` 见 `HAListView.vue:85`）。只读 sensor/domain 无 service 可调，显示标记纯属噪声。
- 数据管理：新增 composable `useEntityOperable.js`（或并入现有 `useEntityMeta.js`，`composables/useEntityMeta.js`），封装 `loadOperable()` / `toggleOperable(entity_id)`，参照 `useEntityMeta` 的别名/备注加载-编辑-保存结构。
- 标记状态以 entity_id 为 key 存 `disabled` 集合，渲染时 `!disabled.has(entity_id)` = 可操作。

### 4.6 边界与一致性

- operable 是纯 Aether 侧概念，**不写 HA**（同 entity_note，HA 无此概念）。
- HA 重连/配置热替换（`ha_routes.py:270` 重建 client）不影响 operable（存 Aether DB）。
- `get_all_devices_grouped`（`ha_service.py:222`）无需改——operable 是叠加在实体上的权限层，不影响设备聚合。
- 与现有校验的关系：entity_id 真实性校验 → **operable 授权校验（新增）** → `match_devices` 语义校验 → 执行。三层串行，互不替代。

## 5. 涉及文件清单

| 文件 | 改动 |
|------|------|
| `app/core/database.py` | 无（复用现有方法） |
| `app/tools.py` | call_service 加授权拦截；get_entities 返回加 `ai_operable` |
| `app/main.py` | `_refresh_ha_catalog` 读黑名单、行尾标 ⛔、controls 跳过禁用项 |
| `app/routes/ha_routes.py` | 新增 `GET/PUT /ha/entity-operable` |
| `app/schema/api_schemas.py` | 新增 `EntityOperableRequest` |
| `app/services/prompt_service.py` | 系统提示加 ⛔ 硬禁令 + 多候选优先规则 |
| `frontend/src/views/HAListView.vue` | `.entity-row` 右侧加 AI 权限标记（仅可控实体） |
| `frontend/src/composables/useEntityOperable.js` | 新建（或并入 `useEntityMeta.js`） |

## 6. 测试要点

- call_service 对禁用 entity 返回 `success:false` + 明确中文 error。
- call_service 对可操作 entity 正常执行。
- **可逆性**：PUT operable=false → 被拦截；PUT operable=true → 恢复放行（反复切换生效）。
- 批量 entity_id（逗号分隔）含一个禁用项 → 拒绝并指明具体哪个。
- DB 读取异常 → 放行（不锁死），日志有 warning。
- get_entities 返回的 `ai_operable` 与黑名单一致。
- 前端 AI 标记只对 `isControllable` 实体显示；切换后乐观更新 + 接口落库。
- catalog 行尾 ⛔ 标记不破坏 `rule_service._parse_ha_catalog` 的 entity_id 正则。
