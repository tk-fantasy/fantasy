# 设备语义映射（/semantics）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给设备配代码级动作映射（如门禁继电器 turn_on↔turn_off），call_service 无条件翻转 service，结果反馈带描述，对称翻转对自动翻转 state，AI 凭直觉调用即可正确操作。

**Architecture:** 复用 `emoji_preferences` 表新 scope `entity_action_map`（per-entity JSON）。新增 `semantic_map.py` 集中映射逻辑（缓存 + 翻转 helper）。`call_service` 执行前过滤替换 service，返回时附 `semantic_mapping`。`_refresh_ha_catalog` 和 `get_entities` 预翻转 state。前端 `/semantics` 页两层结构（实体→服务）。

**Tech Stack:** Python 3 / FastAPI / Pydantic / pytest（后端），Vue 3 Composition API + `<script setup>`（前端）。

## Global Constraints

- **Python 解释器**：`C:\Users\26658\.conda\envs\learning\python.exe`（Git Bash 下 `/c/Users/26658/.conda/envs/learning/python.exe`），系统 `python` 是 Windows Store stub。
- **pytest 运行**：`PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest <path> -v`。**不要**用管道（`| tail`/`| tee`）会挂起。
- **导入风格**：`app/tools.py` 在 `app/` 包下，导入同包用单点 `from .core.database import Database`（双点会 ImportError 被 except 吞掉）。
- **Docker**：代码非 volume 挂载，改代码后 `docker compose build aether && docker compose up -d aether`。
- **DB 写原语**：`emoji_pref_upsert(scope, key, value)` / `emoji_pref_delete(scope, key)`，无 `prefs_set`。
- **scope 命名**：`entity_action_map`（新）。
- **前端 API helper**：`apiGet(url)` / `apiPut(url, body)`（`frontend/src/utils/api.js`），自动解包 `json.data`。
- **侧边栏 icon**：HTML 实体字符串，`v-html` 渲染。
- **测试 DB fixture**：复用 `tests/test_call_service_operable.py` 的 `_init_db` autouse fixture 模式（`Database._instance = None` + `monkeypatch DB_PATH` + `Database.init()`）。
- **GLM 模型**：用户配的 `glm-4-flash`，从 env 读，不要用 Ollama。

---

## File Structure

| 文件 | 职责 | 操作 |
|------|------|------|
| `app/services/semantic_map.py` | 映射缓存 + 翻转 helper（get_action_map / is_flipped_pair / apply_state_flip / flip_state_value / invalidate_cache） | **新建** |
| `tests/test_semantic_map.py` | semantic_map 模块单元测试 | **新建** |
| `app/schema/api_schemas.py` | `ActionMapRequest` schema | 修改（追加，约 L300 后） |
| `app/routes/ha_routes.py` | GET/PUT `/ha/action-maps` + GET `/ha/entity-services` | 修改（追加路由，约 L183 后） |
| `tests/test_action_maps_route.py` | API 路由测试 | **新建** |
| `app/tools.py` | call_service 过滤层 + 结果反馈；get_entities 状态翻转 | 修改（L343 前 + L354 return + L131-134） |
| `tests/test_call_service_semantic_map.py` | call_service 过滤层测试 | **新建** |
| `app/main.py` | `_refresh_ha_catalog` 预翻转 state | 修改（L281-285 循环内） |
| `frontend/src/router/index.js` | `/semantics` 路由 | 修改（routes 数组追加） |
| `frontend/src/components/SidebarNav.vue` | 侧边栏入口 | 修改（navItems 追加） |
| `frontend/src/views/SemanticsView.vue` | 语义映射配置页 | **新建** |

---

### Task 1: semantic_map.py 核心模块 + 单元测试

**Files:**
- Create: `app/services/semantic_map.py`
- Test: `tests/test_semantic_map.py`

**Interfaces:**
- Produces: `get_action_map(entity_id) -> dict|None`（async，带缓存）
- Produces: `is_flipped_pair(mappings) -> bool`（同步）
- Produces: `apply_state_flip(new_state, entity_id) -> dict`（同步，用缓存）
- Produces: `flip_state_value(entity_id, state) -> str`（async，触发缓存加载）
- Produces: `invalidate_cache() -> None`（同步）

- [ ] **Step 1: 写失败测试（缓存 + get_action_map）**

创建 `tests/test_semantic_map.py`：

```python
"""Tests for semantic_map module."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


@pytest.mark.asyncio
async def test_get_action_map_returns_none_when_no_config(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.services.semantic_map import get_action_map, invalidate_cache
    invalidate_cache()
    assert await get_action_map("switch.gate") is None


@pytest.mark.asyncio
async def test_get_action_map_returns_config(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    payload = json.dumps({"mappings": {"turn_on": {"target": "turn_off", "description": "d1"}}})
    await Database.get().emoji_pref_upsert("entity_action_map", "switch.gate", payload)
    from app.services.semantic_map import get_action_map, invalidate_cache
    invalidate_cache()
    result = await get_action_map("switch.gate")
    assert result is not None
    assert result["mappings"]["turn_on"]["target"] == "turn_off"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_semantic_map.py::test_get_action_map_returns_config -v`
Expected: FAIL（ModuleNotFoundError: app.services.semantic_map）

- [ ] **Step 3: 实现 semantic_map.py 的缓存 + get_action_map**

创建 `app/services/semantic_map.py`：

```python
"""设备动作语义映射 — 代码级无条件翻转 service + state 隐含跟随。

核心思路：AI 凭直觉调用（稳定一致），call_service 执行前无条件替换 service，
结果反馈带描述让 AI 正确汇报。对称翻转对（turn_on↔turn_off）自动翻转 state。
映射规则不进提示词（防双重错误），真实解释放结果反馈里。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# 进程内缓存：{entity_id: {mappings: {...}}}。写入时 invalidate，下次读取重载。
_cache: dict[str, dict] = {}
_cache_loaded = False


async def _reload_cache() -> None:
    global _cache_loaded
    try:
        from ..core.database import Database
        raw = await Database.get().prefs_get_by_scope("entity_action_map")
        parsed: dict[str, dict] = {}
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
    except Exception:  # noqa: BLE001
        logger.warning("动作映射缓存加载失败", exc_info=True)


async def get_action_map(entity_id: str) -> dict | None:
    """读取某实体的动作映射。带进程内缓存。DB 异常返回 None 放行。"""
    global _cache_loaded
    if not _cache_loaded:
        await _reload_cache()
    return _cache.get(entity_id)


def invalidate_cache() -> None:
    """写入后调用，下次 get_action_map 时重新加载。"""
    global _cache_loaded
    _cache_loaded = False
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_semantic_map.py -v`
Expected: 2 passed

