# AI 设备操作白名单（entity_operable）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给每个 HA 实体一个"AI 可操作"权限标记，用户在设备详情页用绿/红徽章切换；AI 能看到所有设备，但 call_service 执行前硬拦截被禁用的实体，system prompt 同时软引导多候选优先选可操作的。

**Architecture:** 黑名单存储——复用 `emoji_preferences` 表新增 `entity_operable` scope，只存被显式禁用的实体（默认可操作）。三层联动：(1) 可见层——`get_entities` 返回 `ai_operable` 字段、catalog 行尾标 `⛔AI禁操作`、prompt 写明多候选优先 + 硬禁令；(2) 拦截层——`call_service` 执行前查黑名单拒绝；(3) 编辑层——前端子实体行右侧绿/红徽章（仅可控实体）切换。

**Tech Stack:** Python 3 / FastAPI / Pydantic（后端）；Vue 3 Composition API / vitest / @vue/test-utils（前端）；复用现有 `emoji_preferences` 表，零 schema 迁移。

## Global Constraints

- 存储复用 `app/core/database.py` 的 `emoji_preferences` 表，scope = `entity_operable`，value = `"0"` 表示禁用；删除记录 = 恢复可操作。不新建表、不改 schema。
- 默认可操作（黑名单）：表中没有的实体 = 允许 AI 操作。DB 读取异常一律放行（与现有校验风格一致，避免 DB 故障锁死全屋）。
- 仅对可控实体（`isControllable`，即该 domain 在 `services` 里有服务定义）显示前端徽章；只读 sensor 不显示。
- 徽章用绿色（允许）/红色（禁止）状态色，**不用 🤖 图标**。
- catalog 设备行格式 `- entity_id (类型:domain, 状态:xxx) 名称:xxx` 的前缀部分不可改（`rule_service._parse_ha_catalog` 正则依赖），`⛔AI禁操作` 标记只能加在行尾。
- 校验串行顺序：entity_id 真实性 → operable 授权（本计划新增）→ `match_devices` 语义 → 执行。
- 多候选优先属 prompt 软引导，不改架构、不加自动重定向。

---

## File Structure

| 文件 | 责任 | 新建/修改 |
|------|------|-----------|
| `app/schema/api_schemas.py` | `EntityOperableRequest` 请求体 | 修改（加类） |
| `app/routes/ha_routes.py` | `GET/PUT /ha/entity-operable` | 修改（加两路由） |
| `app/tools.py` | call_service 授权拦截 + get_entities 加 `ai_operable` | 修改 |
| `app/main.py` | `_refresh_ha_catalog` 标 ⛔ + 跳过禁用项 controls | 修改 |
| `app/services/prompt_service.py` | catalog 注入文案加权限约束 | 修改 |
| `frontend/src/composables/useEntityMeta.js` | operable 状态管理 | 修改 |
| `frontend/src/views/HAListView.vue` | 子实体行绿/红徽章 | 修改 |
| `tests/test_ha_routes.py` | operable 路由测试 | 修改 |
| `tests/test_call_service_operable.py` | call_service 授权拦截 + get_entities 字段测试 | 新建 |
| `tests/test_main_catalog_notes.py` | catalog ⛔ 标注测试 | 修改 |
| `tests/test_prompt_service.py` | prompt 权限文案测试 | 修改 |
| `frontend/tests/composables/useEntityMeta.operable.test.js` | composable 测试 | 新建 |

---

### Task 1: 后端 schema + GET/PUT /ha/entity-operable 路由

**Files:**
- Modify: `app/schema/api_schemas.py`（在 `EntityNoteRequest` 后，约 line 286）
- Modify: `app/routes/ha_routes.py`（在 `set_entity_note` 后，约 line 142）
- Test: `tests/test_ha_routes.py`（新增 `TestEntityOperableRoute`）

**Interfaces:**
- Produces: `GET /api/ha/entity-operable` → `{disabled: {entity_id: "0", ...}}`；`PUT /api/ha/entity-operable` body `EntityOperableRequest{entity_id: str, operable: bool}` → `{entity_id, operable}`。写入后调 `container.catalog_refresh_fn()`（可能为 None，需判空）。

- [ ] **Step 1: 在 api_schemas.py 加 EntityOperableRequest**

在 `app/schema/api_schemas.py` 的 `EntityNoteRequest` 类之后（line 285 后）插入：

