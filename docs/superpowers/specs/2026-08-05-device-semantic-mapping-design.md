# 设备语义映射（自定义备注）+ 两阶段设备注入 设计文档

**日期**: 2026-08-05
**状态**: 待评审
**作者**: brainstorming 协作产出

---

## 1. 背景与动机

### 1.1 真实场景

用户家里有继电器类设备（如电控大门），其**物理状态与 HA 上报/接收的信号语义是反的**：

- 继电器 `ON`（`state=on`）→ 物理上是"关门"
- 继电器 `OFF`（`state=off`）→ 物理上是"开门"

设备本身不会告诉系统真实门状态，只暴露继电器位。因此：

- 用户说"**开门**"时，系统得知道要对这台设备发 `turn_off`（而非直觉的 `turn_on`）。
- 用户说"**关门**"时，得发 `turn_on`。

现有架构里，LLM 只看到 `controls_to_text` 生成的"Turn On / Turn Off — action"这种无语义明细，无法知道这台设备的怪癖，会按直觉调用，导致开关反了。

### 1.2 更普遍的问题

继电器反转只是最典型的一种"设备怪癖"。还有：

- 单位偏差（某温度传感器读数偏 +2°C）
- 长按/短按语义不同
- 状态误报、需要二次确认
- 自定义命名与 HA 默认名不一致的业务含义

这些都需要一个**通用的、用户可自定义的、注入到 LLM 认知**的机制。

### 1.3 衍生诉求：架构臃肿

当前主聊天链路把**所有可控设备的完整操作明细**（设备名 + entity_id + 可控项 + domain/service/param + 范围/当前值）全量塞进 system prompt（`prompt_service.py:186-194`），每轮对话都带着。设备一多，token 臃肿、成本上升、LLM 注意力被噪声分散、更易混淆相似设备/拼错 entity_id。

项目方当年放弃"按 query 检索"、改成全量注入，是为了扛住承接指令（"把那个关了"——用户省略设备名）。但全量是"宁可冗余不能漏"的兜底，不是最优。

---

## 2. 决策记录（brainstorming 已确认）

| 决策项 | 选择 | 理由 |
|--------|------|------|
| 功能范围 | **通用设备备注**（自由文本） | 覆盖任意怪癖，不止反转一种 |
| 备注粒度 | **按 entity_id** | 与 `controls_to_text` 行级生成天然对齐 |
| 架构方向 | **重构两阶段**（瘦目录 + 按需拉详情） | 从根本解决 token 臃肿和注意力噪声 |
| 目录格式 | **名字 + entity_id + 能力名**（不带 domain/service/param 明细） | LLM 靠名字+能力判断该选哪台，详情按需拉 |
| 承接指令 | **system prompt 提示拼接近期设备** | 比纯靠历史上下文稳，比全量轻 |
| 备注边界 | **只影响 AI**（不映射前端状态显示） | 范围明确，前端 UI 不动，先解决 AI 理解问题 |
| 交付方式 | **分两阶段交付** | 阶段一零风险立即可用，阶段二架构优化独立验证 |

---

## 3. 现状分析（代码事实）

### 3.1 设备信息到达 LLM 的两条链路

| 链路 | 设备怎么给 LLM | 全量/检索 | 动作触发 |
|------|---------------|----------|---------|
| **主聊天**（"开灯""关门"） | 后台预编译中文文本，塞进 system prompt | **全量** | function calling（`call_service` tool_call） |
| **自动化规则生成**（"如果…就…"） | 塞进 prompt 模板 | 检索子集，无匹配回退全量 | LLM 输出 JSON 文本解析 |

### 3.2 关键代码位置

- **后台预编译**：`app/main.py:214-286` `_refresh_ha_catalog()`，每 60 秒跑一次，生成两份缓存：
  - `_ha_catalog_cache_ref[0]`：物理设备分组目录（`# 设备名` + `- entity_id (类型/状态) 名称:xxx`）
  - `_ha_controls_cache_ref[0]`：中文可控项明细（`controls_to_text` 输出）
