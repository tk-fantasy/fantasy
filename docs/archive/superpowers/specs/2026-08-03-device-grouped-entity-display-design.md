# 设备分组实体展示（Device-Grouped Entity Display）

**日期**: 2026-08-03
**状态**: 设计中

## 背景与动机

当前 Aether 的设备页（`HAListView.vue`）把 HA 里所有白名单实体**扁平铺开**，仅按
`area_name`（区域）一个维度分组。一个物理设备（如小爱音箱Pro）会被 MIoT 协议拆成
10 个实体（media_player + 4 button + 2 switch + 2 notify + 1 sensor），全部平铺后：

- 列表被「诊断/配置类」子实体淹没（版本号、WiFi名、童锁、灵动开关……），核心可控实体
  反而被稀释。
- 同一物理设备的子实体散落在各处，用户难以建立「这台设备整体什么状态」的心智模型。
- 语义匹配（`text_match.py`）在「打开大门开关」时要在多个 `switch.poyde_*` 里区分。

HA 侧 `device_registry` 已有完整的物理设备维度（name / model / manufacturer /
sw_version / area_id），且 entity 通过 `device_id` 关联到 device。但 Aether 后端的
`get_all_devices()` **只返回扁平实体列表，丢弃了 device 维度**。

## 目标

把设备页从「区域 > 实体」两级，改成「区域 > 物理设备 > 实体」三级：

1. 首页（区域分组下）每个物理设备显示为**一张卡片**，卡片上显示设备名、型号、在线状态。
   **卡片本身不强行显示快控**——多实体设备无法客观选出唯一「主实体」，故卡片只做入口。
2. 点设备卡片 → 打开详情抽屉/弹窗，列出该设备下**全部子实体**，按
   「可控 → 只读」两组排列，可控实体在前。可逐个查看/控制。
3. 保留现有区域 tab + 区域分组标题。
4. 保留扁平实体接口给下游（语义匹配、automation、`/ha/entities` 的旧消费者），
   不破坏既有契约。

### 为什么不选「主实体」方案

曾考虑让每张卡片显示一个「主实体」快控，按 domain 优先级表
（`media_player > climate > light > switch ...`）自动选主实体。**否决**，原因：

- 优先级表本身是硬编码，与用户「不要硬编码」的诉求冲突。
- 实测无法唯一区分主/子实体：大门通断器下 `light`（指示灯）和 `switch.on`
  （主通断）同属 controllable + has_state，任何 domain 优先级都会选错。
- 该判断是**语义判断**，HA 数据里没有任何字段标记「哪个是主开关」
  （`entity_category` 对这些子实体全为 null）。

最终采用「卡片不显示快控，详情页按可控性分组」——唯一判断依据是后端
`/ha/services` 真实返回的 service 集合，零硬编码 domain 优先级。

## 非目标（YAGNI）

- 不做设备图的拖拽编排（已有 `KGraphView` / `SgView`）。
- 不做 device 级别的批量操作（如「一键关闭整台设备的所有实体」）——主实体快控已够用。
- 不重构 `entity_controls` / 服务发现机制——只复用现有 `_controls`。
- 不动 MQTT 模拟器设备（它们没有 device 维度，走降级路径，见下）。

## 后端数据现状（已核实）

`ha_service._get_area_maps_cached()` 当前已通过 WS 拉取：

- `config/area_registry/list` → `{area_id: name}`
- `config/device_registry/list` → device 列表（含 name/model/manufacturer/sw_version/area_id）
- `config/entity_registry/list` → entity 列表（含 entity_id/device_id/area_id/platform/disabled_by）

但只暴露了 `entity_id → area_id` 映射，device 维度信息被丢弃。

实测 3 个小米 device：

```
小米智能多模网关2  model=lumi.gateway.mcn001   manufacturer=Aqara   14 entities, 主实体=None（纯网关）
大门              model=poyde.switch.tdq3      manufacturer=poyde   12 entities, 主实体=light..._indicator_light
小爱音箱Pro        model=xiaomi.wifispeaker.lx06 manufacturer=小米   10 entities, 主实体=media_player..._lx06
```

## 设计

### 1. 后端：新增 device 维度数据结构