```python
class EntityOperableRequest(BaseModel):
    """设置实体「AI 可操作」权限的请求体。

    operable=False → 写入 entity_operable 黑名单（禁止 AI 操作）；
    operable=True  → 删除记录（恢复默认可操作）。
    完全可逆，可反复切换。仅影响 Aether 侧，不同步 HA。
    """

    entity_id: str = Field(..., description="HA 实体 ID")
    operable: bool = Field(..., description="True=允许 AI 操作（恢复），False=禁止 AI 操作")

    @field_validator("entity_id", mode="before")
    @classmethod
    def _strip_str(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) else str(v)
```

- [ ] **Step 2: 在 ha_routes.py 加 GET/PUT 路由**

在 `app/routes/ha_routes.py` 顶部 import 行（line 15）补 `EntityOperableRequest`：

```python
from ..schema.api_schemas import HAConfigRequest, HAServiceCallRequest, ModelTestRequest, UniqueSettingsRequest, EntityAliasRequest, EntityNoteRequest, EntityOperableRequest
```

在 `set_entity_note` 函数之后（line 142 后）插入两个路由：

```python
@router.get("/ha/entity-operable")
async def get_entity_operable() -> ApiResponse[dict]:
    """获取被用户禁止 AI 操作的实体集合（黑名单，{entity_id: "0"}）。"""
    from ..core.database import Database
    db = Database.get()
    disabled = await db.prefs_get_by_scope("entity_operable")
    return ApiResponse(data={"disabled": disabled})


@router.put("/ha/entity-operable")
async def set_entity_operable(
    payload: EntityOperableRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/取消实体的「AI 可操作」权限。完全可逆。

    operable=False 写入黑名单（禁止），True 删除记录（恢复可操作）。
    写入后立即刷新 catalog，让 system prompt 不等 60 秒就反映新权限。
    """
    from ..core.database import Database
    entity_id = payload.entity_id
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    if payload.operable:
        # 恢复可操作：删除黑名单记录
        await db.emoji_pref_delete("entity_operable", entity_id)
    else:
        # 禁止 AI 操作：写入黑名单
        await db.emoji_pref_upsert("entity_operable", entity_id, "0")
    # 立即刷新 catalog（与 set_entity_note 同做法）
    refresh_fn = getattr(container, "catalog_refresh_fn", None)
    if refresh_fn is not None:
        try:
            asyncio.create_task(refresh_fn())
        except Exception:  # noqa: BLE001
            logger.warning("catalog refresh after operable change failed", exc_info=True)
    return ApiResponse(data={"entity_id": entity_id, "operable": payload.operable})
```

- [ ] **Step 3: 写失败测试**

在 `tests/test_ha_routes.py` 末尾追加：

```python
class TestEntityOperableRoute:
    """测试 GET/PUT /ha/entity-operable 路由。"""

    @pytest.mark.asyncio
    async def test_get_entity_operable(self, tmp_path, monkeypatch):
        """GET 返回黑名单。"""
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
        await Database.init()
        await Database.get().emoji_pref_upsert("entity_operable", "lock.tong_suo", "0")
        from app.routes.ha_routes import get_entity_operable
        result = await get_entity_operable()
        assert result.code == "ok"
        assert result.data["disabled"] == {"lock.tong_suo": "0"}

    @pytest.mark.asyncio
    async def test_put_disable_then_enable(self, tmp_path, monkeypatch):
        """PUT operable=False 写黑名单，True 恢复（可逆）。"""
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
        await Database.init()
        from app.schema.api_schemas import EntityOperableRequest
        from app.routes.ha_routes import set_entity_operable
        container = _mock_container(catalog_refresh_fn=None)
        # 禁用
        await set_entity_operable(
            EntityOperableRequest(entity_id="lock.tong_suo", operable=False),
            container=container,
        )
        disabled = await Database.get().prefs_get_by_scope("entity_operable")
        assert disabled == {"lock.tong_suo": "0"}
        # 恢复
        await set_entity_operable(
            EntityOperableRequest(entity_id="lock.tong_suo", operable=True),
            container=container,
        )
        disabled = await Database.get().prefs_get_by_scope("entity_operable")
        assert disabled == {}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python -m pytest tests/test_ha_routes.py::TestEntityOperableRoute -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add app/schema/api_schemas.py app/routes/ha_routes.py tests/test_ha_routes.py
git commit -m "feat(backend): entity_operable 黑名单 GET/PUT 路由 + schema"
```

---

### Task 2: call_service 授权拦截（核心）

