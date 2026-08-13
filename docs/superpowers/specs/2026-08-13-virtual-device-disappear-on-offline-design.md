# 虚拟设备「离线即消失」设计

**日期**: 2026-08-13
**状态**: 待评审

## 背景与问题

「高级配置」页已有「虚拟设备」开关，停止/启动 `aether-simulator` + `mosquitto` 两个容器。但停止模拟器后，模拟器的 12 个 mqtt 设备只是变成 `unavailable`，仍然：

- 以「离线」卡片形式**留在 `/halist` 设备列表里**；
- **留在 AI 的设备目录里**（系统提示注入的 catalog + `get_entities` 工具结果）。

用户希望：**关闭虚拟设备后，这些设备彻底消失——AI 看不见、人也看不见**；重新开启后自动回来。

## 需求（已确认）

1. **范围**：只针对模拟器的 12 个 mqtt 设备，真实设备（xiaomi_home / demo）永远不受影响。
2. **触发条件**：当这 12 个设备**全部** `unavailable`/`unknown`（即模拟器整体下线）时才隐藏；部分在线则一个都不隐藏。严格匹配「全部离线才触发」。
3. **效果**：从三处同时消失——
   - AI 系统提示目录（`main.py` 每 60s 重建）
   - AI `get_entities` 工具结果
   - 人类设备列表 `/halist`（`GET /ha/entities`）
4. **AI 行为**：AI 完全不感知这些设备。不向系统提示加任何「虚拟设备已关闭」之类的话术；用户问起时 AI 自然回答「没找到该设备」。
5. **可逆**：模拟器重启、设备恢复在线后自动重新出现。
6. **识别方式**：用配置的**实体 ID 白名单**识别模拟器设备（不依赖 `platform == "mqtt"` 假设，未来接入真实 mqtt 设备不会被误伤）。
7. **即时性**：关闭/开启模拟器后，尽量立刻反映（移除 Aether 自身的缓存延迟）。

## 设计

### 1. 配置：模拟器实体白名单

`config.json` 新增一段（实体 ID 来自 `ha_config/.storage/core.entity_registry` 中 `platform == "mqtt"` 的 12 条）：

```json
"simulator": {
  "entity_ids": [
    "climate.zhong_yang_kong_diao",
    "cover.ke_ting_chuang_lian",
    "fan.ke_ting_feng_shan",
    "humidifier.wo_shi_jia_shi_qi",
    "light.chu_fang_deng",
    "light.chuang_tou_deng",
    "light.ke_ting_diao_deng",
    "sensor.ke_ting_shi_du",
    "sensor.ke_ting_wen_du",
    "switch.chu_fang_zhi_neng_cha_zuo",
    "switch.ke_ting_zhi_neng_cha_zuo",
    "switch.wo_shi_zhi_neng_cha_zuo"
  ]
}
```

读取：`get_config("simulator.entity_ids", [])`。模拟器增减设备时改这里即可。

### 2. HAService 过滤规则（核心）

`app/services/ha_service.py` 新增私有方法：

```python
def _virtual_suppress_set(self, states_by_id: dict[str, dict]) -> set[str]:
    """返回应隐藏的模拟器实体集合。

    规则：配置白名单(simulator.entity_ids)中当前存在的实体若【全部】
    unavailable/unknown → 返回全部；否则返回空集。
    匹配"全部离线才触发"语义。
    """
    whitelist = set(get_config("simulator.entity_ids", []) or [])
    if not whitelist:
        return set()
    present = [eid for eid in whitelist if eid in states_by_id]
    if present and all(
        states_by_id[eid].get("state") in ("unavailable", "unknown")
        for eid in present
    ):
        return set(present)
    return set()
```

#### 插入点 ① `get_all_devices()`（ha_service.py:192）

当前直接遍历 `states`。改为先建 `states_by_id = {s["entity_id"]: s for s in states}`，算出 `suppress = self._virtual_suppress_set(states_by_id)`，在循环内追加 `if entity_id in suppress: continue`。

#### 插入点 ② `get_all_devices_grouped()`（ha_service.py:222）

该方法已构建 `by_id` 字典。在 `by_id` 完整收集后、分组前插入：

```python
suppress = self._virtual_suppress_set(by_id)
if suppress:
    by_id = {eid: s for eid, s in by_id.items() if eid not in suppress}
```

两处都改后，AI 目录、`get_entities` 工具、`/ha/entities` 三处自动同步过滤——因为它们都最终调这两个方法。

### 3. 即时刷新（关/开模拟器后立刻反映）

`app/routes/simulator_routes.py` 的 `simulator_stop` / `simulator_start` 增加 `Depends(get_container)`，在容器动作成功后：

```python
container.ha_service.invalidate_states_cache()   # 清 5s 状态缓存
await container.catalog_refresh_fn()             # 即时重建 AI 目录（main.py:127 已设此钩子）
```

**现实约束（需知晓）**：停止模拟器后，HA 自身需要数秒（mqtt 集成的 broker 断连检测）才会把这 12 个实体标记为 `unavailable`。本步骤只能消除 **Aether 自身** 的 5s + 60s 缓存延迟，让过滤在 HA 标记完成后立即生效；无法让 HA 更快标记。

### 4. 边界（明确不改的部分）

- **不读 `platform` 字段、不改 `_refresh_registry`**：白名单方案不需要识别平台。
- **不动 `get_states_snapshot()`**：自动化/规则引擎仍能看到真实状态（设备确实离线），只是设备**列表/目录**不显示。这样规则能优雅跳过离线设备，而非假装它不存在导致逻辑错乱。
- **不改系统提示话术**：AI 不获得任何「虚拟设备已关闭」提示。
- **不删 HA 实体注册表**：纯展示层过滤，实体在 HA 里始终存在，只是 Aether 的视图里不显示。

## 边界情形

| 情形 | 行为 |
|------|------|
| 白名单为空 / 配置缺失 | 不隐藏任何设备（特性关闭） |
| 白名单实体未在 HA 注册（不在 states） | 不参与「全部离线」判断，忽略 |
| 12 个中部分在线、部分离线 | 一个都不隐藏（仅全部离线才触发） |
| 模拟器重启、设备恢复在线 | 下次拉取自动重新出现 |
| 真实设备掉线 | 不受影响（不在白名单） |

## 测试

新增 `tests/test_ha_service_virtual_suppress.py`（或并入既有 HAService 测试），覆盖 `_virtual_suppress_set` + `get_all_devices` 过滤：

1. 白名单 12 个全 `unavailable` → `get_all_devices` 返回结果不含任何白名单实体。
2. 白名单中 1 个 `on`、其余 `unavailable` → 全部保留。
3. `simulator.entity_ids` 为空 / 缺省 → 不影响任何实体。
4. 白名单实体不在 states（未注册）→ 忽略它，其余全 `unavailable` 仍触发隐藏。

`get_all_devices_grouped` 同样验证一组（确保虚拟设备分组也不出现）。

mock 方式：构造 `states` 列表 + monkeypatch `get_config`，直接调用方法，不依赖 HA / Docker。

## 不在本次范围

- 真实 mqtt 设备的区分（当前部署无此场景；白名单方案天然规避）。
- 设备级「手动隐藏」开关（用户要的是模拟器联动，非手动逐个隐藏）。
- HA 实体注册表的增删（纯展示层过滤，不动 HA 数据）。