- [ ] **Step 5: 写翻转 helper 测试（is_flipped_pair / apply_state_flip / flip_state_value）**

追加到 `tests/test_semantic_map.py`：

```python
def test_is_flipped_pair_true():
    from app.services.semantic_map import is_flipped_pair
    m = {"turn_on": {"target": "turn_off"}, "turn_off": {"target": "turn_on"}}
    assert is_flipped_pair(m) is True


def test_is_flipped_pair_one_side_false():
    from app.services.semantic_map import is_flipped_pair
    m = {"turn_on": {"target": "turn_off"}}
    assert is_flipped_pair(m) is False


def test_is_flipped_pair_empty():
    from app.services.semantic_map import is_flipped_pair
    assert is_flipped_pair({}) is False


@pytest.mark.asyncio
async def test_apply_state_flip_flips_on_off(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    payload = json.dumps({"mappings": {
        "turn_on": {"target": "turn_off"}, "turn_off": {"target": "turn_on"}}})
    await Database.get().emoji_pref_upsert("entity_action_map", "switch.gate", payload)
    from app.services.semantic_map import get_action_map, apply_state_flip, invalidate_cache
    invalidate_cache()
    await get_action_map("switch.gate")  # warm cache
    assert apply_state_flip({"state": "on", "attributes": {}}, "switch.gate")["state"] == "off"
    assert apply_state_flip({"state": "off", "attributes": {}}, "switch.gate")["state"] == "on"


@pytest.mark.asyncio
async def test_apply_state_flip_passthrough_non_flipped(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.services.semantic_map import apply_state_flip, invalidate_cache
    invalidate_cache()
    ns = {"state": "on", "attributes": {}}
    assert apply_state_flip(ns, "switch.none") == ns


@pytest.mark.asyncio
async def test_flip_state_value_async(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    payload = json.dumps({"mappings": {
        "turn_on": {"target": "turn_off"}, "turn_off": {"target": "turn_on"}}})
    await Database.get().emoji_pref_upsert("entity_action_map", "switch.gate", payload)
    from app.services.semantic_map import flip_state_value, invalidate_cache
    invalidate_cache()
    assert await flip_state_value("switch.gate", "on") == "off"
    assert await flip_state_value("switch.gate", "off") == "on"
    assert await flip_state_value("switch.gate", "unavailable") == "unavailable"
    assert await flip_state_value("switch.none", "on") == "on"
```

- [ ] **Step 6: 运行测试确认失败**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_semantic_map.py -v`
Expected: FAIL（is_flipped_pair / apply_state_flip / flip_state_value 不存在）

- [ ] **Step 7: 实现翻转 helper**

追加到 `app/services/semantic_map.py`：

```python
def is_flipped_pair(mappings: dict) -> bool:
    """检测对称翻转对：turn_on→turn_off 且 turn_off→turn_on 同时存在。"""
    def target_of(svc: str) -> str | None:
        e = mappings.get(svc)
        return e.get("target") if isinstance(e, dict) else None
    return (target_of("turn_on") == "turn_off"
            and target_of("turn_off") == "turn_on")


def apply_state_flip(new_state: dict, entity_id: str) -> dict:
    """对称翻转对设备：把 new_state.state on↔off 反转。非翻转设备原样返回。

    同步版，用于 call_service 返回时（缓存已被同次调用的 get_action_map 预热）。
    """
    am = _cache.get(entity_id)
    if not am or not is_flipped_pair(am.get("mappings", {})):
        return new_state
    s = new_state.get("state")
    if s == "on":
        return {**new_state, "state": "off"}
    if s == "off":
        return {**new_state, "state": "on"}
    return new_state


async def flip_state_value(entity_id: str, state: str) -> str:
    """供状态读取点调用（get_entities / catalog）。翻转 on/off。"""
    am = await get_action_map(entity_id)
    if am and is_flipped_pair(am.get("mappings", {})):
        if state == "on":
            return "off"
        if state == "off":
            return "on"
    return state
```

- [ ] **Step 8: 运行测试确认全通过**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_semantic_map.py -v`
Expected: 8 passed

- [ ] **Step 9: 提交**

```bash
cd D:/Aether && git add app/services/semantic_map.py tests/test_semantic_map.py
git commit -m "feat(semantic-map): 动作映射缓存 + 对称翻转对 state 翻转 helper"
```

---

### Task 2: ActionMapRequest schema + REST API

**Files:**
- Modify: `app/schema/api_schemas.py`（追加到 EntityOperableRequest 后，约 L305）
- Modify: `app/routes/ha_routes.py`（追加路由，约 L183 后；import 行 L15 追加）
- Test: `tests/test_action_maps_route.py`

**Interfaces:**
- Consumes: Task 1 的 `semantic_map.invalidate_cache`
- Produces: `GET /ha/action-maps`、`PUT /ha/action-maps`、`GET /ha/entity-services`

- [ ] **Step 1: 写失败测试（PUT 后 GET 能读到）**

创建 `tests/test_action_maps_route.py`：