**Files:**
- Modify: `app/tools.py`（`_register_ha_call_service` handler，在 entity_id 真实性校验块 line 283 之后、语义校验 line 291 之前插入）
- Test: `tests/test_call_service_operable.py`（新建）

**Interfaces:**
- Consumes: `entity_operable` scope（Task 1 落库的黑名单），通过 `Database.get().prefs_get_by_scope("entity_operable")` 读。
- Produces: call_service handler 对黑名单内 entity_id 返回 `{"success": False, "error": "设备「<eid>」被用户设为禁止 AI 操作..."}`，不执行实际控制。

- [ ] **Step 1: 写失败测试（新建测试文件）**

创建 `tests/test_call_service_operable.py`：

```python
"""Tests for call_service operable authorization (Task 2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _init_db(tmp_path, monkeypatch):
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")


def _build_deps(states):
    """构造只注册了 call_service 的 ToolDeps。"""
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
async def test_call_service_blocks_disabled_entity(tmp_path, monkeypatch):
    """黑名单内 entity 被拒绝，不调用 HA。"""
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    await Database.get().emoji_pref_upsert("entity_operable", "lock.tong_suo", "0")

    tool = _build_deps([{"entity_id": "lock.tong_suo", "state": "locked", "attributes": {}}])
    session = MagicMock()
    session.current_query = "解锁童锁"
    result = await tool.handler(
        {"domain": "lock", "service": "unlock", "entity_id": "lock.tong_suo"}, session
    )
    assert result["success"] is False
    assert "禁止" in result["error"]


@pytest.mark.asyncio
async def test_call_service_allows_enabled_entity(tmp_path, monkeypatch):
    """不在黑名单的 entity 正常执行。"""
    from app.core.database import Database
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()

    tool = _build_deps([{"entity_id": "light.bed", "state": "off", "attributes": {}}])
    # ha_client.call_service 走 call_with_probe，mock 掉
    ha_client = tool.handler.__closure__  # 仅占位；实际通过 deps.ha_client_ref 控制
    session = MagicMock()
    session.current_query = "开灯"
    with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
        result = await tool.handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.bed"}, session
        )
    assert result["success"] is True
```

> 注：`test_call_service_allows_enabled_entity` 用 `patch("app.tools.call_with_probe", ...)` 绕过真实 HA 调用。需在文件顶部补 `from unittest.mock import patch`。

补全文件顶部 import：

```python
from unittest.mock import AsyncMock, MagicMock, patch
```

- [ ] **Step 2: 运行测试，确认禁用用例失败**

Run: `python -m pytest tests/test_call_service_operable.py::test_call_service_blocks_disabled_entity -v`
Expected: FAIL（目前 call_service 不拦截，返回 success:true）。

- [ ] **Step 3: 实现 call_service 授权拦截**

在 `app/tools.py` 的 `_register_ha_call_service` handler 中，找到 entity_id 真实性校验块（约 line 269-285，以 `if entity_id:` 开头、`except Exception: logger.warning("call_service: entity_id 校验失败，放行"...)` 结尾）之后、语义校验块（`query = getattr(session, "current_query"...)`）之前，插入：

```python
            # 授权校验：用户可在设备页把危险设备（童锁/门锁）标为禁止 AI 操作。
            # 读 entity_operable 黑名单，命中则拒绝。DB 异常时放行（避免锁死全屋）。
            if entity_id:
                try:
                    from .core.database import Database
                    disabled = await Database.get().prefs_get_by_scope("entity_operable")
                    eid_list_op = [e.strip() for e in str(entity_id).split(",") if e.strip()]
                    blocked = [e for e in eid_list_op if e in disabled]
                    if blocked:
                        names = "、".join(blocked)
                        logger.info("call_service 拒绝未授权 entity_id: %s", blocked)
                        return {
                            "success": False,
                            "error": (
                                f"设备「{names}」被用户设为禁止 AI 操作。请勿尝试调用，"
                                "如实告知用户需手动操作或在设备页解除限制。"
                            ),
                        }
                except Exception:
                    logger.warning("call_service: 授权校验失败，放行", exc_info=True)
```

- [ ] **Step 4: 运行全部测试，确认通过**