在 `ha_service.py` 新增方法 `get_all_devices_grouped()`，返回设备分组结构：

```python
# 返回结构
{
  "areas": [{"area_id": "yan_fa_bu", "area_name": "研发部"}],
  "devices": [
    {
      "device_id": "...",
      "name": "小爱音箱Pro",            # name_by_user 优先，否则 name
      "model": "xiaomi.wifispeaker.lx06",
      "manufacturer": "小米",
      "sw_version": "1.94.14",
      "area_id": "yan_fa_bu",
      "area_name": "研发部",
      "entity_count": 4,
      "controllable_count": 3,
      "entities": [ ... ]              # 该设备下所有白名单实体（含 _controls）
    },
    ...
  ]
}
```

#### 卡片内排序：可控优先（零硬编码）

后端不选「主实体」。每个 device 的 `entities` 列表在前端按可控性分组渲染，
判断依据**唯一来源是 `/ha/services` 真实 service 定义**：

```python
# 复用前端 isControllable / isToggleable 的语义，后端在 _controls 里已有标记
# 可控 = entity 所属 domain 有以下任一 service：
CONTROL_SIGNAL_SERVICES = {
    "turn_on", "turn_off", "toggle",
    "lock", "unlock",
    "open_cover", "close_cover", "open_valve", "close_valve",
    "play_media", "play_pause", "media_stop", "media_play", "media_next",
    "set_temperature", "set_humidity", "set_position", "select_source",
    "start", "stop",  # vacuum 等
}
```

- 这个集合描述的是「什么算一次控制动作」，是**动作语义**而非 domain 优先级。
  新增 domain（如未来 HA 加 `camera` 可控制）只要暴露这些 service 之一即自动生效，
  无需改这张表——它是 service 名集合，不是 domain 集合。
- 卡片内渲染顺序：`controllable entities` 在前，`readonly entities`（sensor、
  只读 attribute）在后，各自内部按 entity_id 字母序稳定排列。

实测三个设备分组结果（已验证）：

| 设备 | 可控（前） | 只读（后） |
|------|-----------|-----------|
| 多模网关2 | 0 | IP、WiFi名、接入方式 |
| 大门 | 指示灯、童锁、灵动开关、主通断 | 故障、版本号 |
| 小爱音箱Pro | 音箱、静音、睡眠模式 | 音频ID |

#### 降级：无 device 维度的实体（MQTT 模拟器等）

部分实体在 entity_registry 里 `device_id = null`（典型是 MQTT 手动配置的实体）。
这些实体**无法归属到任何 device**，处理策略：

- 在 `devices` 列表里为每个「无 device 的实体」生成一个**虚拟设备**：
  `device_id = "virtual:" + entity_id`，`name = entity.friendly_name`，
  `model = null`，`entities = [该实体本身]`。
- 虚拟设备在卡片上与真实设备外观一致，只是没有型号/厂商行。
- 这样保证前端只有一条渲染路径，不为 MQTT 单独写分支。

### 2. 后端：API 改造

**修改 `/api/ha/entities`**（`ha_routes.py`）：返回结构改为

```json
{
  "areas": [...],
  "devices": [...],          // 新：设备分组
  "entities": [...],         // 保留：扁平列表，兼容下游
  "count": 25
}
```

`entities` 字段保持原样（含 `_controls`），确保 `/ha/entities` 的既有消费者
（`tools.py`、`prompt_service.py`、`text_match.py`、`automation_service.py`）
完全不受影响。前端改读 `devices` 字段。

### 3. 前端：HAListView 三级渲染

#### 数据模型

```js
const devices = ref([])      // 替代 entities 作为主数据源
const areas = computed(...)  // 从 devices 里聚合 area_name
```

#### 分组 computed（区域 > 设备）

```js
const groupedDevices = computed(() => {
  // 区域 tab 过滤 + 搜索过滤后，按 area_name 分组
  const groups = {}
  for (const dev of filteredDevices.value) {
    const area = dev.area_name || '未分组'
    ;(groups[area] ||= []).push(dev)
  }
  return Object.entries(groups).sort(([a],[b]) => a.localeCompare(b))
})
```

