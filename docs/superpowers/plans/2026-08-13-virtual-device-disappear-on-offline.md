# 虚拟设备「离线即消失」实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 关闭虚拟设备（模拟器）后，其 12 个 mqtt 设备从 AI 设备目录、`get_entities` 工具结果、`/halist` 设备列表三处同时彻底消失；重新开启后自动恢复。

**Architecture:** 所有设备视图最终都流经 `HAService.get_all_devices()` / `get_all_devices_grouped()` 两个方法。在那一处加一条「白名单实体全部 unavailable 才整体隐藏」的过滤规则，三处视图自动同步。配置一份 `simulator.entity_ids` 白名单精确识别模拟器设备（不依赖 platform 假设）。停/启模拟器后顺带失效缓存 + 即时重建目录，消除 Aether 自身缓存延迟。

**Tech Stack:** Python 3 / FastAPI / pytest（后端）；config.json（配置）；Docker Compose（部署验证）。

**Spec:** `docs/superpowers/specs/2026-08-13-virtual-device-disappear-on-offline-design.md`

## Global Constraints

- 识别模拟器设备**只用** `simulator.entity_ids` 白名单，不读 `platform` 字段。
- 触发隐藏的条件是白名单中**当前存在的实体全部** `state ∈ {"unavailable","unknown"}`；部分在线则一个都不隐藏。
- **不改** `get_states_snapshot()`、**不改**系统提示话术、**不删** HA 实体注册表。
- 测试用 `pytest` + `unittest.mock`，按 `tests/test_ha_service.py` 既有风格（`_make_service` + 直接设缓存字段 + `@pytest.mark.asyncio`）。
- 频繁提交，每个 Task 结束一次 commit。

---

## File Structure

- **Modify** `config.json` — 新增顶层 `simulator.entity_ids` 白名单（12 个实体 ID）。
- **Modify** `app/services/ha_service.py` — 加 `get_config` 导入；新增 `_virtual_suppress_set()` 方法；在 `get_all_devices()` 与 `get_all_devices_grouped()` 中应用过滤。
- **Modify** `app/routes/simulator_routes.py` — `simulator_stop`/`simulator_start` 加 `Depends(get_container)`，成功后失效缓存 + 触发目录刷新。
- **Modify** `tests/test_ha_service.py` — 新增 `TestVirtualSuppress` 测试类（复用既有 `_make_service`）。
- **Modify** `tests/test_simulator_routes.py` — 新增「停/启后刷新设备视图」测试。

> 说明：spec 写的是新建 `tests/test_ha_service_virtual_suppress.py`；本计划改为并入既有 `tests/test_ha_service.py`，复用其 `_make_service` helper，更内聚。

---

## Task 1: 配置白名单 + `_virtual_suppress_set` 核心逻辑（TDD）

**Files:**
- Modify: `config.json`（新增顶层 `simulator` 段）
- Modify: `app/services/ha_service.py:6-8`（加导入）、新增方法（紧邻 `get_all_devices` 之前，约 line 185）
- Test: `tests/test_ha_service.py`（新增 `TestVirtualSuppress` 类）

**Interfaces:**
- Produces: `HAService._virtual_suppress_set(self, states_by_id: dict[str, dict]) -> set[str]` —— 返回应隐藏的 entity_id 集合。被 Task 2、Task 3 调用。
- Consumes: `app.services.ha_service.get_config("simulator.entity_ids", [])`（本 Task 一并接入导入）。

- [ ] **Step 1: 给 config.json 加白名单**

在 `config.json` 顶层加一段（与 `weather`/`stt` 等同级）。完整内容：

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

- [ ] **Step 2: 写失败测试**

在 `tests/test_ha_service.py` 末尾新增：