Run: `python -m pytest tests/test_call_service_operable.py -v`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add app/tools.py tests/test_call_service_operable.py
git commit -m "feat(backend): call_service 加 entity_operable 授权硬拦截"
```

---

### Task 3: get_entities 返回 ai_operable 字段

**Files:**
- Modify: `app/tools.py`（`_register_ha_get_entities` handler，约 line 117-126 区域）
- Test: `tests/test_call_service_operable.py`（追加用例）

**Interfaces:**
- Produces: `get_entities` 返回的 `entities` 数组每项增加 `ai_operable: bool`（默认 true，在黑名单则 false）。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_call_service_operable.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_get_entities_returns_ai_operable(tmp_path, monkeypatch):
    """get_entities 返回 ai_operable：黑名单内 false，其余 true。"""
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    monkeypatch.setattr("app.core.database.DB_PATH", tmp_path / "t.db")
    await Database.init()
    await Database.get().emoji_pref_upsert("entity_operable", "lock.tong_suo", "0")

    from app.tools import ToolDeps, _register_ha_get_entities
    from app.mcp.mcp_client_manager import MCPClientManager
    mgr = MCPClientManager()
    ha_service = MagicMock()
    ha_service.get_all_devices = AsyncMock(return_value=[
        {"entity_id": "lock.tong_suo", "domain": "lock", "state": "locked", "attributes": {}},
        {"entity_id": "light.bed", "domain": "light", "state": "off", "attributes": {}},
    ])
    ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": []})
    ha_service.get_service_defs = AsyncMock(return_value=[])
    deps = ToolDeps(
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=ha_service, ha_client_ref=[MagicMock()],
    )
    _register_ha_get_entities(deps)
    tool = mgr.get_tool("ha_devices___get_entities")
    with patch("app.services.entity_controls.resolve_controls", return_value={}):
        result = await tool.handler({}, MagicMock())
    by_id = {e["entity_id"]: e for e in result["entities"]}
    assert by_id["lock.tong_suo"]["ai_operable"] is False
    assert by_id["light.bed"]["ai_operable"] is True
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest tests/test_call_service_operable.py::test_get_entities_returns_ai_operable -v`
Expected: FAIL（KeyError: 'ai_operable'）

- [ ] **Step 3: 实现 get_entities 加 ai_operable**

在 `app/tools.py` 的 `_register_ha_get_entities` handler 中，找到 notes_map 读取块（约 line 118-126，`for device in devices:` 循环前），在其后、`for device in devices:` 循环处改造。先把 operable 黑名单读取加到 notes_map 读取之后：

```python
            # 用户「AI 可操作」黑名单：让 LLM 在 get_entities 返回里看到权限
            operable_disabled: dict[str, str] = {}
            try:
                from .core.database import Database
                operable_disabled = await Database.get().prefs_get_by_scope("entity_operable")
            except Exception:  # noqa: BLE001
                logger.warning("get_entities: operable 读取失败", exc_info=True)
```

再把 `for device in devices:` 循环体（约 line 124-126）改为：

```python
            for device in devices:
                device["_controls"] = resolve_controls(device, raw_svc_defs)
                device["note"] = notes_map.get(device["entity_id"], "")
                device["ai_operable"] = device["entity_id"] not in operable_disabled
```

- [ ] **Step 4: 运行，确认通过**

Run: `python -m pytest tests/test_call_service_operable.py::test_get_entities_returns_ai_operable -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/tools.py tests/test_call_service_operable.py
git commit -m "feat(backend): get_entities 返回实体 ai_operable 权限字段"
```

---

### Task 4: catalog 标 ⛔ + 跳过禁用项 controls

**Files:**
- Modify: `app/main.py`（`_refresh_ha_catalog`，约 line 242-293）
- Test: `tests/test_main_catalog_notes.py`（追加用例）