#### 卡片渲染（纯入口，无快控）

设备卡片（区域 section 内）显示：

- 设备名（`device.name`）+ emoji（复用现有 emoji prefs，scope 改为 `device:<device_id>`）
- 型号 / 厂商小字（有则显示，无则省略）
- 在线状态：device 下任一实体 `!= unavailable` 即「在线」
- 实体计数摘要：`N 个可控 · M 项属性`（N=controllable_count，M=entity_count-N）
- 卡片**不显示任何快控**（多实体设备无法客观选主实体）
- 卡片点击 → 打开设备详情 modal

#### 设备详情 modal

复用现有 `selectedDevice` + modal 结构，但改为：

- modal 顶部显示设备名/型号/区域
- 列出 `device.entities`，**分两组**：
  - 「控制」组：controllable 实体，每行 = friendly_name + 状态 + 快捷 toggle
    （可 toggle 的）/ 模式按钮（climate 等多模式）。点击行展开完整属性。
  - 「信息」组：readonly 实体（sensor / 只读 attribute），每行 = friendly_name + 数值。
- 点击单个实体行 → 展开该实体的完整属性/能力（复用现有
  `dynamicInfoRows` / `capabilities` / `displayAttributes` 逻辑，数据源从
  `selectedDevice` 改成「当前展开的实体」）。
- 分组本身是前端 computed：`entities.filter(isControllable)` / `.filter(!isControllable)`，
  `isControllable` 复用前端现有函数（已基于 services 判断），无新规则。

### 4. 缓存与失效

- `get_all_devices_grouped()` 复用 `_get_states_cached`（5s TTL）和
  `_get_area_maps_cached`（60s TTL，已含 device_registry），不新增缓存层。
- `call_service` 后 `invalidate_states_cache()` 保持不变，5s 内前端重拉即刷新。

## 改动清单

| 文件 | 改动 |
|------|------|
| `app/services/ha_service.py` | 新增 `get_all_devices_grouped()`；`_get_area_maps_cached` 额外暴露 `device_id→device_info` 和 `entity_id→device_id` 映射（已有数据，只补返回值） |
| `app/routes/ha_routes.py` | `/ha/entities` 增加 `devices` 字段；保留 `entities` 字段 |
| `frontend/src/views/HAListView.vue` | 主数据源改 `devices`；分组 computed 改两级；卡片模板重写为设备维度；modal 改为设备详情 |

## 风险与权衡

1. **可控性判断依赖 service 定义**：极少数 domain 可能没有标准 service 但用户认为
   「可控」（如某些自定义集成）。可接受——前端 `isControllable` 本就基于 services，
   行为一致；用户仍可在「信息」组看到该实体。
2. **虚拟设备膨胀**：若用户有大量无 device 的 MQTT 实体，`devices` 列表会等长
   增长。但每虚拟设备只含 1 实体，渲染开销与原扁平列表相当，不劣化。
3. **device_registry 60s 缓存**：用户在 HA 改了设备名/区域后，最多 60s 才反映到
   Aether。可接受——和现有 area 缓存策略一致。
4. **emoji prefs scope 变更**：从 `entity:<id>` 变为 `device:<id>`，旧 emoji 偏好
   不会自动迁移。本期接受（用户重新选一次即可），不做迁移脚本。

## 测试要点

- 后端：3 个小米 device 正确分组（小爱音箱Pro / 大门 / 多模网关2），
  `entity_count` / `controllable_count` 与实测一致（4/3、6/4、4/0）。
- 后端：MQTT 模拟器实体生成虚拟设备，`device_id` 以 `virtual:` 开头。
- 后端：`/ha/entities` 同时返回 `devices` 和 `entities`，`entities` 的 count 与
  `devices` 内全部 entities 之和一致。
- 前端：区域 tab 过滤、搜索过滤在设备维度生效。
- 前端：设备详情 modal 按「控制 / 信息」两组排列，可控实体在前；toggle 后状态
  乐观更新。
- 前端：`isControllable` 完全基于 services 判断，无 domain 硬编码优先级。
- 回归：`tools.py` / `text_match.py` / `automation_service.py` 仍用扁平 `entities`，
  行为不变。