```python
"""Tests for /ha/action-maps and /ha/entity-services routes."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


def _container_with_services(services_return=None):
    from app.container import AppContainer
    from app.clients.ha_client import HomeAssistantClient
    c = AppContainer.__new__(AppContainer)
    c.ha_service = MagicMock()
    c.ha_service.get_service_defs = AsyncMock(return_value=services_return or {})
    # ha_client 是 @property（读 ha_client_ref[0]），不能直接 setattr，设 ha_client_ref
    c.ha_client_ref = [MagicMock(spec=HomeAssistantClient)]
    c.catalog_refresh_fn = None
    return c


@pytest.mark.asyncio
async def test_put_then_get_action_map(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.routes.ha_routes import get_action_maps, set_action_map
    # PUT
    services = {"switch": ["turn_on", "turn_off", "toggle"]}
    container = _container_with_services(services)
    payload = type("P", (), {"entity_id": "switch.gate", "mappings": {
        "turn_on": {"target": "turn_off", "description": "d1"},
        "turn_off": {"target": "turn_on", "description": "d2"},
    }})()
    put_res = await set_action_map(payload, container)
    assert put_res.data["entity_id"] == "switch.gate"
    # GET
    get_res = await get_action_maps()
    m = get_res.data["maps"]["switch.gate"]["mappings"]
    assert m["turn_on"]["target"] == "turn_off"
    assert m["turn_off"]["target"] == "turn_on"


@pytest.mark.asyncio
async def test_put_empty_mappings_deletes(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.routes.ha_routes import get_action_maps, set_action_map
    container = _container_with_services({"switch": ["turn_on", "turn_off"]})
    payload = type("P", (), {"entity_id": "switch.gate", "mappings": {
        "turn_on": {"target": "turn_off", "description": "d"}}})()
    await set_action_map(payload, container)
    # 清空
    payload_empty = type("P", (), {"entity_id": "switch.gate", "mappings": {}})()
    await set_action_map(payload_empty, container)
    get_res = await get_action_maps()
    assert "switch.gate" not in get_res.data["maps"]


@pytest.mark.asyncio
async def test_put_rejects_invalid_target(tmp_path, monkeypatch):
    from app.core.database import Database
    from app.core.exceptions import AppException
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.routes.ha_routes import set_action_map
    container = _container_with_services({"switch": ["turn_on", "turn_off"]})
    payload = type("P", (), {"entity_id": "switch.gate", "mappings": {
        "turn_on": {"target": "nonexistent", "description": "d"}}})()
    with pytest.raises(AppException):
        await set_action_map(payload, container)


@pytest.mark.asyncio
async def test_entity_services_route(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.routes.ha_routes import get_entity_services
    services = {"switch": {"turn_on": {"fields": ["entity_id"]},
                           "turn_off": {"fields": ["entity_id"]}}}
    container = _container_with_services(services)
    res = await get_entity_services(container)
    assert res.data["services"]["switch"] == ["turn_on", "turn_off"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_action_maps_route.py -v`
Expected: FAIL（import ha_routes 里的 set_action_map/get_action_maps 不存在）

- [ ] **Step 3: 加 ActionMapRequest schema**

在 `app/schema/api_schemas.py` 的 `EntityOperableRequest` 类后追加：

```python
class ActionMapRequest(BaseModel):
    """设置实体动作语义映射的请求体。

    mappings 形如 {"turn_on": {"target": "turn_off", "description": "继电器反转..."}}。
    target 必须属于该域（domain）的可用 services；target==源 service 无意义（被过滤丢弃）。
    空 mappings = 删除该实体的全部映射。写入后立即刷新 catalog 缓存。
    """

    entity_id: str = Field(..., description="HA 实体 ID")
    mappings: dict = Field(default_factory=dict, description="{svc: {target, description}}")

    @field_validator("entity_id", mode="before")
    @classmethod
    def _strip_entity_id(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) else str(v)
```

- [ ] **Step 4: 加路由（GET/PUT action-maps + GET entity-services）**

在 `app/routes/ha_routes.py` L15 import 行追加 `ActionMapRequest`：

```python
from ..schema.api_schemas import HAConfigRequest, HAServiceCallRequest, ModelTestRequest, UniqueSettingsRequest, EntityAliasRequest, EntityNoteRequest, EntityOperableRequest, ActionMapRequest
```

在 `set_entity_operable` 路由后（约 L183）追加三个路由：

```python
@router.get("/ha/action-maps")
async def get_action_maps() -> ApiResponse[dict]:
    """获取全部已配置的动作语义映射 {entity_id: {mappings: {...}}}。"""
    import json
    from ..core.database import Database
    db = Database.get()
    raw = await db.prefs_get_by_scope("entity_action_map")
    maps: dict[str, dict] = {}
    for eid, val in raw.items():
        try:
            obj = json.loads(val) if isinstance(val, str) else val
            if isinstance(obj, dict) and obj.get("mappings"):
                maps[eid] = obj
        except (ValueError, TypeError):
            logger.warning("action-maps 解析失败 entity=%s", eid, exc_info=True)
    return ApiResponse(data={"maps": maps})


@router.put("/ha/action-maps")
async def set_action_map(
    payload: ActionMapRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/更新一个实体的动作映射。空 mappings = 删除。

    校验：每个 target 必须属于该域 services 且 ≠ 源 service。
    写入后清缓存并触发 catalog 刷新。
    """
    import json
    from ..core.database import Database
    from ..services.semantic_map import invalidate_cache

    entity_id = payload.entity_id
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    if not payload.mappings:
        await db.emoji_pref_delete("entity_action_map", entity_id)
    else:
        # 校验 target 合法性
        domain = entity_id.split(".")[0]
        svc_defs = await container.ha_service.get_service_defs(container.ha_client, domains={domain})
        valid_svcs = set((svc_defs.get(domain, {}) or {}).keys())
        if not valid_svcs:
            raise AppException(
                f"域 {domain} 的服务列表获取失败，无法校验映射",
                code="ha_error", http_status=502,
            )
        cleaned: dict[str, dict] = {}
        for svc, entry in payload.mappings.items():
            if not isinstance(entry, dict):
                continue
            target = entry.get("target", "")
            if not target or target == svc:
                continue
            if target not in valid_svcs:
                raise AppException(
                    f"service '{target}' 不属于域 {domain}（可用: {sorted(valid_svcs)}）",
                    code="invalid_target", http_status=400,
                )
            cleaned[svc] = {"target": target, "description": entry.get("description", "")}
        if not cleaned:
            await db.emoji_pref_delete("entity_action_map", entity_id)
        else:
            await db.emoji_pref_upsert(
                "entity_action_map", entity_id, json.dumps({"mappings": cleaned})
            )
    invalidate_cache()
    refresh_fn = getattr(container, "catalog_refresh_fn", None)
    if refresh_fn is not None:
        try:
            asyncio.create_task(refresh_fn())
        except Exception:  # noqa: BLE001
            logger.warning("catalog refresh after action-map save failed", exc_info=True)
    return ApiResponse(data={"entity_id": entity_id, "mappings": payload.mappings})


@router.get("/ha/entity-services")
async def get_entity_services(container: AppContainer = Depends(get_container)) -> ApiResponse[dict]:
    """返回按域分组的可用服务列表（供前端拉取可配置的 action）。

    复用 ha_service.get_service_defs，剥离成 {domain: [svc_name, ...]}。
    """
    try:
        svc_defs = await container.ha_service.get_service_defs(container.ha_client)
        services = {domain: sorted(svcs.keys()) for domain, svcs in svc_defs.items()}
        return ApiResponse(data={"services": services})
    except Exception as e:
        logger.exception("entity-services failed")
        raise AppException(f"服务列表获取失败: {e}", code="ha_error", http_status=502)
```