**Interfaces:**
- Consumes: `entity_operable` 黑名单（读 DB）。
- Produces: catalog 设备行禁用项行尾追加 ` ⛔AI禁操作`；controls 明细跳过禁用项（不给 LLM 操作引导）。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_main_catalog_notes.py` 末尾追加（复用其 `_init_db_singleton` fixture 模式，该文件已有同名 autouse fixture）：

```python
@pytest.mark.asyncio
async def test_refresh_catalog_marks_disabled_and_skips_controls(tmp_path):
    """禁用实体行尾标 ⛔，且不出现在 controls 明细里。"""
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
        await Database.init()
    await Database.get().emoji_pref_upsert("entity_operable", "lock.tong_suo", "0")

    fake_dev = {
        "entity_id": "lock.tong_suo", "state": "locked", "domain": "lock",
        "attributes": {"friendly_name": "童锁"},
    }
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": [
        {"name": "童锁", "model": None, "area_name": None, "entities": [fake_dev]},
    ]})
    mock_ha_service.get_all_devices = AsyncMock(return_value=[fake_dev])
    mock_ha_service.get_service_defs = AsyncMock(return_value={
        "lock": {"lock": {"fields": ["entity_id"]}, "unlock": {"fields": ["entity_id"]}},
    })
    mock_ha_client = MagicMock()

    with patch("app.main.ha_service", mock_ha_service), \
         patch("app.main.ha_client", mock_ha_client), \
         patch("app.services.entity_controls.resolve_controls", return_value={"lock": {}, "unlock": {}}):
        await __import__("app.main", fromlist=["_refresh_ha_catalog"])._refresh_ha_catalog()

    from app.main import _ha_catalog_cache_ref, _ha_controls_cache_ref
    catalog = _ha_catalog_cache_ref[0]
    controls = _ha_controls_cache_ref[0]
    assert "⛔AI禁操作" in catalog
    assert "lock.tong_suo" in catalog
    # controls 明细不含禁用项
    assert "lock.tong_suo" not in controls
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest tests/test_main_catalog_notes.py::test_refresh_catalog_marks_disabled_and_skips_controls -v`
Expected: FAIL（catalog 里没有 ⛔，controls 里仍含 lock.tong_suo）。

- [ ] **Step 3: 实现 catalog 标注 + controls 跳过**

在 `app/main.py` 的 `_refresh_ha_catalog` 中：

(a) 在 notes_map 读取块（约 line 242-247，`notes_map = {}` ... `except Exception: logger.warning("Failed to load entity notes for catalog")`）之后，追加 operable 黑名单读取：

```python
        operable_disabled: dict[str, str] = {}
        try:
            from .core.database import Database
            operable_disabled = await Database.get().prefs_get_by_scope("entity_operable")
        except Exception:  # noqa: BLE001
            logger.warning("Failed to load entity_operable for catalog")
```

(b) 找到可控实体目录行构造（约 line 275-279）：
```python
            for e in controllable:
                eid = e["entity_id"]
                lines.append(
                    f"- {eid} (类型:{e['domain']}, 状态:{e['state']}) 名称:{dev_name}"
                )
```
改为（行尾追加 ⛔ 标记）：
```python
            for e in controllable:
                eid = e["entity_id"]
                marker = " ⛔AI禁操作" if eid in operable_disabled else ""
                lines.append(
                    f"- {eid} (类型:{e['domain']}, 状态:{e['state']}) 名称:{dev_name}{marker}"
                )
```

(c) 找到 controls 明细内层循环（约 line 284-291）：
```python
                for e in controllable:
                    flat = next((d for d in devices if d["entity_id"] == e["entity_id"]), None)
```
改为（跳过禁用项）：
```python
                for e in controllable:
                    if e["entity_id"] in operable_disabled:
                        continue
                    flat = next((d for d in devices if d["entity_id"] == e["entity_id"]), None)
```

- [ ] **Step 4: 运行，确认通过**

Run: `python -m pytest tests/test_main_catalog_notes.py -v`
Expected: PASS（含原有 note 测试 + 新增 operable 测试）

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main_catalog_notes.py
git commit -m "feat(backend): catalog 标注 ⛔ 禁用项 + controls 跳过"
```

---

### Task 5: prompt 注入权限约束文案

**Files:**
- Modify: `app/services/prompt_service.py`（`build_system_prompt` 的 device_catalog 注入段，约 line 196-201）
- Test: `tests/test_prompt_service.py`（追加用例）

**Interfaces:**
- Produces: 当注入 device_catalog 时，附加「标 ⛔ 的禁操作 + 多候选优先选未标 ⛔ 的」约束文案。该段独立于可被用户覆盖的 GUIDELINES，始终注入。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_prompt_service.py` 末尾追加：

```python
@pytest.mark.asyncio
async def test_system_prompt_includes_operable_constraint():
    """注入 device_catalog 时，system prompt 含白名单权限约束文案。"""
    from app.services.prompt_service import build_system_prompt
    prompt = await build_system_prompt(
        device_catalog="# 童锁\n- lock.tong_suo (类型:lock, 状态:locked) 名称:童锁 ⛔AI禁操作"
    )
    assert "⛔" in prompt
    assert "多候选" in prompt or "优先" in prompt