```python
class TestVirtualSuppress:
    """模拟器设备「全部离线才隐藏」过滤规则。"""

    SIM_IDS = [
        "light.chuang_tou_deng", "climate.zhong_yang_kong_diao",
        "cover.ke_ting_chuang_lian", "sensor.ke_ting_wen_du",
    ]

    def _states_by_id(self, overrides: dict[str, str]) -> dict[str, dict]:
        """构造 states_by_id：默认全部 on，overrides 覆盖指定 entity 的 state。"""
        return {
            eid: {"entity_id": eid, "state": overrides.get(eid, "on"), "attributes": {}}
            for eid in self.SIM_IDS
        }

    def test_all_unavailable_suppresses_all(self, monkeypatch):
        """白名单全 unavailable → 全部隐藏。"""
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: self.SIM_IDS if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        states = self._states_by_id({eid: "unavailable" for eid in self.SIM_IDS})
        assert svc._virtual_suppress_set(states) == set(self.SIM_IDS)

    def test_partial_online_suppresses_none(self, monkeypatch):
        """有 1 个在线 → 一个都不隐藏。"""
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: self.SIM_IDS if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        states = self._states_by_id({"light.chuang_tou_deng": "on"})  # 其余 unavailable
        assert svc._virtual_suppress_set(states) == set()

    def test_empty_whitelist_suppresses_none(self, monkeypatch):
        """白名单为空 → 不隐藏任何设备（特性关闭）。"""
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: [] if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        states = self._states_by_id({eid: "unavailable" for eid in self.SIM_IDS})
        assert svc._virtual_suppress_set(states) == set()

    def test_unregistered_entity_ignored(self, monkeypatch):
        """白名单实体不在 states（未注册）→ 忽略它；其余全 unavailable 仍触发。"""
        only_two = ["light.chuang_tou_deng", "climate.zhong_yang_kong_diao"]
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: self.SIM_IDS if path == "simulator.entity_ids" else default,
        )
        svc, _ = _make_service([])
        # states 里只有 2 个，且都 unavailable
        states = self._states_by_id({})
        states = {eid: s for eid, s in states.items() if eid in only_two}
        for s in states.values():
            s["state"] = "unavailable"
        assert svc._virtual_suppress_set(states) == set(only_two)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `python -m pytest tests/test_ha_service.py::TestVirtualSuppress -v`
Expected: FAIL（`AttributeError: 'HAService' object has no attribute '_virtual_suppress_set'`，且 `get_config` 尚未导入）

- [ ] **Step 4: 加导入**

`app/services/ha_service.py` 第 8 行 `from ..clients.ha_client import HomeAssistantClient` 之后新增一行：

```python
from ..core.config import get_config
```

- [ ] **Step 5: 实现 `_virtual_suppress_set`**

在 `app/services/ha_service.py` 中、`_DEVICE_DOMAINS` 定义（约 line 186-190）之后、`get_all_devices`（约 line 192）之前，新增方法：

```python
    def _virtual_suppress_set(self, states_by_id: dict[str, dict]) -> set[str]:
        """返回应隐藏的模拟器实体集合。

        规则：配置白名单(simulator.entity_ids)中当前存在的实体若【全部】
        unavailable/unknown → 返回全部；否则返回空集。
        匹配「全部离线才触发」语义。白名单为空则特性关闭。
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

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_ha_service.py::TestVirtualSuppress -v`
Expected: 4 passed

- [ ] **Step 7: 确认未破坏既有测试**

Run: `python -m pytest tests/test_ha_service.py -v`
Expected: 全部 passed（既有 `TestHAService`、`TestInvalidateStatesCache` 不受影响）

- [ ] **Step 8: 提交**

```bash
git add config.json app/services/ha_service.py tests/test_ha_service.py
git commit -m "feat(ha): 模拟器设备「全部离线才隐藏」过滤规则 + 白名单配置"
```

---

## Task 2: 在 `get_all_devices()` 应用过滤（TDD）

**Files:**
- Modify: `app/services/ha_service.py:192-220`（`get_all_devices`）
- Test: `tests/test_ha_service.py`（`TestVirtualSuppress` 类内新增集成测试）

**Interfaces:**
- Consumes: `HAService._virtual_suppress_set`（Task 1 产出）
- Produces: `get_all_devices()` 返回值不再包含被隐藏实体（供 `/ha/entities` flat 视图、`get_entities` 工具消费）

- [ ] **Step 1: 写失败测试**

在 `tests/test_ha_service.py` 的 `TestVirtualSuppress` 类末尾追加（异步，走真实 `get_all_devices` 路径）：

```python
    @pytest.mark.asyncio
    async def test_flat_devices_excluded_when_all_offline(self, monkeypatch):
        """get_all_devices：模拟器全 offline 时，flat 列表不含模拟器实体。"""
        sim_ids = ["light.chuang_tou_deng", "climate.zhong_yang_kong_diao"]
        real_id = "light.real_bed"
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: sim_ids if path == "simulator.entity_ids" else default,
        )
        devices = [
            {"entity_id": "light.chuang_tou_deng", "state": "unavailable", "attributes": {}},
            {"entity_id": "climate.zhong_yang_kong_diao", "state": "unavailable", "attributes": {}},
            {"entity_id": "light.real_bed", "state": "on", "attributes": {}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {"bedroom": "Bedroom"}
        svc._entity_area_map = {eid: "bedroom" for eid in sim_ids + [real_id]}
        svc._area_cache_at = 9999999999

        result = await svc.get_all_devices()
        ids = [d["entity_id"] for d in result]
        assert ids == ["light.real_bed"]  # 模拟器两个被隐藏，真实设备保留

    @pytest.mark.asyncio
    async def test_flat_devices_kept_when_partial_online(self, monkeypatch):
        """get_all_devices：模拟器有 1 个在线时，全部保留。"""
        sim_ids = ["light.chuang_tou_deng", "climate.zhong_yang_kong_diao"]
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: sim_ids if path == "simulator.entity_ids" else default,
        )
        devices = [
            {"entity_id": "light.chuang_tou_deng", "state": "on", "attributes": {}},
            {"entity_id": "climate.zhong_yang_kong_diao", "state": "unavailable", "attributes": {}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {"bedroom": "Bedroom"}
        svc._entity_area_map = {eid: "bedroom" for eid in sim_ids}
        svc._area_cache_at = 9999999999

        result = await svc.get_all_devices()
        ids = [d["entity_id"] for d in result]
        assert set(ids) == set(sim_ids)  # 一个都不隐藏
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ha_service.py::TestVirtualSuppress::test_flat_devices_excluded_when_all_offline tests/test_ha_service.py::TestVirtualSuppress::test_flat_devices_kept_when_partial_online -v`
Expected: FAIL（当前 `get_all_devices` 未过滤，模拟器实体仍出现）

- [ ] **Step 3: 改 `get_all_devices`**

`app/services/ha_service.py` 的 `get_all_devices` 方法，把：

```python
        states = await self._get_states_cached()
        area_map, entity_area_map = await self._get_area_maps_cached()
        alias_map = await self._get_alias_map()
        devices = []
        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
```

改为：

```python
        states = await self._get_states_cached()
        states_by_id = {s["entity_id"]: s for s in states}
        suppress = self._virtual_suppress_set(states_by_id)
        area_map, entity_area_map = await self._get_area_maps_cached()
        alias_map = await self._get_alias_map()
        devices = []
        for state in states:
            entity_id = state["entity_id"]
            if entity_id in suppress:
                continue
            domain = entity_id.split(".")[0]
```

（仅新增 `states_by_id` / `suppress` 两行 + 循环内 3 行 `if entity_id in suppress: continue`，其余完全不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ha_service.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/services/ha_service.py tests/test_ha_service.py
git commit -m "feat(ha): get_all_devices 应用模拟器离线隐藏过滤"
```

---

## Task 3: 在 `get_all_devices_grouped()` 应用过滤（TDD）

**Files:**
- Modify: `app/services/ha_service.py:222-256`（`get_all_devices_grouped` 的 `by_id` 收集段）
- Test: `tests/test_ha_service.py`（`TestVirtualSuppress` 类内新增 grouped 集成测试）

**Interfaces:**
- Consumes: `HAService._virtual_suppress_set`（Task 1 产出）
- Produces: `get_all_devices_grouped()` 返回的 `devices[]` 不含被隐藏实体（供 AI 系统提示目录、`/ha/entities` grouped 视图消费）

- [ ] **Step 1: 写失败测试**

在 `tests/test_ha_service.py` 的 `TestVirtualSuppress` 类末尾追加：

```python
    @pytest.mark.asyncio
    async def test_grouped_devices_excluded_when_all_offline(self, monkeypatch):
        """get_all_devices_grouped：模拟器全 offline 时，分组视图也不含模拟器。"""
        sim_ids = ["light.chuang_tou_deng", "cover.ke_ting_chuang_lian"]
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: sim_ids if path == "simulator.entity_ids" else default,
        )
        devices = [
            {"entity_id": "light.chuang_tou_deng", "state": "unavailable", "attributes": {"friendly_name": "床头灯"}},
            {"entity_id": "cover.ke_ting_chuang_lian", "state": "unavailable", "attributes": {"friendly_name": "窗帘"}},
        ]
        svc, _ = _make_service(devices)
        # grouped 用 _get_full_registry → _refresh_registry；把缓存置新鲜避免 WS
        svc._area_map = {"bedroom": "卧室"}
        svc._entity_area_map = {eid: "bedroom" for eid in sim_ids}
        svc._device_info_map = {}
        svc._entity_device_map = {}
        svc._registry_cache_at = 9999999999

        result = await svc.get_all_devices_grouped()
        all_entity_ids = [
            eid for dev in result.get("devices", []) for eid in dev.get("entity_ids", [])
        ]
        assert all_entity_ids == []  # 全隐藏

    @pytest.mark.asyncio
    async def test_grouped_devices_kept_when_partial_online(self, monkeypatch):
        sim_ids = ["light.chuang_tou_deng", "cover.ke_ting_chuang_lian"]
        monkeypatch.setattr(
            "app.services.ha_service.get_config",
            lambda path, default=None: sim_ids if path == "simulator.entity_ids" else default,
        )
        devices = [
            {"entity_id": "light.chuang_tou_deng", "state": "on", "attributes": {"friendly_name": "床头灯"}},
            {"entity_id": "cover.ke_ting_chuang_lian", "state": "unavailable", "attributes": {"friendly_name": "窗帘"}},
        ]
        svc, _ = _make_service(devices)
        svc._area_map = {"bedroom": "卧室"}
        svc._entity_area_map = {eid: "bedroom" for eid in sim_ids}
        svc._device_info_map = {}
        svc._entity_device_map = {}
        svc._registry_cache_at = 9999999999

        result = await svc.get_all_devices_grouped()
        all_entity_ids = [
            eid for dev in result.get("devices", []) for eid in dev.get("entity_ids", [])
        ]
        assert set(all_entity_ids) == set(sim_ids)  # 一个都不隐藏
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ha_service.py::TestVirtualSuppress::test_grouped_devices_excluded_when_all_offline tests/test_ha_service.py::TestVirtualSuppress::test_grouped_devices_kept_when_partial_online -v`
Expected: FAIL（grouped 未过滤，模拟器实体仍出现）

- [ ] **Step 3: 改 `get_all_devices_grouped` 的 `by_id` 收集段**

`app/services/ha_service.py` 的 `get_all_devices_grouped`，把开头的：

```python
        states = await self._get_states_cached()
        reg = await self._get_full_registry()
        area_map = reg["area_map"]
        device_info_map = reg["device_info_map"]
        entity_device_map = reg["entity_device_map"]
        alias_map = await self._get_alias_map()

        # 先把白名单+area 过滤后的实体收集成 {entity_id: state_dict}
        by_id: dict[str, dict] = {}
        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".")[0]
            if domain not in self._DEVICE_DOMAINS:
                continue
```

改为：

```python
        states = await self._get_states_cached()
        states_by_id = {s["entity_id"]: s for s in states}
        suppress = self._virtual_suppress_set(states_by_id)
        reg = await self._get_full_registry()
        area_map = reg["area_map"]
        device_info_map = reg["device_info_map"]
        entity_device_map = reg["entity_device_map"]
        alias_map = await self._get_alias_map()

        # 先把白名单+area 过滤后的实体收集成 {entity_id: state_dict}
        by_id: dict[str, dict] = {}
        for state in states:
            entity_id = state["entity_id"]
            if entity_id in suppress:
                continue
            domain = entity_id.split(".")[0]
            if domain not in self._DEVICE_DOMAINS:
                continue
```

（仅新增 `states_by_id` / `suppress` 两行 + 循环内 3 行 continue，其余不变。）

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ha_service.py -v`
Expected: 全部 passed

- [ ] **Step 5: 提交**

```bash
git add app/services/ha_service.py tests/test_ha_service.py
git commit -m "feat(ha): get_all_devices_grouped 应用模拟器离线隐藏过滤"
```

---

## Task 4: 停/启模拟器后即时刷新设备视图（TDD）

**Files:**
- Modify: `app/routes/simulator_routes.py:17-18`（导入）、`104-131`（`simulator_stop`/`simulator_start` + 新增 `_refresh_device_views`）
- Test: `tests/test_simulator_routes.py`（新增刷新测试）

**Interfaces:**
- Consumes: `app.container.get_container` / `AppContainer`；`container.ha_service.invalidate_states_cache()`；`container.catalog_refresh_fn()`（main.py:127 设的钩子，返回 coroutine）。
- Produces: `simulator_stop`/`simulator_start` 在成功后即时刷新；`_refresh_device_views(container)` helper。

> 注：路由用 `getattr` 守卫访问 container 字段。直接调用测试（既有 4 个）不传 container 时，`container` 取默认值 `Depends(...)` 对象，`getattr(..., "ha_service", None)` 返回 None → 跳过刷新，**既有测试无需改动仍通过**。

- [ ] **Step 1: 写失败测试**

在 `tests/test_simulator_routes.py` 末尾的 `TestSimulatorStopStart` 类里追加：

```python
    @pytest.mark.asyncio
    async def test_stop_refreshes_device_views(self):
        """stop 成功后应失效 HAService 状态缓存并触发 AI 目录刷新。"""
        from unittest.mock import AsyncMock, MagicMock

        async def fake_action(name, action):
            return {"available": True, "ok": True}

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            await simulator_routes.simulator_stop(container=container)

        container.ha_service.invalidate_states_cache.assert_called_once()
        container.catalog_refresh_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_refreshes_device_views(self):
        """start 成功后同样刷新设备视图。"""
        from unittest.mock import AsyncMock, MagicMock

        async def fake_action(name, action):
            return {"available": True, "ok": True}

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            await simulator_routes.simulator_start(container=container)

        container.ha_service.invalidate_states_cache.assert_called_once()
        container.catalog_refresh_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_partial_failure_skips_refresh(self):
        """部分失败（ok=False）时不刷新（避免用半同步状态重建目录）。"""
        from unittest.mock import AsyncMock, MagicMock

        async def fake_action(name, action):
            return {"available": True, "ok": False, "error": "HTTP 500"}

        container = MagicMock()
        container.ha_service.invalidate_states_cache = MagicMock()
        container.catalog_refresh_fn = AsyncMock()

        with patch.object(simulator_routes, "docker_socket_available", return_value=True), \
             patch.object(simulator_routes, "_container_action", side_effect=fake_action):
            await simulator_routes.simulator_stop(container=container)

        container.ha_service.invalidate_states_cache.assert_not_called()
        container.catalog_refresh_fn.assert_not_awaited()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_simulator_routes.py::TestSimulatorStopStart::test_stop_refreshes_device_views tests/test_simulator_routes.py::TestSimulatorStopStart::test_start_refreshes_device_views tests/test_simulator_routes.py::TestSimulatorStopStart::test_stop_partial_failure_skips_refresh -v`
Expected: FAIL（`simulator_stop` 当前不接受 `container` 关键字参数 → `TypeError`）

- [ ] **Step 3: 加导入**

`app/routes/simulator_routes.py` 把：

```python
from fastapi import APIRouter
```

改为：

```python
from fastapi import APIRouter, Depends
```

并在 `from ..core.api_models import ApiResponse` 之后新增：

```python
from ..container import AppContainer, get_container
```

- [ ] **Step 4: 加 `_refresh_device_views` helper**

在 `app/routes/simulator_routes.py` 的 `_container_action` 函数之后、`@router.get("/simulator/status")` 之前，新增：

```python
async def _refresh_device_views(container: AppContainer) -> None:
    """停/启模拟器后，立即失效 HAService 状态缓存并重建 AI 设备目录，
    让「离线即消失」过滤尽快反映。

    仅消除 Aether 自身的 5s/60s 缓存延迟；HA 把 mqtt 实体标 unavailable
    仍需数秒（broker 断连检测），那部分无法加速。失败不阻塞响应。
    用 getattr 守卫，便于路由被直接调用（测试）时优雅降级。
    """
    try:
        ha_service = getattr(container, "ha_service", None)
        if ha_service is not None and hasattr(ha_service, "invalidate_states_cache"):
            ha_service.invalidate_states_cache()
        refresh_fn = getattr(container, "catalog_refresh_fn", None)
        if refresh_fn is not None:
            await refresh_fn()
    except Exception as exc:  # noqa: BLE001
        logger.warning("刷新设备视图失败: %s", exc)
```

- [ ] **Step 5: 改 `simulator_stop`**

`app/routes/simulator_routes.py` 把整个 `simulator_stop` 替换为：

```python
@router.post("/simulator/stop")
async def simulator_stop(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """停止虚拟设备模拟器和 mosquitto（设备全部下线）。"""
    if not docker_socket_available():
        return ApiResponse(code="unavailable", message="Docker socket 不可用", data={"ok": False})
    sim = await _container_action(SIMULATOR_CONTAINER, "stop")
    mqtt = await _container_action(MQTT_CONTAINER, "stop")
    ok = bool(sim.get("ok")) and bool(mqtt.get("ok"))
    if ok:
        await _refresh_device_views(container)
    return ApiResponse(
        code="ok" if ok else "partial",
        message="已停止" if ok else "部分失败",
        data={"ok": ok, "simulator": sim, "mqtt": mqtt},
    )
```

- [ ] **Step 6: 改 `simulator_start`**

把整个 `simulator_start` 替换为：

```python
@router.post("/simulator/start")
async def simulator_start(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """启动 mosquitto 和虚拟设备模拟器（先 broker 后模拟器）。"""
    if not docker_socket_available():
        return ApiResponse(code="unavailable", message="Docker socket 不可用", data={"ok": False})
    mqtt = await _container_action(MQTT_CONTAINER, "start")
    sim = await _container_action(SIMULATOR_CONTAINER, "start")
    ok = bool(mqtt.get("ok")) and bool(sim.get("ok"))
    if ok:
        await _refresh_device_views(container)
    return ApiResponse(
        code="ok" if ok else "partial",
        message="已启动" if ok else "部分失败",
        data={"ok": ok, "simulator": sim, "mqtt": mqtt},
    )
```

- [ ] **Step 7: 运行测试确认通过（含既有用例不回归）**

Run: `python -m pytest tests/test_simulator_routes.py -v`
Expected: 全部 passed（新增 3 个 + 既有 8 个，共 11 个）

- [ ] **Step 8: 提交**

```bash
git add app/routes/simulator_routes.py tests/test_simulator_routes.py
git commit -m "feat(simulator): 停/启后即时失效缓存+重建目录，加速离线消失"
```

---

## Task 5: 重建镜像 + 手动验证

**Files:** 无代码改动（部署验证）

> 此 rebuild 同时让此前修复的 simulator_routes 两个 bug（同步 transport + v1.41 版本号）生效——「虚拟设备」开关按钮也会首次正常出现。

- [ ] **Step 1: 跑全部相关测试**

Run: `python -m pytest tests/test_ha_service.py tests/test_simulator_routes.py -v`
Expected: 全部 passed

- [ ] **Step 2: 重建 aether 镜像并重建容器**

Run: `docker compose up -d --build aether`
Expected: 镜像构建成功，容器重建为 `Up` 状态（短暂重启）

- [ ] **Step 3: 验证开关按钮已出现**

浏览器进入「高级」页 →「虚拟设备」分区：右侧应出现拨动开关，显示「当前：运行中」。
（若仍显示「需 Docker 部署…」→ docker.sock 未挂载或 transport 修复未生效，回头查 Task 4 导入与 rebuild。）

- [ ] **Step 4: 验证「关闭即消失」**

1. 点开关关闭虚拟设备，等约 5–15 秒（HA 标 mqtt 实体 unavailable）。
2. 打开 `/halist` 设备列表：模拟器的灯/空调/窗帘/风扇/插座/温湿度应**全部消失**；真实设备（小米等）仍在。
3. 在聊天里问 AI「把卧室床头灯打开」：AI 应回「没找到该设备」之类（目录里已无此设备）。

- [ ] **Step 5: 验证「重新开启即恢复」**

1. 点开关重新开启虚拟设备，等约 5–15 秒。
2. `/halist` 列表：模拟器设备应**重新出现**且在线。
3. AI 能再次识别并控制这些设备。

- [ ] **Step 6: 提交验证记录（可选）**

无需代码提交。若验证中发现问题，回到对应 Task 修复。

---

## Self-Review（计划 vs spec）

- **配置白名单（spec §1）** → Task 1 Step 1 ✓
- **`_virtual_suppress_set` 规则（spec §2）** → Task 1 Step 5 ✓（全 unavailable→全隐藏；部分在线→空集；空白名单→空集）
- **插入点 get_all_devices（spec §2①）** → Task 2 ✓
- **插入点 get_all_devices_grouped（spec §2②）** → Task 3 ✓
- **即时刷新（spec §3）** → Task 4 ✓（invalidate_states_cache + catalog_refresh_fn，仅成功路径）
- **边界不动（spec §4）** → 计划未改 `get_states_snapshot`、未改提示话术、未删注册表 ✓
- **边界情形（spec 表）** → Task 1 测试覆盖 全离线/部分在线/空白名单/未注册 四种 ✓
- **测试（spec §测试）** → Task 1–4 共 4+2+2+3 个测试 ✓
- **占位符扫描**：无 TBD/TODO，所有代码块完整 ✓
- **类型一致**：`_virtual_suppress_set(states_by_id: dict[str, dict]) -> set[str]` 在 Task 1 定义，Task 2/3 一致调用 ✓