- [ ] **Step 5: 运行测试确认通过**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_action_maps_route.py -v`
Expected: 4 passed

- [ ] **Step 6: 提交**

```bash
cd D:/Aether && git add app/schema/api_schemas.py app/routes/ha_routes.py tests/test_action_maps_route.py
git commit -m "feat(api): /ha/action-maps GET/PUT + /ha/entity-services，含 target 合法性校验"
```

---

### Task 3: call_service 过滤层（动作翻转 + 结果反馈 + state 翻转）

**Files:**
- Modify: `app/tools.py`（L343 `call_with_probe` 调用前插入过滤；L354 return 改造）
- Test: `tests/test_call_service_semantic_map.py`

**Interfaces:**
- Consumes: Task 1 的 `get_action_map` / `apply_state_flip`
- Produces: call_service 返回值新增可选 `semantic_mapping` 字段（AI 可见）

- [ ] **Step 1: 写失败测试（有映射时翻转 + 返回 semantic_mapping）**

创建 `tests/test_call_service_semantic_map.py`：

```python
"""Tests for call_service semantic action-map filtering."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


def _build_deps(states):
    from app.tools import ToolDeps, _register_ha_call_service
    from app.mcp.mcp_client_manager import MCPClientManager
    mgr = MCPClientManager()
    ha_client = MagicMock()
    ha_client.get_states = AsyncMock(return_value=states)
    ha_service = MagicMock()
    ha_service.get_all_devices = AsyncMock(return_value=[
        {"entity_id": s["entity_id"], "domain": s["entity_id"].split(".")[0],
         "attributes": {"friendly_name": s["entity_id"]}}
        for s in states
    ])
    deps = ToolDeps(
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=ha_service, ha_client_ref=[ha_client],
    )
    _register_ha_call_service(deps)
    return mgr.get_tool("ha_devices___call_service")


@pytest.mark.asyncio
async def test_call_service_maps_turn_on_to_turn_off(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    payload = json.dumps({"mappings": {
        "turn_on": {"target": "turn_off", "description": "继电器反转：开门断电"},
        "turn_off": {"target": "turn_on", "description": "继电器反转：关门通电"}}})
    await Database.get().emoji_pref_upsert("entity_action_map", "switch.gate", payload)
    from app.services.semantic_map import invalidate_cache
    invalidate_cache()

    states = [{"entity_id": "switch.gate", "state": "off", "attributes": {}}]
    tool = _build_deps(states)
    session = MagicMock()
    session.current_query = "开门"
    captured = {}
    async def fake_call(hc, domain, service, eid, data):
        captured["service"] = service
        return {}
    with patch("app.tools.call_with_probe", new=fake_call):
        result = await tool.handler(
            {"domain": "switch", "service": "turn_on", "entity_id": "switch.gate"}, session
        )
    # AI 调 turn_on → 实际执行 turn_off
    assert captured["service"] == "turn_off"
    assert result["success"] is True
    assert result["semantic_mapping"]["requested"] == "turn_on"
    assert result["semantic_mapping"]["executed"] == "turn_off"
    assert "继电器反转" in result["semantic_mapping"]["description"]
    # 对称翻转对 → state 翻转（物理 off=开门 → AI 看到 on）
    assert result["new_state"]["state"] == "on"


@pytest.mark.asyncio
async def test_call_service_no_map_passes_through(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.services.semantic_map import invalidate_cache
    invalidate_cache()

    states = [{"entity_id": "light.bed", "state": "off", "attributes": {}}]
    tool = _build_deps(states)
    session = MagicMock()
    session.current_query = "开灯"
    captured = {}
    async def fake_call(hc, domain, service, eid, data):
        captured["service"] = service
        return {}
    with patch("app.tools.call_with_probe", new=fake_call):
        result = await tool.handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.bed"}, session
        )
    assert captured["service"] == "turn_on"
    assert "semantic_mapping" not in result
    assert result["new_state"]["state"] == "off"


@pytest.mark.asyncio
async def test_call_service_db_error_passes_through(tmp_path, monkeypatch):
    """DB 异常时放行原 service，不抛错。"""
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    from app.services.semantic_map import invalidate_cache
    invalidate_cache()
    # 模拟 DB 故障
    monkeypatch.setattr("app.services.semantic_map._reload_cache",
                        AsyncMock(side_effect=Exception("db down")))

    states = [{"entity_id": "switch.gate", "state": "off", "attributes": {}}]
    tool = _build_deps(states)
    session = MagicMock()
    session.current_query = "开门"
    captured = {}
    async def fake_call(hc, domain, service, eid, data):
        captured["service"] = service
        return {}
    with patch("app.tools.call_with_probe", new=fake_call):
        result = await tool.handler(
            {"domain": "switch", "service": "turn_on", "entity_id": "switch.gate"}, session
        )
    assert captured["service"] == "turn_on"
    assert result["success"] is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_call_service_semantic_map.py -v`
Expected: FAIL（service 没被翻转，无 semantic_mapping 字段）

- [ ] **Step 3: 实现 call_service 过滤层**

在 `app/tools.py` 的 `_register_ha_call_service` handler 里，找到 L343 `result = await call_with_probe(ha_client, domain, service, entity_id, data)` 这一行，**在它前面**插入过滤：

```python
            # 语义映射过滤：无条件替换 service（不依赖意图判断，避免双重错误）。
            # AI 凭直觉调用，过滤器无条件纠正，结果反馈事后解释。
            original_service = service
            mapped_description = None
            if entity_id:
                try:
                    from .services.semantic_map import get_action_map
                    action_map = await get_action_map(str(entity_id).split(",")[0].strip())
                    if action_map:
                        entry = action_map.get("mappings", {}).get(service)
                        if entry and entry.get("target") and entry["target"] != service:
                            service = entry["target"]
                            mapped_description = entry.get("description", "")
                            logger.info("call_service 语义映射: %s.%s → %s",
                                        entity_id, original_service, service)
                except Exception:  # noqa: BLE001
                    logger.warning("call_service: 语义映射查询失败，放行原 service", exc_info=True)
            result = await call_with_probe(ha_client, domain, service, entity_id, data)
```

然后找到 L354 `return {"success": True, "result": result, "new_state": new_state}` 这一行，**替换**为：

```python
            ret: dict = {"success": True, "result": result, "new_state": new_state}
            if service != original_service:
                # 动作被映射 → 带描述，让 AI 理解实际发生了什么、如何汇报给用户
                ret["semantic_mapping"] = {
                    "requested": original_service,
                    "executed": service,
                    "description": mapped_description or "该设备配置了语义映射",
                }
                # 对称翻转对 → state 隐含跟随翻转（避免 AI 看到相反状态说反话）
                if new_state and new_state.get("state") in ("on", "off"):
                    try:
                        from .services.semantic_map import apply_state_flip
                        ret["new_state"] = apply_state_flip(new_state, entity_id)
                    except Exception:  # noqa: BLE001
                        logger.warning("call_service: state 翻转失败，放行原 state", exc_info=True)
            return ret
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_call_service_semantic_map.py -v`
Expected: 3 passed

- [ ] **Step 5: 跑 call_service 既有测试确认无回归**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_call_service_operable.py -v`
Expected: 3 passed（黑名单拦截、正常放行、ai_operable 字段都不受影响）

- [ ] **Step 6: 提交**

```bash
cd D:/Aether && git add app/tools.py tests/test_call_service_semantic_map.py
git commit -m "feat(call-service): 语义映射过滤层 — 无条件翻转 service + 结果反馈 + state 隐含翻转"
```

---

### Task 4: get_entities / catalog 状态预翻转

**Files:**
- Modify: `app/tools.py`（`_register_ha_get_entities` handler，L131-134 循环内追加 state 翻转；`_register_ha_get_device_manual` handler，L218 resolve_controls 前追加 state 翻转）
- Modify: `app/main.py`（`_refresh_ha_catalog` L281-285 循环内追加 state 预翻转）
- Test: `tests/test_get_entities_state_flip.py`

**Interfaces:**
- Consumes: Task 1 的 `flip_state_value`
- Produces: get_entities 返回的 entity.state 翻转后；get_device_manual 的 controls current 翻转后；catalog 行 `状态:{...}` 翻转后

**注意：状态读取点共 5 处**（自检修正，原 spec 写 4 处漏了 get_device_manual）：
1. `call_service` 返回 `new_state`（Task 3 已处理）
2. `get_entities` 返回的 entity.state（本 Task）
3. `get_device_manual` 的 controls current（本 Task，经 entity.get("state") 渗入 controls_to_text）
4. catalog 行 `状态:{...}`（本 Task）
5. catalog 的 resolve_controls current（本 Task，与 4 同函数同循环，翻转 flat entity 即覆盖）

- [ ] **Step 1: 写失败测试（get_entities 翻转对称设备的 state）**

创建 `tests/test_get_entities_state_flip.py`：

```python
"""Tests for get_entities state pre-flip on symmetric-mapped devices."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