```

- [ ] **Step 2: 运行，确认失败**

Run: `python -m pytest tests/test_prompt_service.py::test_system_prompt_includes_operable_constraint -v`
Expected: FAIL（当前 catalog 注入段无权限文案）。

- [ ] **Step 3: 实现 prompt 文案**

在 `app/services/prompt_service.py` 的 `build_system_prompt` 中，找到 device_catalog 注入段（约 line 196-201）：
```python
    elif device_catalog:
        parts.append(
            f"\n当前 Home Assistant 可用设备（按物理设备分组，# 开头是设备名，"
            f"下方 - 是它包含的可控实体。向用户介绍有哪些设备时，以 # 开头的物理设备"
            f"为单位，不要把同一设备下的传感器/属性拆成多个独立设备念出）：\n{device_catalog}"
        )
```
改为（追加权限约束句）：
```python
    elif device_catalog:
        parts.append(
            f"\n当前 Home Assistant 可用设备（按物理设备分组，# 开头是设备名，"
            f"下方 - 是它包含的可控实体。向用户介绍有哪些设备时，以 # 开头的物理设备"
            f"为单位，不要把同一设备下的传感器/属性拆成多个独立设备念出）：\n{device_catalog}\n"
            f"设备操作权限：标 ⛔AI禁操作 的实体被用户禁止 AI 操作，即使可见也绝不能 call_service，"
            f"应告知用户需手动处理；当用户指令能匹配多个实体时，优先选未标 ⛔ 的去操作，"
            f"被禁的自然跳过。"
        )
```

- [ ] **Step 4: 运行，确认通过**

Run: `python -m pytest tests/test_prompt_service.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add app/services/prompt_service.py tests/test_prompt_service.py
git commit -m "feat(backend): system prompt 注入 operable 白名单约束文案"
```

---

### Task 6: 前端 composable operable 状态管理

**Files:**
- Modify: `frontend/src/composables/useEntityMeta.js`
- Test: `frontend/tests/composables/useEntityMeta.operable.test.js`（新建）

**Interfaces:**
- Produces: `useEntityMeta` 增加 `entityOperable`（ref，`{entity_id: "0"}` 禁用集合）、`loadEntityOperable()`、`toggleOperable(entityId)`。`toggleOperable` 调 `PUT /api/ha/entity-operable`，乐观更新本地集合。

- [ ] **Step 1: 写失败测试（新建）**

创建 `frontend/tests/composables/useEntityMeta.operable.test.js`：

```javascript
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ref } from 'vue'
import { useEntityMeta } from '../../src/composables/useEntityMeta'

describe('useEntityMeta operable', () => {
  beforeEach(() => {
    global.fetch = vi.fn()
  })

  it('loadEntityOperable 填充 entityOperable', async () => {
    global.fetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ data: { disabled: { 'lock.tong_suo': '0' } } }),
    })
    const { entityOperable, loadEntityOperable } = useEntityMeta(ref(null), ref(null))
    await loadEntityOperable()
    expect(entityOperable.value['lock.tong_suo']).toBe('0')
  })

  it('toggleOperable 禁用→发 PUT operable:false 并写入本地', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: {} }) })
    const { entityOperable, toggleOperable } = useEntityMeta(ref(null), ref(null))
    await toggleOperable('light.bed') // 当前允许 → 禁用
    expect(entityOperable.value['light.bed']).toBe('0')
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/entity-operable', expect.objectContaining({
      method: 'PUT',
      body: JSON.stringify({ entity_id: 'light.bed', operable: false }),
    }))
  })

  it('toggleOperable 恢复→发 PUT operable:true 并删除本地', async () => {
    global.fetch.mockResolvedValueOnce({ ok: true, json: async () => ({ data: {} }) })
    const { entityOperable, toggleOperable } = useEntityMeta(ref(null), ref(null))
    entityOperable.value['lock.tong_suo'] = '0' // 预置为禁用
    await toggleOperable('lock.tong_suo') // 禁用 → 恢复
    expect(entityOperable.value['lock.tong_suo']).toBeUndefined()
    expect(global.fetch).toHaveBeenCalledWith('/api/ha/entity-operable', expect.objectContaining({
      body: JSON.stringify({ entity_id: 'lock.tong_suo', operable: true }),
    }))
  })
})
```

- [ ] **Step 2: 运行，确认失败**

Run: `cd frontend && npx vitest run tests/composables/useEntityMeta.operable.test.js`
Expected: FAIL（`loadEntityOperable is not a function`）

- [ ] **Step 3: 实现 composable**

在 `frontend/src/composables/useEntityMeta.js` 中：

(a) 在 `entityNotes` 声明（约 line 17-19）后追加 operable 状态：

```javascript
  const entityOperable = ref({})        // {entity_id: "0"} — 被禁用 AI 操作的实体集合