- **缓存读取**：`app/main.py:132-137` `_get_ha_device_catalog()` / `_get_ha_device_controls()`
- **注入点**：`app/services/prompt_service.py:186-194` `build_system_prompt()`——有 `device_controls` 就注入全量，无则退回 `device_catalog`
- **上下文组装**：`app/agents/dispatcher.py:370-433` `_prepare_context()`
- **控件文本生成**：`app/services/entity_controls.py:207-244` `controls_to_text()`
- **工具**：
  - `app/tools.py:107-170` `get_entities`（返回全量 devices + entities + `_controls`）
  - `app/tools.py:173-274` `call_service`（含 entity_id 真实性校验 + query→entity 语义校验）
- **规则生成**：`app/services/rule_service.py:21-31` `_filter_devices`（按 query 模糊匹配过滤，无匹配回退全量）、`rule_service.py:208-214` 调 `controls_to_text`
- **存储**：`app/core/database.py:486-515` `emoji_pref_upsert` / `emoji_pref_delete` / `prefs_get_by_scope`（`entity_alias` scope 已在用，复用 `emoji_preferences` 表）
- **现有别名 API**：`app/routes/ha_routes.py:57-100` `GET/PUT /ha/entity-aliases`

### 3.3 已确认的约束

- `controls_to_text` 被**三个**地方调用：`main.py:277`（后台预编译）、`rule_service.py:213`（规则生成）、间接经 `get_entities` 工具。**备注注入到 `controls_to_text`，三条路同时受益。**
- `rule_service` 那条路**不能改瘦目录**——规则生成依赖完整 domain/service/param 明细，否则崩。瘦目录只用于主聊天 system prompt。
- HA 没有"备注"概念，**备注是 Aether 私有**，不同步到 HA（这点比 `entity_alias` 简单，`entity_alias` 还要同步 HA entity_registry.name）。

---

## 4. 总体设计

分两阶段交付。**阶段一独立可用、零架构风险**；**阶段二在阶段一基础上做架构优化**。

### 4.1 阶段一：备注能力 + 按需拉详情工具

**目标**：解决继电器反转等设备怪癖问题，AI 能正确理解和调用。保留现有全量注入，零架构风险。

**改动**：
1. 备注存储（复用 `emoji_preferences` 表，新 scope `entity_note`）
2. 备注注入 `controls_to_text`（行末拼一句）
3. 新增 `get_device_manual(entity_id)` 工具（按需拉单台设备详情 + 备注）
4. 备注 CRUD API（`GET/PUT /ha/entity-notes`）
5. 前端备注输入框

**效果**：LLM 在全量注入的设备列表里直接看到备注，例如：
```
大门继电器 (switch.gate_relay):
  Turn On — action
    domain=switch | service=turn_on
  Turn Off — action
    domain=switch | service=turn_off
  备注: 这台是继电器，ON=关门, OFF=开门。用户说"开门"时调用 turn_off
```

### 4.2 阶段二：两阶段架构重构

**目标**：从根本解决 token 臃肿和注意力噪声。

**改动**：
1. 新增瘦目录生成函数 `catalog_to_text()`（名字+ID+能力名）
2. 后台缓存改造：`_ha_controls_cache_ref` 不再预编译全量，改为只缓存瘦目录；全量 controls 移到 `get_device_manual` 按需算
3. `build_system_prompt` 策略改造：注入瘦目录 + 近期设备提示
4. dispatcher 记录并传递"近期交互设备"

**风险**：承接指令准确率可能下降，需充分测试调优。

---

## 5. 阶段一详细设计

### 5.1 数据模型

复用 `emoji_preferences` 表，scope = `entity_note`：

| 字段 | 含义 | 示例 |
|------|------|------|
| scope | 固定 `"entity_note"` | `entity_note` |
| key | entity_id | `switch.gate_relay` |
| emoji_char | 备注文本（自由文本，可多行） | `这台是继电器，ON=关门, OFF=开门...` |