@pytest.mark.asyncio
async def test_get_entities_flips_state_for_mapped_device(tmp_path, monkeypatch):
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    payload = json.dumps({"mappings": {
        "turn_on": {"target": "turn_off"}, "turn_off": {"target": "turn_on"}}})
    await Database.get().emoji_pref_upsert("entity_action_map", "switch.gate", payload)
    from app.services.semantic_map import invalidate_cache
    invalidate_cache()

    from app.tools import ToolDeps, _register_ha_get_entities
    from app.mcp.mcp_client_manager import MCPClientManager
    mgr = MCPClientManager()
    ha_service = MagicMock()
    ha_service.get_all_devices = AsyncMock(return_value=[
        {"entity_id": "switch.gate", "domain": "switch", "state": "off", "attributes": {}},
        {"entity_id": "light.bed", "domain": "light", "state": "off", "attributes": {}},
    ])
    ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": []})
    ha_service.get_service_defs = AsyncMock(return_value={})
    deps = ToolDeps(
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=ha_service, ha_client_ref=[MagicMock()],
    )
    _register_ha_get_entities(deps)
    tool = mgr.get_tool("ha_devices___get_entities")
    with patch("app.services.entity_controls.resolve_controls", return_value={}):
        result = await tool.handler({}, MagicMock())
    by_id = {e["entity_id"]: e for e in result["entities"]}
    # gate 物理 off=开门 → 翻转后 on（AI 直觉 on=开）
    assert by_id["switch.gate"]["state"] == "on"
    # 普通灯不翻
    assert by_id["light.bed"]["state"] == "off"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_get_entities_state_flip.py -v`
Expected: FAIL（switch.gate state 仍为 off）

- [ ] **Step 3: get_entities 追加 state 翻转**

在 `app/tools.py` 的 `_register_ha_get_entities` handler 里，找到（约 L131-134）：

```python
            for device in devices:
                device["_controls"] = resolve_controls(device, raw_svc_defs)
                device["note"] = notes_map.get(device["entity_id"], "")
                device["ai_operable"] = device["entity_id"] not in operable_disabled