```

(b) 在 `loadEntityNotes` 函数（约 line 69-77）后追加两个函数：

```javascript
  // ======================== AI 可操作权限 ========================

  async function loadEntityOperable() {
    try {
      const res = await fetch('/api/ha/entity-operable', { credentials: 'include' })
      const json = await res.json()
      entityOperable.value = json.data?.disabled || {}
    } catch (e) {
      console.error('Failed to load entity operable:', e)
    }
  }

  async function toggleOperable(entityId) {
    // 当前禁用（在集合里）→ 切到允许；当前允许 → 切到禁用
    const isDisabled = entityOperable.value[entityId] !== undefined
    const operable = isDisabled // true=恢复允许，false=禁用
    try {
      await fetch('/api/ha/entity-operable', {
        method: 'PUT',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: entityId, operable }),
      })
      if (operable) {
        delete entityOperable.value[entityId]
      } else {
        entityOperable.value[entityId] = '0'
      }
    } catch (e) {
      console.error('Failed to toggle operable:', e)
    }
  }
```

(c) 在 return 对象（约 line 119-134）追加导出：

```javascript
    entityOperable,
    loadEntityOperable,
    toggleOperable,
```

- [ ] **Step 4: 运行，确认通过**

Run: `cd frontend && npx vitest run tests/composables/useEntityMeta.operable.test.js`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
git add frontend/src/composables/useEntityMeta.js frontend/tests/composables/useEntityMeta.operable.test.js
git commit -m "feat(frontend): useEntityMeta 加 operable 权限状态管理"
```

---

### Task 7: 前端子实体行绿/红徽章 UI

**Files:**
- Modify: `frontend/src/views/HAListView.vue`（composable 解构、onMounted、控制区 entity-row 模板、style）
- Test: 手动验证 + 构建校验（HAListView 深度依赖 modal/Teleport，单测 mount 成本高，改用构建校验 + 手动验证清单）

**Interfaces:**
- Consumes: Task 6 的 `entityOperable` / `loadEntityOperable` / `toggleOperable`。
- Produces: 设备详情弹窗「控制」区每个可控实体行右侧显示绿（AI 可操作）/红（禁止 AI）徽章，点击切换；只读实体行不显示。

- [ ] **Step 1: 解构 composable 新增项**

在 `frontend/src/views/HAListView.vue` 的 `useEntityMeta(...)` 解构（约 line 26-31）追加：

```javascript
const {
  entityAliases, editingName, nameInput,
  entityNotes, noteInput, editingNote,
  entityOperable,
  loadEntityAliases, startEditName, saveName, resetName,
  loadEntityNotes, startEditNote, saveNote, resetNote,
  loadEntityOperable, toggleOperable,
} = useEntityMeta(selectedEntity, selectedDevice)
```

- [ ] **Step 2: onMounted 加载 operable**

在 `onMounted`（约 line 650-655）追加调用：

```javascript
onMounted(() => {
  loadEntities()
  loadEmojiPrefs()
  loadEntityAliases()
  loadEntityNotes()
  loadEntityOperable()
})
```

- [ ] **Step 3: 控制区 entity-row 加徽章**

找到控制区 entity-row（约 line 742-753，`v-for="ent in (...).filter(e => isControllable(e))"` 那段），在 `<BaseToggle .../>` **之前**插入徽章：

```html
                    <span
                      class="ai-operable-badge"
                      :class="{ allowed: entityOperable[ent.entity_id] === undefined, disabled: entityOperable[ent.entity_id] !== undefined }"
                      @click.stop="toggleOperable(ent.entity_id)"
                      :title="entityOperable[ent.entity_id] === undefined ? '允许 AI 操作，点击禁止' : '已禁止 AI 操作，点击恢复'"
                    >{{ entityOperable[ent.entity_id] === undefined ? 'AI 可操作' : '禁止 AI' }}</span>
```

完整 entity-row 应为（在原结构基础上插入徽章行）：