**为何复用而非新建表**：`entity_alias` 已验证这套机制可用，`prefs_get_by_scope("entity_note")` 一次取全部，注入时 O(1) 查 dict。

### 5.2 备注注入：`controls_to_text` 改造

**位置**：`app/services/entity_controls.py:207`

**改动**：`controls_to_text` 增加可选参数 `note: str | None = None`。

- 当 `note` 非空时，在实体标题行（`{pad}子功能 {eid}:` 或 `{name} ({eid})`）下方、可控项列表之前，插入一行备注。
- 备注前缀用明确的标记，让 LLM 注意到这是用户自定义的语义提示。

**伪代码**：
```python
def controls_to_text(entity, controls, indent=0, note=None):
    eid = entity["entity_id"]
    pad = "  " * indent
    # ... 现有标题行逻辑 ...
    if note:  # 在标题后、可控项前插入备注
        lines.append(f"{pad}  备注（用户自定义，优先级最高）：{note}")
    if not controls:
        lines.append(f"{pad}  (no controls)")
        return "\n".join(lines)
    # ... 现有可控项遍历 ...
```

**调用方适配**：
- `main.py:277` 后台预编译：读全部备注 `db.prefs_get_by_scope("entity_note")`，按 entity_id 查 dict 传入。
- `rule_service.py:213` 规则生成：同样读备注传入（规则生成也受益于备注——例如建规则时 LLM 也能看到"开门要调 turn_off"）。
- `get_device_manual` 工具（新增）：查单条备注传入。

### 5.3 新增工具 `get_device_manual`

**位置**：`app/tools.py`，新增 `_register_ha_get_device_manual(deps)`，在 `register_all_tools` 里注册。

**用途**：按需拉取单台（或多台）设备的完整可控项明细 + 备注。阶段一作为补充手段（LLM 可主动调用看详情+备注）；阶段二成为控制前的强制步骤。

**接口**：
```python
MCPTool(
    client_id="ha_devices",
    tool_name="get_device_manual",
    description=(
        "查询单台或多台设备的详细操作手册（含 domain/service/param 明细和用户自定义备注）。"
        "控制不熟悉的设备、或设备有特殊语义（如继电器反转）时调用本工具。"
        "支持传一个或多个 entity_id（逗号分隔）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "entity_ids": {
                "type": "string",
                "description": "一个或多个 entity_id，逗号分隔",
            }
        },
        "required": ["entity_ids"],
    },
    handler=handler,
)
```

**handler 逻辑**：
1. 拉全部实体 + service defs（复用 `get_entities` handler 的拉取逻辑）
2. 按 `entity_ids`（逗号分隔）过滤
3. 对每个目标实体调 `resolve_controls` + `controls_to_text`（传入该实体的备注）
4. 返回拼好的文本 + 备注原文

**为何支持批量**：减少多轮工具调用往返，缓解"本地小模型多轮易错"的风险。

### 5.4 API

**位置**：`app/routes/ha_routes.py`，照 `entity-aliases` 写法。

**新增路由**：
- `GET /ha/entity-notes` → 返回 `{notes: {entity_id: note_text}}`
- `PUT /ha/entity-notes` → 设置/更新一条备注（空串表示删除）

**请求 schema**（`app/schema/api_schemas.py`）：
```python
class EntityNoteRequest(BaseModel):
    entity_id: str
    note: str = ""
```

**与 `entity_alias` 的差异**：
- **不同步 HA**（HA 无此概念），只写 Aether DB。
- 备注可多行（`emoji_char` 字段是 TEXT，无长度限制）。
- 写入后调 `container.ha_service.invalidate_states_cache()` 让后台 `_refresh_ha_catalog` 下个周期重新读备注（或立即触发一次刷新）。

### 5.5 前端

**改动最小化**：在设备列表/编辑处加一个"备注"输入框（textarea，支持多行）。

**调用**：`GET /ha/entity-notes` 拉全部 → 渲染到对应实体；编辑后 `PUT /ha/entity-notes` 保存。