```

替换为：

```python
            # 语义映射：对称翻转对设备预翻转 state（让 AI 查询时认知正确）
            from .services.semantic_map import flip_state_value
            for device in devices:
                device["_controls"] = resolve_controls(device, raw_svc_defs)
                device["note"] = notes_map.get(device["entity_id"], "")
                device["ai_operable"] = device["entity_id"] not in operable_disabled
                try:
                    device["state"] = await flip_state_value(
                        device["entity_id"], str(device.get("state", ""))
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("get_entities: state 翻转失败", exc_info=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_get_entities_state_flip.py -v`
Expected: 1 passed

- [ ] **Step 5: catalog 预翻转（app/main.py `_refresh_ha_catalog`）**

在 `app/main.py` 的 `_refresh_ha_catalog` 函数里，找到（约 L281-286）：

```python
            for e in controllable:
                eid = e["entity_id"]
                marker = " ⛔AI禁操作" if eid in operable_disabled else ""
                lines.append(
                    f"- {eid} (类型:{e['domain']}, 状态:{e['state']}) 名称:{dev_name}{marker}"
                )
```

替换为（状态翻转后用于 catalog 行 + 传给 resolve_controls 的 flat entity）：

```python
            from .services.semantic_map import flip_state_value
            for e in controllable:
                eid = e["entity_id"]
                marker = " ⛔AI禁操作" if eid in operable_disabled else ""
                # 语义映射：对称翻转对设备预翻转 state（catalog 行 + resolve_controls 都用翻转后的）
                display_state = e["state"]
                try:
                    display_state = await flip_state_value(eid, str(display_state))
                except Exception:  # noqa: BLE001
                    pass
                lines.append(
                    f"- {eid} (类型:{e['domain']}, 状态:{display_state}) 名称:{dev_name}{marker}"
                )
                # 把翻转后的 state 写回 flat entity，resolve_controls 的 current 也跟着对
                if e["entity_id"] == eid and display_state != e["state"]:
                    if flat := next((d for d in devices if d["entity_id"] == eid), None):
                        flat["state"] = display_state
```

注意：`flat` 查找在下面 L294 原本也有一次（`flat = next(...)`），这里提前翻转写回，下面那次读到的就是翻转后的值，无冲突。

- [ ] **Step 6: get_device_manual 追加 state 翻转（第 5 个读取点）**

`get_device_manual` 的 `controls_to_text` 会渲染 `entity.get("state")` 作为 current（`entity_controls.py` L40），所以也要翻转。在 `app/tools.py` 的 `_register_ha_get_device_manual` handler 里，找到（约 L217-218）：

```python
                found.append(eid)
                controls = resolve_controls(dev, raw_svc_defs)
```

替换为：

```python
                found.append(eid)
                # 语义映射：对称翻转对设备预翻转 state（controls current 跟着对）
                try:
                    from .services.semantic_map import flip_state_value
                    dev = {**dev, "state": await flip_state_value(eid, str(dev.get("state", "")))}
                except Exception:  # noqa: BLE001
                    logger.warning("get_device_manual: state 翻转失败", exc_info=True)
                controls = resolve_controls(dev, raw_svc_defs)
```

注意用 `{**dev, ...}` 创建翻转后的副本，不改原 dev_by_eid 里的引用（避免污染同次循环其他读取）。

- [ ] **Step 7: 跑 catalog 相关既有测试确认无回归**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_entity_controls.py tests/test_dispatcher.py -v`
Expected: 全 passed（catalog 翻转是无映射设备原样返回，不影响）

- [ ] **Step 8: 提交**

```bash
cd D:/Aether && git add app/tools.py app/main.py tests/test_get_entities_state_flip.py
git commit -m "feat(state-flip): get_entities + get_device_manual + catalog 对称翻转对设备预翻转 state"
```

---

### Task 5: 前端路由 + 侧边栏入口

**Files:**
- Modify: `frontend/src/router/index.js`（routes 数组追加）
- Modify: `frontend/src/components/SidebarNav.vue`（navItems 追加）

**Interfaces:**
- Produces: `/semantics` 路由指向 `SemanticsView.vue`（Task 6 创建）

- [ ] **Step 1: 加路由**

在 `frontend/src/router/index.js` 的 routes 数组里，在 `advanced` 路由后追加：

```js
  {
    path: '/semantics',
    name: 'Semantics',
    component: () => import('../views/SemanticsView.vue'),
  },
```

- [ ] **Step 2: 加侧边栏入口**

在 `frontend/src/components/SidebarNav.vue` 的 `navItems` 数组（L61-66）追加一项：

```js
const navItems = [
  { path: '/chat', icon: '&#128172;', label: '管家' },
  { path: '/cameras', icon: '&#127909;', label: '摄像头' },
  { path: '/semantics', icon: '&#128260;', label: '语义' },
  { path: '/settings', icon: '&#9881;', label: '设置' },
  { path: '/advanced', icon: '&#128295;', label: '高级' },
]
```

（`&#128260;` = 🔀）

- [ ] **Step 3: 提交**

```bash
cd D:/Aether && git add frontend/src/router/index.js frontend/src/components/SidebarNav.vue
git commit -m "feat(frontend): /semantics 路由 + 侧边栏入口"
```

---

### Task 6: SemanticsView.vue 配置页

**Files:**
- Create: `frontend/src/views/SemanticsView.vue`

**Interfaces:**
- Consumes: Task 2 的 `GET /ha/action-maps`、`PUT /ha/action-maps`、`GET /ha/entity-services`、现有 `GET /ha/entities`

- [ ] **Step 1: 创建 SemanticsView.vue**

创建 `frontend/src/views/SemanticsView.vue`：

```vue
<script setup>
import { ref, computed, onMounted } from 'vue'
import { apiGet, apiPut } from '../utils/api'

// 已配置的映射：{ entity_id: { mappings: { svc: {target, description} } } }
const actionMaps = ref({})
// 全部实体列表（取 name / entity_id）
const entities = ref([])
// 按域的可用 services：{ domain: [svc_name, ...] }
const domainServices = ref({})
const loading = ref(true)
const saving = ref(false)
const error = ref('')

// Modal 状态
const showModal = ref(false)
const selectedEntityId = ref('')
const searchKeyword = ref('')
// 当前编辑的映射草稿：{ svc: { target, description } }
const draftMappings = ref({})

// 实体名称查找
const entityNameMap = computed(() => {
  const m = {}
  for (const e of entities.value) {
    const name = e.attributes?.friendly_name || e.entity_id
    m[e.entity_id] = name
  }
  return m
})

// 搜索过滤后的实体列表
const filteredEntities = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return entities.value
  return entities.value.filter((e) => {
    const name = (e.attributes?.friendly_name || '').toLowerCase()
    return name.includes(kw) || e.entity_id.toLowerCase().includes(kw)
  })
})

// 选中实体的可用 services（按域）
const selectedServices = computed(() => {
  if (!selectedEntityId.value) return []
  const domain = selectedEntityId.value.split('.')[0]
  return domainServices.value[domain] || []
})

// 已配置映射的实体卡片列表
const configuredList = computed(() => {
  return Object.entries(actionMaps.value).map(([eid, cfg]) => {
    const mappings = cfg.mappings || {}
    const summary = Object.entries(mappings)
      .map(([svc, e]) => `${svc}→${e.target}`)
      .join('、')
    return { entityId: eid, name: entityNameMap.value[eid] || eid, mappings, summary }
  })
})

async function loadData() {
  loading.value = true
  error.value = ''
  try {
    const [mapsData, entData, svcData] = await Promise.all([
      apiGet('/api/ha/action-maps'),
      apiGet('/api/ha/entities'),
      apiGet('/api/ha/entity-services'),
    ])
    actionMaps.value = mapsData.maps || {}
    entities.value = entData.entities || []
    domainServices.value = svcData.services || {}
  } catch (e) {
    error.value = e.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function openAddModal() {
  selectedEntityId.value = ''
  searchKeyword.value = ''
  draftMappings.value = {}
  showModal.value = true
}

function openEditModal(entityId) {
  selectedEntityId.value = entityId
  searchKeyword.value = ''
  const existing = actionMaps.value[entityId]?.mappings || {}
  // 深拷贝到草稿
  draftMappings.value = JSON.parse(JSON.stringify(existing))
  showModal.value = true
}

function selectEntity(eid) {
  selectedEntityId.value = eid
  const existing = actionMaps.value[eid]?.mappings || {}
  draftMappings.value = JSON.parse(JSON.stringify(existing))
}

// 草稿里某 service 的 target（默认=自身）
function targetOf(svc) {
  return draftMappings.value[svc]?.target || svc
}
function descOf(svc) {
  return draftMappings.value[svc]?.description || ''
}
function setTarget(svc, target) {
  if (!draftMappings.value[svc]) draftMappings.value[svc] = { target: svc, description: '' }
  draftMappings.value[svc].target = target
}
function setDesc(svc, desc) {
  if (!draftMappings.value[svc]) draftMappings.value[svc] = { target: svc, description: '' }
  draftMappings.value[svc].description = desc
}
function isMapped(svc) {
  return targetOf(svc) !== svc
}

async function saveMappings() {
  if (!selectedEntityId.value) return
  saving.value = true
  error.value = ''
  // 只收集 target≠自身 的
  const cleaned = {}
  for (const [svc, entry] of Object.entries(draftMappings.value)) {
    if (entry.target && entry.target !== svc) {
      cleaned[svc] = { target: entry.target, description: entry.description || '' }
    }
  }
  try {
    await apiPut('/api/ha/action-maps', { entity_id: selectedEntityId.value, mappings: cleaned })
    // 更新本地
    if (Object.keys(cleaned).length) {
      actionMaps.value[selectedEntityId.value] = { mappings: cleaned }
    } else {
      delete actionMaps.value[selectedEntityId.value]
    }
    showModal.value = false
  } catch (e) {
    error.value = e.message || '保存失败'
  } finally {
    saving.value = false
  }
}

async function deleteAllMappings(entityId) {
  if (!confirm(`确认删除「${entityNameMap.value[entityId] || entityId}」的全部映射？`)) return
  saving.value = true
  try {
    await apiPut('/api/ha/action-maps', { entity_id: entityId, mappings: {} })
    delete actionMaps.value[entityId]
    if (selectedEntityId.value === entityId) showModal.value = false
  } catch (e) {
    error.value = e.message || '删除失败'
  } finally {
    saving.value = false
  }
}

onMounted(loadData)
</script>

<template>
  <div class="semantics-page">
    <header class="page-header">
      <h1>语义映射</h1>
      <p class="hint">配置设备的动作映射（如门禁继电器 turn_on↔turn_off），系统会自动适配物理操作。</p>
    </header>

    <div v-if="loading" class="loading">加载中…</div>
    <div v-else-if="error && !showModal" class="error">{{ error }}</div>

    <section v-if="!loading" class="configured-list">
      <div v-if="configuredList.length === 0" class="empty">
        <p>尚未配置任何映射</p>
      </div>
      <div v-for="item in configuredList" :key="item.entityId" class="map-card">
        <div class="card-main" @click="openEditModal(item.entityId)">
          <div class="card-title">{{ item.name }}</div>
          <div class="card-sub">{{ item.entityId }}</div>
          <div class="card-summary">{{ item.summary }}</div>
        </div>
        <button class="btn-delete" @click.stop="deleteAllMappings(item.entityId)">删除</button>
      </div>
      <button class="btn-add" @click="openAddModal">+ 添加设备</button>
    </section>

    <!-- 配置 Modal -->
    <div v-if="showModal" class="modal-overlay" @click.self="showModal = false">
      <div class="modal-content semantics-modal">
        <div class="modal-header">
          <h2>配置语义映射</h2>
          <button class="btn-close" @click="showModal = false">×</button>
        </div>
        <div v-if="error" class="error">{{ error }}</div>

        <!-- 一级：实体选择（仅未选时显示） -->
        <div v-if="!selectedEntityId" class="entity-picker">
          <input v-model="searchKeyword" class="search-input" placeholder="搜索设备名称或 entity_id…" />
          <div class="entity-list">
            <div
              v-for="e in filteredEntities"
              :key="e.entity_id"
              class="entity-row"
              @click="selectEntity(e.entity_id)"
            >
              <span class="er-name">{{ e.attributes?.friendly_name || e.entity_id }}</span>
              <span class="er-id">{{ e.entity_id }}</span>
            </div>
          </div>
        </div>

        <!-- 二级：服务映射配置（选中实体后显示） -->
        <div v-else class="service-config">
          <div class="selected-entity">
            <button class="btn-back" @click="selectedEntityId = ''">← 返回选择</button>
            <span class="se-name">{{ entityNameMap[selectedEntityId] || selectedEntityId }}</span>
            <span class="se-id">{{ selectedEntityId }}</span>
          </div>
          <div v-if="selectedServices.length === 0" class="empty-services">
            该设备域无可用服务
          </div>
          <div v-else class="service-rows">
            <div v-for="svc in selectedServices" :key="svc" class="service-row">
              <div class="sr-head">
                <span class="sr-svc">{{ svc }}</span>
                <span class="sr-arrow">→</span>
                <select
                  class="sr-select"
                  :value="targetOf(svc)"
                  @change="setTarget(svc, $event.target.value)"
                >
                  <option v-for="t in selectedServices" :key="t" :value="t">{{ t }}</option>
                </select>
              </div>
              <input
                v-if="isMapped(svc)"
                class="sr-desc"
                :value="descOf(svc)"
                @input="setDesc(svc, $event.target.value)"
                placeholder="描述（映射触发时带给 AI，解释实际发生了什么）"
              />
            </div>
          </div>
          <div class="modal-actions">
            <button class="btn-delete" @click="deleteAllMappings(selectedEntityId)">删除全部映射</button>
            <button class="btn-save" :disabled="saving" @click="saveMappings">
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.semantics-page { padding: 20px; max-width: 800px; margin: 0 auto; }
.page-header h1 { font-size: 22px; margin-bottom: 4px; }
.hint { color: var(--color-text-secondary, #888); font-size: 13px; margin-bottom: 16px; }
.loading, .empty, .error { padding: 24px; text-align: center; color: var(--color-text-secondary, #888); }
.error { color: var(--color-danger, #e5484d); }
.configured-list { display: flex; flex-direction: column; gap: 8px; }
.map-card {
  display: flex; align-items: center; gap: 8px;
  background: var(--color-surface, rgba(255,255,255,0.04));
  border: 1px solid var(--color-border, rgba(255,255,255,0.1));
  border-radius: 12px; padding: 12px;
}
.card-main { flex: 1; cursor: pointer; }
.card-title { font-weight: 600; }
.card-sub { font-size: 12px; color: var(--color-text-secondary, #888); }
.card-summary { font-size: 13px; margin-top: 4px; color: var(--color-text-secondary, #aaa); }
.btn-add { margin-top: 8px; }
.btn-delete { color: var(--color-danger, #e5484d); background: transparent; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); border-radius: 8px; padding: 6px 12px; cursor: pointer; }
.btn-save { background: var(--color-primary, #4c6ef5); color: #fff; border: none; border-radius: 8px; padding: 8px 16px; cursor: pointer; }
.btn-save:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-close { background: transparent; border: none; font-size: 22px; cursor: pointer; color: var(--color-text-secondary, #888); }
.semantics-modal { max-width: 640px; }
.entity-picker { padding: 16px; }
.search-input { width: 100%; padding: 8px 12px; border-radius: 8px; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); background: var(--color-surface, rgba(255,255,255,0.04)); color: var(--color-text, #fff); margin-bottom: 12px; box-sizing: border-box; }
.entity-list { max-height: 360px; overflow-y: auto; display: flex; flex-direction: column; gap: 4px; }
.entity-row { display: flex; justify-content: space-between; padding: 8px 12px; border-radius: 8px; cursor: pointer; border: 1px solid transparent; }
.entity-row:hover { background: var(--color-surface-hover, rgba(255,255,255,0.08)); border-color: var(--color-border, rgba(255,255,255,0.1)); }
.er-name { font-weight: 500; }
.er-id { font-size: 12px; color: var(--color-text-secondary, #888); }
.service-config { padding: 16px; }
.selected-entity { display: flex; align-items: center; gap: 8px; margin-bottom: 16px; }
.btn-back { background: transparent; border: none; cursor: pointer; color: var(--color-text-secondary, #888); }
.se-name { font-weight: 600; }
.se-id { font-size: 12px; color: var(--color-text-secondary, #888); }
.empty-services { padding: 24px; text-align: center; color: var(--color-text-secondary, #888); }
.service-rows { display: flex; flex-direction: column; gap: 12px; max-height: 360px; overflow-y: auto; }
.service-row { border: 1px solid var(--color-border, rgba(255,255,255,0.1)); border-radius: 8px; padding: 8px 12px; }
.sr-head { display: flex; align-items: center; gap: 8px; }
.sr-svc { font-weight: 500; min-width: 120px; }
.sr-arrow { color: var(--color-text-secondary, #888); }
.sr-select { flex: 1; padding: 4px 8px; border-radius: 6px; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); background: var(--color-surface, rgba(255,255,255,0.04)); color: var(--color-text, #fff); }
.sr-desc { width: 100%; margin-top: 8px; padding: 6px 8px; border-radius: 6px; border: 1px solid var(--color-border, rgba(255,255,255,0.1)); background: var(--color-surface, rgba(255,255,255,0.04)); color: var(--color-text, #fff); box-sizing: border-box; font-size: 13px; }
.modal-actions { display: flex; justify-content: space-between; margin-top: 16px; }
</style>
```

- [ ] **Step 2: 提交**

```bash
cd D:/Aether && git add frontend/src/views/SemanticsView.vue
git commit -m "feat(frontend): SemanticsView 语义映射配置页（两层：实体→服务）"
```

---

### Task 7: 全量回归 + Docker 重建 + 端到端验证

**Files:** 无（验证任务）

- [ ] **Step 1: 跑全部后端测试确认无回归**

Run: `PYTHONPATH=/d/Aether /c/Users/26658/.conda/envs/learning/python.exe -m pytest tests/test_semantic_map.py tests/test_action_maps_route.py tests/test_call_service_semantic_map.py tests/test_get_entities_state_flip.py tests/test_call_service_operable.py tests/test_entity_controls.py -v`
Expected: 全 passed

- [ ] **Step 2: Docker 重建**

Run: `cd D:/Aether && docker compose build aether && docker compose up -d aether`
Expected: 容器启动正常，日志无 import 错误

- [ ] **Step 3: 前端构建验证**

Run: `cd D:/Aether/frontend && npm run build`
Expected: 构建成功，无编译错误

- [ ] **Step 4: 端到端手动验证（虚拟设备门禁继电器）**

在 `/semantics` 页：
1. 添加 `switch.gate`（虚拟门禁继电器）
2. 配置 turn_on → turn_off，描述"继电器反转：开门时断电"
3. 配置 turn_off → turn_on，描述"继电器反转：关门时通电"
4. 保存

聊天测试：
- 说"开门" → 验证日志 `call_service 语义映射: switch.gate.turn_on → turn_off`
- 说"开门" → 验证 AI 回复包含"打开/已开"（不是"关闭"）
- 说"关门" → 验证实际调 turn_on
- 问"门开着吗" → 验证 AI 基于翻转后 state 回答正确

- [ ] **Step 5: 提交验证记录（如有日志/截图调整）**

如有修复，提交。否则此任务无新增 commit。

---

## Self-Review 记录

**Spec 覆盖检查：**
- §4 数据模型（entity_action_map scope + JSON 结构）→ Task 1/2 ✅
- §5 层 1 动作过滤 → Task 3 Step 3 ✅
- §5 层 2 结果反馈 + state 翻转 → Task 3 Step 3 ✅
- §5 层 3 提示词零暴露（不动 controls_to_text）→ 全程未改 controls_to_text ✅
- §6 state 读取点（自检修正为 5 处，原 spec 写 4 处漏了 get_device_manual）→ Task 3（call_service new_state）+ Task 4（get_entities + get_device_manual + catalog 行 + catalog resolve_controls，其中后两者在同循环翻转 flat entity 一次覆盖）✅
- §7 GET/PUT action-maps + entity-services → Task 2 ✅
- §7 PUT target 校验 → Task 2 Step 4 ✅
- §8 前端两层结构 → Task 6 ✅
- §11 风险（DB 异常放行、循环校验、toggle）→ Task 3 DB 异常测试 + Task 2 target 校验 ✅

**类型一致性：**
- `get_action_map(entity_id) -> dict|None` — Task 1 定义，Task 3 消费 ✅
- `apply_state_flip(new_state, entity_id) -> dict` — Task 1 定义，Task 3 消费 ✅
- `flip_state_value(entity_id, state) -> str` — Task 1 定义，Task 4 消费 ✅
- `invalidate_cache()` — Task 1 定义，Task 2 消费 ✅
- `is_flipped_pair(mappings) -> bool` — Task 1 内部用 ✅

**占位符扫描：** 无 TBD/TODO，所有代码块完整 ✅