```html
                  <div
                    v-for="ent in (selectedDevice.entities || []).filter(e => isControllable(e))"
                    :key="ent.entity_id"
                    class="entity-row"
                    :class="{ active: selectedEntity && selectedEntity.entity_id === ent.entity_id, on: isOn(ent) }"
                    @click="selectEntity(ent)"
                  >
                    <span class="entity-icon" :style="{ color: getDomainIcon(ent.entity_id).color }">{{ getDomainIcon(ent.entity_id).icon }}</span>
                    <span class="entity-name">{{ ent.name || ent.entity_id }}</span>
                    <span class="entity-state">{{ getCardPrimary(ent) }}</span>
                    <span
                      class="ai-operable-badge"
                      :class="{ allowed: entityOperable[ent.entity_id] === undefined, disabled: entityOperable[ent.entity_id] !== undefined }"
                      @click.stop="toggleOperable(ent.entity_id)"
                      :title="entityOperable[ent.entity_id] === undefined ? '允许 AI 操作，点击禁止' : '已禁止 AI 操作，点击恢复'"
                    >{{ entityOperable[ent.entity_id] === undefined ? 'AI 可操作' : '禁止 AI' }}</span>
                    <BaseToggle v-if="isToggleable(ent)" :modelValue="isOn(ent)" @click.stop @update:modelValue="toggleDevice(ent)" />
                  </div>
```

- [ ] **Step 4: 加徽章样式（绿/红）**

在 `<style scoped>` 内（如 `.entity-state` 规则之后，约 line 1340 附近）追加：

```css
.ai-operable-badge {
  font-size: var(--text-xs, 11px);
  font-weight: var(--weight-medium, 500);
  padding: 2px 8px;
  border-radius: var(--radius-full, 999px);
  cursor: pointer;
  user-select: none;
  white-space: nowrap;
  transition: opacity var(--duration-fast, 0.15s);
}
.ai-operable-badge.allowed {
  color: var(--color-success, #2ecc71);
  background: var(--color-success-bg, rgba(46,204,113,0.15));
}
.ai-operable-badge.disabled {
  color: #fff;
  background: var(--color-danger, #e74c3c);
}
.ai-operable-badge:hover { opacity: 0.8; }
```

- [ ] **Step 5: 构建校验**

Run: `cd frontend && npm run build`
Expected: 构建成功（无编译错误）。

- [ ] **Step 6: 手动验证清单**

启动前后端，在设备页逐项确认：
- [ ] 打开任一设备详情弹窗，「控制」区每个可控实体行右侧出现徽章；只读实体（信息区）无徽章。
- [ ] 默认所有徽章为绿色「AI 可操作」。
- [ ] 点击某实体徽章 → 变红色「禁止 AI」；再点 → 回到绿色「AI 可操作」（可逆）。
- [ ] 在聊天里让 AI 操作刚禁用的实体 → AI 回复被禁止/需手动（而非真去操作）。
- [ ] 让 AI 操作一个有歧义的指令（如多个灯）→ 正常执行未禁用的那个。

- [ ] **Step 7: Commit**

```bash
git add frontend/src/views/HAListView.vue
git commit -m "feat(frontend): 设备详情子实体行加 AI 可操作绿/红徽章"
```

---

## Self-Review

**1. Spec coverage:**
- §2 粒度（子实体）→ Task 1 schema + Task 7 徽章按 entity_id ✓
- §2 默认可操作（黑名单）→ Task 1 只存禁用项 ✓
- §2 拦截策略 A（硬拦截 + 软标注）→ Task 2（硬）+ Task 3/4/5（软）✓
- §2 优先策略（多候选优先）→ Task 5 prompt 文案 ✓
- §2 可逆性 → Task 1 PUT operable=true 删除记录 + Task 6 toggle 双向 ✓
- §2 DB 故障放行 → Task 2 except 放行 ✓
- §2 prompt 标注保留 → Task 4 行尾 ⛔ + Task 5 文案 ✓
- §2 仅可控实体显示 → Task 7 徽章在控制区 `filter(isControllable)` ✓
- §4.6 校验顺序 → Task 2 插入位置在真实性校验后、语义校验前 ✓

**2. Placeholder scan:** 无 TBD/TODO；每个代码步骤含完整代码。Task 7 的前端 UI 用手动验证清单替代单测（已在 task 注明理由：modal+Teleport mount 成本高），构建校验 + 手动清单覆盖。

**3. Type consistency:**
- scope 名全计划统一 `entity_operable` ✓
- value 统一 `"0"` ✓
- 路由路径统一 `/api/ha/entity-operable`（后端 `/ha/entity-operable` + 前缀 `/api`）✓
- 工具名 `ha_devices___call_service` / `ha_devices___get_entities`（client_id `ha_devices` + `___` + tool_name）✓
- composable 导出名 `entityOperable` / `loadEntityOperable` / `toggleOperable` 在 Task 6 定义、Task 7 消费一致 ✓