**复用现有 UI 模式**：参考 `entity_alias` 的编辑交互（已有别名编辑的地方，旁边加备注输入）。

### 5.6 阶段一验收标准

- [ ] 用户能在前端给 `switch.gate_relay` 添加备注"ON=关门, OFF=开门"
- [ ] 主聊天里说"开门"，LLM 调用 `call_service(domain=switch, service=turn_off)`
- [ ] 主聊天里说"关门"，LLM 调用 `call_service(domain=switch, service=turn_on)`
- [ ] 自动化规则生成（"如果X就开门"）也正确使用 `turn_off`
- [ ] `get_device_manual` 工具能被 LLM 调用，返回详情+备注
- [ ] 备注为空时不影响现有行为（空备注 = 无备注行）
- [ ] 备注持久化，重启后仍在

---

## 6. 阶段二详细设计

### 6.1 瘦目录生成：`catalog_to_text`

**位置**：`app/services/entity_controls.py`，新增函数。

**格式**（用户已确认"名字+ID+能力名"）：
```
设备目录（控制前调 get_device_manual 查 domain/service/param 明细）：
- 大门继电器 (switch.gate_relay) — 能力: turn_on, turn_off
- 客厅吊灯 (light.chuang_ding) — 能力: turn_on, turn_off, brightness
- 空调 (climate.ke_ting_kt) — 能力: set_temperature, set_fan_mode, turn_on, turn_off
```

**能力名来源**：`resolve_controls` 返回的每个控件，其 `service` 字段去重。

### 6.2 后台缓存改造

**位置**：`app/main.py:214-286` `_refresh_ha_catalog`

**改动**：
- `_ha_catalog_cache_ref[0]`：仍保留（物理设备分组目录，`rule_service._parse_ha_catalog` 正则依赖其格式，**不能改格式**）
- `_ha_controls_cache_ref[0]`：从"全量 controls 明细"改为"瘦目录"（`catalog_to_text` 输出）
- 全量 controls 明细不再预编译，移到 `get_device_manual` 按需算

**注意**：`rule_service` 仍需要全量 controls 明细。它走的是自己的 `_filter_devices` + 直接调 `controls_to_text`（`rule_service.py:206-214`），**不依赖后台缓存**。所以后台缓存改造不影响规则生成。

### 6.3 system prompt 策略改造

**位置**：`app/services/prompt_service.py:186-194`

**改动**：`build_system_prompt` 的设备注入逻辑从：
```python
if device_controls:
    parts.append(f"\n设备可控项（...）：\n{device_controls}")
elif device_catalog:
    parts.append(f"\n当前 HA 可用设备...：\n{device_catalog}")
```
改为：
```python
if device_catalog_brief:  # 瘦目录
    parts.append(
        f"\n设备目录（控制前调 get_device_manual 查 domain/service/param 明细和备注）：\n{device_catalog_brief}"
    )
if recent_devices:  # 近期设备提示（承接指令用）
    parts.append(f"\n用户最近交互的设备：{recent_devices}")
```

### 6.4 近期设备追踪

**位置**：`app/agents/dispatcher.py`

**机制**：在 session 上记录最近 N 次（建议 N=3）`call_service` / `get_device_manual` 涉及的 entity_id + 设备名。`_prepare_context` 把这个列表拼成提示注入 system prompt。

**提示格式**：
```
用户最近交互的设备：
- 大门继电器 (switch.gate_relay)
- 客厅吊灯 (light.chuang_ding)
```

承接指令（"把那个关了"）时，LLM 优先从这个列表锁定设备。

### 6.5 阶段二验收标准

- [ ] system prompt 不再含全量 domain/service/param 明细（体积显著下降）
- [ ] LLM 控制设备前会先调 `get_device_manual`（或在承接指令时直接命中近期设备）
- [ ] 承接指令（"把刚才那个关了"）在近期设备提示下准确率不低于改造前
- [ ] 规则生成链路不受影响（仍能拿到全量明细）
- [ ] 备注仍正确注入（经 `get_device_manual` 返回 + 瘦目录里也可带一句话摘要，待定）

### 6.6 阶段二风险与缓解

| 风险 | 缓解 |
|------|------|
| 承接指令准确率下降 | 近期设备提示（N=3 起步，实测调整）；必要时回退全量 |
| 多轮工具调用，本地小模型易错 | `get_device_manual` 支持批量 entity_id；GUIDELINES 里明确"承接指令可直接用近期设备，不必重新拉 manual" |
| 跨 session 承接失效 | 近期设备列表持久化到 session（同 session 内有效）；跨 session 靠历史摘要兜底 |

---

## 7. 数据流总览

### 7.1 阶段一数据流

```
用户前端 → PUT /ha/entity-notes → emoji_preferences(scope=entity_note) → invalidate cache
                                                                              ↓
后台 _refresh_ha_catalog (60s) → 读 prefs_get_by_scope("entity_note") → controls_to_text(note=...) → _ha_controls_cache_ref
                                                                                                            ↓
用户聊天 → dispatcher._prepare_context → build_system_prompt(注入含备注的全量 controls) → LLM 看到"开门要调 turn_off"
```

### 7.2 阶段二数据流（在阶段一基础上）

```
后台 _refresh_ha_catalog → catalog_to_text → _ha_controls_cache_ref (瘦目录)
                                                                    ↓
用户聊天 → dispatcher._prepare_context(含近期设备) → build_system_prompt(瘦目录 + 近期设备提示) → LLM
                                                                                                      ↓
                                                                                          get_device_manual(entity_id) → 按需拉详情+备注
                                                                                                      ↓
                                                                                                  call_service
```

---

## 8. 测试策略

### 8.1 单元测试

- `entity_controls.controls_to_text` 带 note 参数的输出格式（`tests/test_entity_controls.py` 扩展）
- `catalog_to_text` 输出格式（阶段二）
- `get_device_manual` handler 过滤逻辑
- 备注存储 CRUD（复用 `test_entity_controls.py` 的 DB fixture 模式）

### 8.2 集成测试

- 备注持久化：写 → 重启 → 读
- 备注注入：设备注后，`_refresh_ha_catalog` 生成的文本含备注行
- 端到端：设继电器反转备注 → 聊天"开门" → 验证 LLM 调 `turn_off`（mock LLM 响应）

### 8.3 回归测试（阶段二重点）

- 承接指令语料集：收集 10-20 条承接指令，改造前后对比准确率
- 规则生成回归：确保 `_filter_devices` + `controls_to_text` 仍正常

---

## 9. 未决问题（留给实现阶段）

1. **备注长度上限**：`emoji_char` 是 TEXT 无限制，但超长备注会撑大 system prompt。建议前端软限 200 字，后端不硬限。
2. **近期设备 N 值**：阶段二 N=3 起步，实测调整。
3. **瘦目录里是否带一句话备注摘要**：**不带**。瘦目录只列名字+ID+能力名，备注完全经 `get_device_manual` 按需暴露，避免备注撑大瘦目录失去其"轻量"意义。若实测发现某些备注必须常驻（如反转类高频怪癖），再单独迭代。
4. **阶段二的回退开关**：是否提供配置项让用户切回全量注入（应对极端承接指令场景）。建议有，作为安全网。

---

## 10. 不做的事（YAGNI）

- **不**做结构化的"状态反转规则"（on→关门/off→开门的映射表）。自由文本备注已覆盖，结构化反而限制表达力。
- **不**改前端状态显示。备注只影响 AI，UI 仍按 HA 原始 state 显示。
- **不**同步备注到 HA。HA 无此概念，且备注是 Aether 私有增强。
- **不**在阶段一动 `rule_service` 的全量 controls 消费。瘦目录只用于主聊天。
- **不**做多粒度（设备级 + 实体级）备注。按 entity_id 单粒度已够。
