# 设备自定义备注（阶段一）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户给任意设备写一段自由文本备注（如"继电器 ON=关门，OFF=开门"），备注注入到 LLM 看到的设备可控项文本里，使 AI 正确理解和调用设备——零架构风险，不动现有全量注入。

**Architecture:** 复用 `emoji_preferences` 表（新 scope `entity_note`）存备注；在 `controls_to_text` 增加可选 `note` 参数把备注拼进设备文本（三条消费链路——后台预编译、规则生成、get_entities 工具——同时受益）；新增 `get_device_manual` 工具按需拉单台详情+备注；新增 `GET/PUT /ha/entity-notes` REST API；前端在设备详情页加备注 textarea。

**Tech Stack:** Python / FastAPI / aiosqlite / pytest-asyncio（后端）；Vue 3 Composition API + `<script setup>`（前端）。

**参考文档:** `docs/superpowers/specs/2026-08-05-device-semantic-mapping-design.md`（第 5 节「阶段一详细设计」是本计划依据）。

## Global Constraints

- 复用 `emoji_preferences` 表的 `scope="entity_note"`，**不新建表**（`entity_alias` 已验证该机制）。
- 备注是 Aether 私有，**不同步到 HA**（HA 无此概念；这点比 `entity_alias` 简单）。
- 备注可多行，后端不硬限长度；前端软限 200 字（textarea `maxlength`）。
- 备注为空时不输出备注行，行为与现状完全一致（零回归）。
- 备注只影响 AI 认知，**不改前端状态显示**（UI 仍按 HA 原始 state 显示）。
- `controls_to_text` 被 `main.py:277`、`rule_service.py:213`、`get_entities` 工具三处调用，备注注入必须经此一处入口同时覆盖三条链路。
- `rule_service` 那条路**不能改瘦目录**（本计划也不涉及，仅传 note）。
- 每个任务结束必须有独立可运行的测试通过，并 commit。

---

## 文件结构

| 文件 | 责任 | 本计划动作 |
|------|------|-----------|
| `app/services/entity_controls.py` | 设备控件推导 + 文本生成 | 改：`controls_to_text` 加 `note` 参数 |
| `app/main.py` | 后台 catalog 预编译 | 改：`_refresh_ha_catalog` 读备注并传给 `controls_to_text` |
| `app/services/rule_service.py` | 自动化规则生成 | 改：规则生成时读备注传给 `controls_to_text` |
| `app/tools.py` | MCP 工具注册 | 改：`get_entities` 读备注传 note；新增 `_register_ha_get_device_manual` |
| `app/schema/api_schemas.py` | Pydantic 请求模型 | 改：新增 `EntityNoteRequest` |
| `app/routes/ha_routes.py` | HA REST 路由 | 改：新增 `GET/PUT /ha/entity-notes` |
| `tests/test_entity_controls.py` | 控件文本测试 | 改：加 note 注入测试 |
| `tests/test_rule_service.py` | 规则生成测试 | 改：加备注注入断言 |
| `tests/test_ha_routes.py` | HA 路由测试 | 改：加 entity-notes 路由测试 |
| `tests/test_tools_get_device_manual.py` | 新工具测试 | 新建 |
| `frontend/src/views/HAListView.vue` | 设备列表页 | 改：加备注加载/保存/textarea UI |

---

## Task 1: `controls_to_text` 注入备注

**目标**：单点改造，让备注以明确的标记行拼进设备文本，所有三条消费链路经此同时受益。

**Files:**
- Modify: `app/services/entity_controls.py:207-244`（`controls_to_text`）
- Test: `tests/test_entity_controls.py`（`TestControlsToText` 类内新增）

**Interfaces:**
- Produces: `controls_to_text(entity, controls, indent=0, note: str | None = None) -> str`。新增可选 `note` 参数；`note` 为空（None 或空串）时不输出备注行（向后兼容，现有调用方不传 note 行为不变）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_entity_controls.py` 末尾的 `TestControlsToText` 类内追加（最后一个方法之后、类结束之前）：

```python
    def test_note_injected_when_present(self):
        """note 非空时，标题行下方插入备注行（优先级最高标记）。"""
        entity = {"entity_id": "switch.gate", "attributes": {"friendly_name": "大门"}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        text = controls_to_text(entity, controls, note="ON=关门, OFF=开门")
        assert "大门 (switch.gate)" in text
        assert "备注" in text
        assert "ON=关门, OFF=开门" in text
        # 备注行在标题行之后、可控项之前
        title_idx = text.index("大门 (switch.gate)")
        note_idx = text.index("ON=关门, OFF=开门")
        action_idx = text.index("Turn On")
        assert title_idx < note_idx < action_idx

    def test_note_omitted_when_none_or_empty(self):
        """note 为 None 或空串时不输出备注行（向后兼容）。"""
        entity = {"entity_id": "switch.gate", "attributes": {"friendly_name": "大门"}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        # None（默认值）
        text_default = controls_to_text(entity, controls)
        assert "备注" not in text_default
        # 空串
        text_empty = controls_to_text(entity, controls, note="")
        assert "备注" not in text_empty
        # 仅空白
        text_blank = controls_to_text(entity, controls, note="   ")
        assert "备注" not in text_blank

    def test_note_multiline_supported(self):
        """多行备注保留换行（每行都带备注前缀缩进）。"""
        entity = {"entity_id": "switch.gate", "attributes": {"friendly_name": "大门"}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        text = controls_to_text(entity, controls, note="第一行\n第二行")
        assert "第一行" in text
        assert "第二行" in text

    def test_note_with_indent(self):
        """indent>=1（子功能）时，备注行也带正确缩进。"""
        entity = {"entity_id": "switch.gate", "attributes": {}}
        controls = {"turn_on": {"type": "action", "service": "turn_on", "param": None}}
        text = controls_to_text(entity, controls, indent=1, note="子功能备注")
        # 子功能标题行存在
        assert "子功能 switch.gate:" in text
        assert "子功能备注" in text
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_entity_controls.py::TestControlsToText::test_note_injected_when_present -v`
Expected: FAIL —— `TypeError: controls_to_text() got an unexpected keyword argument 'note'` 或备注未出现。

- [ ] **Step 3: 实现 `controls_to_text` 备注注入**

修改 `app/services/entity_controls.py` 的 `controls_to_text`（L207 起）。完整替换函数签名和函数体上半部分：

把当前：
```python
def controls_to_text(entity: dict, controls: dict, indent: int = 0) -> str:
    """把 resolve_controls 的结果转成给 LLM 看的中文可控项文本。

    Args:
        entity: 设备/实体 dict（取 friendly_name 和 entity_id）
        controls: resolve_controls 返回的 {attr: ctrl_dict}
        indent: 缩进层级。0=独立块（标题用 friendly_name + entity_id）；
            >=1=作为子项，标题用 entity_id（不用 friendly_name——MIoT 子实体名常带
            噪声如「麦克风 静音」，会被 LLM 当独立设备念出），由上层用设备名作总标题。
    """
    eid = entity["entity_id"]
    pad = "  " * indent
    if indent == 0:
        name = (entity.get("attributes") or {}).get("friendly_name", "") or eid
        lines = [f"{name} ({eid})"]
    else:
        lines = [f"{pad}子功能 {eid}:"]
    if not controls:
        lines.append(f"{pad}  (no controls)")
        return "\n".join(lines)
```

替换为：
```python
def controls_to_text(entity: dict, controls: dict, indent: int = 0, note: str | None = None) -> str:
    """把 resolve_controls 的结果转成给 LLM 看的中文可控项文本。

    Args:
        entity: 设备/实体 dict（取 friendly_name 和 entity_id）
        controls: resolve_controls 返回的 {attr: ctrl_dict}
        indent: 缩进层级。0=独立块（标题用 friendly_name + entity_id）；
            >=1=作为子项，标题用 entity_id（不用 friendly_name——MIoT 子实体名常带
            噪声如「麦克风 静音」，会被 LLM 当独立设备念出），由上层用设备名作总标题。
        note: 用户自定义备注（如继电器反转语义）。非空时在标题行下方、可控项之前
            插入「备注」行，让 LLM 看到设备的怪癖；为空（None/空串/纯空白）时不输出
            （保持现状，零回归）。三条消费链路（后台预编译/规则生成/get_entities）
            经此一处同时注入。
    """
    eid = entity["entity_id"]
    pad = "  " * indent
    if indent == 0:
        name = (entity.get("attributes") or {}).get("friendly_name", "") or eid
        lines = [f"{name} ({eid})"]
    else:
        lines = [f"{pad}子功能 {eid}:"]
    # 备注行：在标题之后、可控项之前。多行备注逐行带前缀（缩进对齐可控项层级）。
    if note and note.strip():
        for ln in note.split("\n"):
            lines.append(f"{pad}  备注（用户自定义，优先级最高）：{ln}")
    if not controls:
        lines.append(f"{pad}  (no controls)")
        return "\n".join(lines)
```

（函数下半部分 `for attr, ctrl in controls.items():` 循环**不动**，原样保留。）

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_entity_controls.py::TestControlsToText -v`
Expected: PASS（含 4 个新测试 + 原有测试全过）。

再跑全量确认无回归：
Run: `python -m pytest tests/test_entity_controls.py -v`
Expected: PASS（所有测试）。

- [ ] **Step 5: Commit**

```bash
git add app/services/entity_controls.py tests/test_entity_controls.py
git commit -m "feat(entity_controls): controls_to_text 支持 note 参数注入用户备注"
```

---

## Task 2: 后台预编译链路接入备注

**目标**：`_refresh_ha_catalog` 读全部备注，按 entity_id 传给 `controls_to_text`。主聊天 system prompt 立即受益（60 秒刷新周期后生效）。

**Files:**
- Modify: `app/main.py:214-286`（`_refresh_ha_catalog`，重点是 L270-279 的 controls 生成段）
- Test: 新建 `tests/test_main_catalog_notes.py`

**Interfaces:**
- Consumes: `controls_to_text(entity, controls, indent, note)`（Task 1 产出）
- Consumes: `Database.get().prefs_get_by_scope("entity_note") -> dict[entity_id, note_text]`（`database.py:506` 已存在，无需新建）
- Produces: `_ha_controls_cache_ref[0]` 含备注行的 controls 文本（无新增公开接口，仅行为变化）

- [ ] **Step 1: 写失败测试**

新建 `tests/test_main_catalog_notes.py`：

```python
"""Tests for _refresh_ha_catalog note injection (Task 2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _init_db_singleton(tmp_path):
    """每个测试用一个临时 Database 单例，避免污染全局。"""
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    Database._write_lock = None
    yield
    if Database._db:
        import asyncio
        try:
            asyncio.get_event_loop().run_until_complete(Database._db.close())
        except Exception:
            pass


@pytest.mark.asyncio
async def test_refresh_catalog_injects_note(tmp_path):
    """_refresh_ha_catalog 把 entity_note 备注拼进 controls 缓存。"""
    from app.core.database import Database
    with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
        await Database.init()
    await Database.get().emoji_pref_upsert("entity_note", "switch.gate", "ON=关门, OFF=开门")

    # 构造 mock ha_service：单设备 + 单可控实体
    fake_dev = {
        "entity_id": "switch.gate", "state": "off", "domain": "switch",
        "attributes": {"friendly_name": "大门"},
    }
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": [
        {"name": "大门", "model": None, "area_name": None,
         "entities": [fake_dev]},
    ]})
    mock_ha_service.get_all_devices = AsyncMock(return_value=[fake_dev])
    mock_ha_service.get_service_defs = AsyncMock(return_value={
        "switch": {
            "turn_on": {"fields": ["entity_id"]},
            "turn_off": {"fields": ["entity_id"]},
        },
    })
    mock_ha_client = MagicMock()

    catalog_ref = ["c"]
    controls_ref = [""]

    with patch("app.main.ha_service", mock_ha_service), \
         patch("app.main.ha_client", mock_ha_client), \
         patch("app.main._ha_catalog_cache_ref", catalog_ref), \
         patch("app.main._ha_controls_cache_ref", controls_ref):
        from app.main import _refresh_ha_catalog
        await _refresh_ha_catalog()

    assert "ON=关门, OFF=开门" in controls_ref[0]
    assert "备注" in controls_ref[0]
    assert "switch.gate" in controls_ref[0]


@pytest.mark.asyncio
async def test_refresh_catalog_no_notes_no_change(tmp_path):
    """无备注时 controls 缓存不含备注行（零回归）。"""
    from app.core.database import Database
    with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
        await Database.init()

    fake_dev = {
        "entity_id": "light.lamp", "state": "on", "domain": "light",
        "attributes": {"friendly_name": "床头灯"},
    }
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": [
        {"name": "床头灯", "model": None, "area_name": None, "entities": [fake_dev]},
    ]})
    mock_ha_service.get_all_devices = AsyncMock(return_value=[fake_dev])
    mock_ha_service.get_service_defs = AsyncMock(return_value={
        "light": {"turn_on": {"fields": ["entity_id"]}},
    })
    mock_ha_client = MagicMock()

    catalog_ref = [""]
    controls_ref = [""]

    with patch("app.main.ha_service", mock_ha_service), \
         patch("app.main.ha_client", mock_ha_client), \
         patch("app.main._ha_catalog_cache_ref", catalog_ref), \
         patch("app.main._ha_controls_cache_ref", controls_ref):
        from app.main import _refresh_ha_catalog
        await _refresh_ha_catalog()

    assert "备注" not in controls_ref[0]
    # 但设备可控项仍正常生成
    assert "床头灯" in controls_ref[0]
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_main_catalog_notes.py -v`
Expected: FAIL —— `assert "ON=关门, OFF=开门" in controls_ref[0]`（备注未注入）。

- [ ] **Step 3: 修改 `_refresh_ha_catalog`**

修改 `app/main.py` 的 `_refresh_ha_catalog`（L214 起）。在函数体内、`for dev in grouped.get("devices", []):` 循环**之前**读取备注字典；在循环内调 `controls_to_text` 处传入 `note`。

定位当前代码（L224-235 附近，`from .services.entity_controls import resolve_controls, controls_to_text` 之后、循环之前）：
```python
        grouped = await ha_service.get_all_devices_grouped()
        devices = await ha_service.get_all_devices()
        raw_svc_defs = await ha_service.get_service_defs(
            ha_client, domains=set(d.get("domain", "") for d in devices)
        )
        # 诊断/属性类 domain：不作为独立设备条目念给用户
        DIAGNOSTIC_DOMAINS = {"sensor", "binary_sensor"}
        lines = []
        controls_lines = []
        for dev in grouped.get("devices", []):
```

替换为（在 `controls_lines = []` 后、循环前插入备注读取；循环签名不变）：
```python
        grouped = await ha_service.get_all_devices_grouped()
        devices = await ha_service.get_all_devices()
        raw_svc_defs = await ha_service.get_service_defs(
            ha_client, domains=set(d.get("domain", "") for d in devices)
        )
        # 诊断/属性类 domain：不作为独立设备条目念给用户
        DIAGNOSTIC_DOMAINS = {"sensor", "binary_sensor"}
        lines = []
        controls_lines = []
        # 用户自定义备注（entity_note scope）：按 entity_id 查 dict 注入 controls。
        # 不常驻 DB 连接——每 60 秒刷新周期读一次即可（备注变更最多 60 秒生效）。
        notes_map: dict[str, str] = {}
        try:
            from .core.database import Database
            notes_map = await Database.get().prefs_get_by_scope("entity_note")
        except Exception:  # noqa: BLE001
            logger.warning("Failed to load entity notes for catalog")
        for dev in grouped.get("devices", []):
```

然后定位循环内 controls 生成段（L270-279 附近）：
```python
            if raw_svc_defs:
                dev_controls_lines = [f"{dev_name}:"]
                for e in controllable:
                    flat = next((d for d in devices if d["entity_id"] == e["entity_id"]), None)
                    if flat:
                        controls = resolve_controls(flat, raw_svc_defs)
                        if controls:
                            dev_controls_lines.append(controls_to_text(flat, controls, indent=1))
                if len(dev_controls_lines) > 1:
                    controls_lines.append("\n".join(dev_controls_lines))
```

替换为（仅 `controls_to_text` 调用加 `note=` 参数）：
```python
            if raw_svc_defs:
                dev_controls_lines = [f"{dev_name}:"]
                for e in controllable:
                    flat = next((d for d in devices if d["entity_id"] == e["entity_id"]), None)
                    if flat:
                        controls = resolve_controls(flat, raw_svc_defs)
                        if controls:
                            dev_controls_lines.append(
                                controls_to_text(flat, controls, indent=1, note=notes_map.get(e["entity_id"]))
                            )
                if len(dev_controls_lines) > 1:
                    controls_lines.append("\n".join(dev_controls_lines))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_main_catalog_notes.py -v`
Expected: PASS（2 个测试）。

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_main_catalog_notes.py
git commit -m "feat(main): _refresh_ha_catalog 注入用户自定义备注到 controls 缓存"
```

---

## Task 3: 规则生成链路接入备注

**目标**：`rule_service` 生成自动化规则时也读备注传给 `controls_to_text`（建规则时 LLM 也能看到"开门要调 turn_off"）。

**架构事实**（执行前须知）：`RuleService` 不直接持有 `ha_service`，而是通过三个 provider 注入数据：
- `set_ha_devices_provider(async () -> list[dict])` → 完整设备数据（带 attributes）
- `set_ha_services_provider(() -> dict)` → HA 服务定义
- `set_ha_catalog_provider(() -> str)` → catalog 字符串

`controls_to_text` 在 `_prepare_rule_context`（L197-214）里被调用，传入的 `d` 是来自 `_ha_devices_provider` 的设备 dict（含 `entity_id`）。因此测试要用 `svc.set_ha_devices_provider(...)` 注入设备，而不是 patch `ha_service`。

**Files:**
- Modify: `app/services/rule_service.py:197-214`（`_prepare_rule_context` 的 controls 生成段）
- Test: `tests/test_rule_service.py`（新增 `TestRuleServiceNotes` 类）

**Interfaces:**
- Consumes: `controls_to_text(entity, controls, note)`（Task 1 产出）
- Consumes: `Database.get().prefs_get_by_scope("entity_note")`（`database.py:506`）
- Produces: 规则生成 prompt 里的 `controls_text` 含备注行

- [ ] **Step 1: 写失败测试**

在 `tests/test_rule_service.py` 末尾新增（参考 `TestRuleServicePerUser` 的 mock client + provider 注入风格）：

```python


class TestRuleServiceNotes:
    """规则生成注入用户自定义备注（Task 3）。"""

    @pytest.mark.asyncio
    async def test_build_rule_injects_note_into_prompt(self):
        """rule_service 把 entity_note 备注拼进 system prompt，LLM 据此正确选 service。"""
        # Mock Database.get().prefs_get_by_scope 返回备注
        mock_db = MagicMock()
        mock_db.prefs_get_by_scope = AsyncMock(return_value={
            "switch.gate": "ON=关门, OFF=开门。用户说开门时调 turn_off",
        })

        # mock client：截获 chat 调用，断言 prompt 含备注
        mock_client = MagicMock()
        mock_client.enabled = True
        captured = {}

        async def fake_chat(messages, max_tokens=None, **kw):
            captured["prompt"] = messages[0]["content"] if messages else ""
            return '{"name":"r","type":"vision","condition":"有人","actions":[],"action_descriptions":[],"cooldown_seconds":10,"summary":""}'

        mock_client.chat = AsyncMock(side_effect=fake_chat)
        svc = RuleService(client=mock_client)

        async def _devices():
            return [
                {"entity_id": "switch.gate", "state": "off", "domain": "switch",
                 "name": "大门", "attributes": {"friendly_name": "大门"}},
            ]

        async def _services():
            return {"switch": {"turn_on": ["entity_id"], "turn_off": ["entity_id"]}}

        # 注入 devices provider（含 attributes 的完整设备）
        svc.set_ha_devices_provider(_devices)
        svc.set_ha_services_provider(_services)
        svc.set_ha_catalog_provider(lambda: "- switch.gate (类型:switch, 状态:off) 名称:大门")

        with patch("app.core.database.Database.get", return_value=mock_db), \
             patch("app.core.key_resolver.resolve_key_for_role_user", new=AsyncMock(return_value=None)):
            await svc.build_rule("大门", user_id="u1")

        assert "ON=关门, OFF=开门" in captured["prompt"]
        assert "备注" in captured["prompt"]
```

> **脚手架对齐说明（已验证）**：上面这段测试经 commit `6ee3fac` 验证可跑通。注意几个必须与真实 API 对齐的点（计划早期版本曾写错，已修正）：
> 1. `_devices` / `_services` 必须是 `async def`——`_prepare_rule_context` 对它们 `await`（`rule_service.py:180/188`），生产 wiring（`main.py:465-475`）传的就是 `async def`。
> 2. `fake_chat` 签名要能吃 `client.chat(messages, 20)` 的第二个位置参数（`max_tokens`，`rule_service.py:252`）。
> 3. `_services()` 的 per-service value 必须是 **list** `["entity_id"]`（不是 `{"fields": [...]}` dict）——生产 wiring 是 `{domain: {svc: info["fields"]}}`，`_prepare_rule_context` 会再包一层 `{"fields": fields}`。
> 4. `build_rule` 的 query 必须能命中 `_filter_devices`（设备名"大门"→ query 用"大门"，不是"开门"），否则设备被过滤掉、`controls_text` 恒空、备注永不进 prompt。

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_rule_service.py::TestRuleServiceNotes -v`
Expected: FAIL —— `assert "ON=关门, OFF=开门" in captured["prompt"]` 失败（备注未注入 prompt）。

- [ ] **Step 3: 修改 `rule_service.py`**

定位 `_prepare_rule_context` 的 controls 生成段（L197-214，`controls_text = ""` 开始）。在 `raw_svc_defs = ...` 之后、`for d in full_devices:` 循环之前读备注：

当前：
```python
        controls_text = ""
        if full_devices and services_info:
            raw_svc_defs = {
                domain: {svc: {"fields": fields} for svc, fields in svcs.items()}
                for domain, svcs in services_info.items()
            }
            # 为所有 full_devices 预计算 _controls（用于校验 + 提示词）
            for d in full_devices:
                d["_controls"] = resolve_controls(d, raw_svc_defs)
            # domain 过滤后生成中文 controls
            filtered_devices = _filter_devices(filter_text, full_devices)
            c_lines = []
            for d in filtered_devices:
                controls = d.get("_controls", {})
                if controls:
                    c_lines.append(controls_to_text(d, controls))
            controls_text = "\n\n".join(c_lines) if c_lines else ""
```

替换为（在 `raw_svc_defs = ...` 之后插入备注读取；`controls_to_text(d, controls)` 调用加 `note=`）：
```python
        controls_text = ""
        if full_devices and services_info:
            raw_svc_defs = {
                domain: {svc: {"fields": fields} for svc, fields in svcs.items()}
                for domain, svcs in services_info.items()
            }
            # 用户自定义备注：让规则生成 LLM 也看到设备怪癖（如继电器反转语义）
            notes_map: dict[str, str] = {}
            try:
                from ..core.database import Database
                notes_map = await Database.get().prefs_get_by_scope("entity_note")
            except Exception:  # noqa: BLE001
                logger.warning("Failed to load entity notes for rule generation", exc_info=True)
            # 为所有 full_devices 预计算 _controls（用于校验 + 提示词）
            for d in full_devices:
                d["_controls"] = resolve_controls(d, raw_svc_defs)
            # domain 过滤后生成中文 controls
            filtered_devices = _filter_devices(filter_text, full_devices)
            c_lines = []
            for d in filtered_devices:
                controls = d.get("_controls", {})
                if controls:
                    c_lines.append(
                        controls_to_text(d, controls, note=notes_map.get(d.get("entity_id", "")))
                    )
            controls_text = "\n\n".join(c_lines) if c_lines else ""
```

> **注**：执行前确认 `rule_service.py` 顶部已有 `import logging` + `logger = logging.getLogger(__name__)`，若无则补上（按现有风格）。

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_rule_service.py::TestRuleServiceNotes -v`
Expected: PASS。

再跑规则全量防回归：
Run: `python -m pytest tests/test_rule_service.py -v`
Expected: PASS（所有测试）。

- [ ] **Step 5: Commit**

```bash
git add app/services/rule_service.py tests/test_rule_service.py
git commit -m "feat(rule_service): 规则生成注入用户自定义备注"
```

---

## Task 4: 请求 schema `EntityNoteRequest`

**目标**：新增备注写入的 Pydantic 请求模型（照 `EntityAliasRequest` 写法）。

**Files:**
- Modify: `app/schema/api_schemas.py:274-283`（在 `EntityAliasRequest` 之后）

**Interfaces:**
- Produces: `EntityNoteRequest(entity_id: str, note: str = "")`（空串表示删除备注）

- [ ] **Step 1: 新增 schema**

在 `app/schema/api_schemas.py` 的 `EntityAliasRequest` 类之后（L283 之后、`# --------------- Chat ---------------` 之前）插入：

```python


class EntityNoteRequest(BaseModel):
    """设置实体备注的请求体（用户自定义备注，注入 LLM 认知，影响 AI 调用决策）。

    与 EntityAliasRequest 的差异：
    - 别名同步到 HA（entity_registry.name），备注是 Aether 私有、不同步 HA。
    - 别名改显示名，备注只影响 AI 理解，不改前端状态显示。
    - 备注可多行（自由文本，描述设备怪癖如继电器反转语义）。
    """

    entity_id: str = Field(..., description="HA 实体 ID")
    note: str = Field(default="", description="用户自定义备注，空串表示删除备注")

    @field_validator("entity_id", "note", mode="before")
    @classmethod
    def _strip_str(cls, v: object) -> str:
        return str(v).strip() if isinstance(v, str) else str(v)
```

- [ ] **Step 2: 验证 import 可用**

Run: `python -c "from app.schema.api_schemas import EntityNoteRequest; r = EntityNoteRequest(entity_id='switch.gate', note='测试'); print(r)"`
Expected: 打印 `entity_id='switch.gate' note='测试'`，无异常。

- [ ] **Step 3: Commit**

```bash
git add app/schema/api_schemas.py
git commit -m "feat(schema): 新增 EntityNoteRequest 模型"
```

---

## Task 5: REST API `GET/PUT /ha/entity-notes`

**目标**：照 `entity-aliases` 写法，新增备注的 GET/PUT 路由。**不同步 HA**，只写 Aether DB；写入后清状态缓存让后台刷新周期重读备注。

**Files:**
- Modify: `app/routes/ha_routes.py`（在 `set_entity_alias` 之后，约 L100）
- Test: `tests/test_ha_routes.py`（新增 `TestEntityNotesRoute` 类）

**Interfaces:**
- Consumes: `EntityNoteRequest`（Task 4）、`db.prefs_get_by_scope("entity_note")`、`db.emoji_pref_upsert/delete("entity_note", ...)`
- Produces: `GET /ha/entity-notes` → `ApiResponse(data={"notes": {entity_id: note}})`；`PUT /ha/entity-notes` → `ApiResponse(data={"entity_id", "note"})`

- [ ] **Step 1: 写失败测试**

在 `tests/test_ha_routes.py` 末尾新增（参考 `TestHAEntitiesRoute` 的 mock 风格；用真实临时 DB 保证 prefs CRUD 真跑通）：

```python
class TestEntityNotesRoute:
    """测试 /api/ha/entity-notes 路由（Task 5）。"""

    @pytest.fixture
    async def _db(self, tmp_path):
        from app.core.database import Database
        Database._instance = None
        Database._db = None
        Database._write_lock = None
        with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
            await Database.init()
            yield Database.get()

    @pytest.mark.asyncio
    async def test_get_entity_notes_empty(self, _db):
        from app.routes.ha_routes import get_entity_notes
        result = await get_entity_notes()
        assert result.code == "ok"
        assert result.data == {"notes": {}}

    @pytest.mark.asyncio
    async def test_put_then_get_entity_note(self, _db):
        from app.routes.ha_routes import get_entity_notes, set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        container = _mock_container(ha_service=MagicMock())
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="ON=关门, OFF=开门"), container=container)

        result = await get_entity_notes()
        assert result.data["notes"]["switch.gate"] == "ON=关门, OFF=开门"

    @pytest.mark.asyncio
    async def test_put_empty_note_deletes(self, _db):
        from app.routes.ha_routes import get_entity_notes, set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        container = _mock_container(ha_service=MagicMock())
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="备注1"), container=container)
        # 空串删除
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note=""), container=container)

        result = await get_entity_notes()
        assert "switch.gate" not in result.data["notes"]

    @pytest.mark.asyncio
    async def test_put_invalidates_ha_cache(self, _db):
        """写入后调 invalidate_states_cache，让后台 _refresh_ha_catalog 下周期重读。"""
        from app.routes.ha_routes import set_entity_note
        from app.schema.api_schemas import EntityNoteRequest

        mock_ha_service = MagicMock()
        container = _mock_container(ha_service=mock_ha_service)
        await set_entity_note(EntityNoteRequest(entity_id="switch.gate", note="x"), container=container)
        mock_ha_service.invalidate_states_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_put_missing_entity_id_rejected(self, _db):
        from app.routes.ha_routes import set_entity_note
        from app.schema.api_schemas import EntityNoteRequest
        from app.core.exceptions import AppException

        container = _mock_container(ha_service=MagicMock())
        with pytest.raises(AppException):
            await set_entity_note(EntityNoteRequest(entity_id="", note="x"), container=container)
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_ha_routes.py::TestEntityNotesRoute -v`
Expected: FAIL —— `ImportError: cannot import name 'get_entity_notes'`（路由尚未创建）。

- [ ] **Step 3: 实现路由**

修改 `app/routes/ha_routes.py`。先补 import（L14 那行把 `EntityNoteRequest` 加进去）：

当前：
```python
from ..schema.api_schemas import HAConfigRequest, HAServiceCallRequest, ModelTestRequest, UniqueSettingsRequest, EntityAliasRequest
```
替换为：
```python
from ..schema.api_schemas import HAConfigRequest, HAServiceCallRequest, ModelTestRequest, UniqueSettingsRequest, EntityAliasRequest, EntityNoteRequest
```

然后在 `set_entity_alias` 函数之后（L100 `return ApiResponse(...)` 之后、`@router.get("/ha/services")` 之前）插入两个路由：

```python


@router.get("/ha/entity-notes")
async def get_entity_notes() -> ApiResponse[dict]:
    """获取全部实体备注映射 {entity_id: note}（用户自定义，注入 LLM 认知）。"""
    from ..core.database import Database
    db = Database.get()
    notes = await db.prefs_get_by_scope("entity_note")
    return ApiResponse(data={"notes": notes})


@router.put("/ha/entity-notes")
async def set_entity_note(
    payload: EntityNoteRequest, container: AppContainer = Depends(get_container)
) -> ApiResponse[dict]:
    """设置/更新一个实体备注。空串 note 表示删除备注。

    备注只写 Aether DB（不同步 HA——HA 无此概念），用于注入 LLM 认知：
    让 AI 看到设备怪癖（如继电器 ON=关门），据此正确调用 service。
    写入后清 HA 状态缓存，让后台 _refresh_ha_catalog 下个周期重读备注。
    """
    from ..core.database import Database
    entity_id = payload.entity_id
    note = payload.note
    if not entity_id:
        raise AppException("缺少 entity_id", code="missing_params", http_status=400)

    db = Database.get()
    if note:
        await db.emoji_pref_upsert("entity_note", entity_id, note)
    else:
        await db.emoji_pref_delete("entity_note", entity_id)
    # 清缓存让后台刷新周期重读备注（与 set_entity_alias 同模式）
    container.ha_service.invalidate_states_cache()
    return ApiResponse(data={"entity_id": entity_id, "note": note})
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_ha_routes.py::TestEntityNotesRoute -v`
Expected: PASS（5 个测试）。

- [ ] **Step 5: Commit**

```bash
git add app/routes/ha_routes.py tests/test_ha_routes.py
git commit -m "feat(ha_routes): 新增 GET/PUT /ha/entity-notes 备注路由"
```

---

## Task 6: 新增 `get_device_manual` 工具

**目标**：新增 MCP 工具，让 LLM 按需拉单台/多台设备的完整可控项明细 + 备注。阶段一作为补充手段（LLM 可主动调用看详情+备注）。

**Files:**
- Modify: `app/tools.py`（`_register_ha_get_entities` 之后新增 `_register_ha_get_device_manual`；`register_all_tools` 注册）
- Test: 新建 `tests/test_tools_get_device_manual.py`

**Interfaces:**
- Consumes: `deps.ha_service.get_all_devices/get_service_defs`、`resolve_controls`、`controls_to_text(entity, controls, note)`、`Database.get().prefs_get_by_scope("entity_note")`
- Produces: MCP 工具 `get_device_manual(entity_ids: str)`，返回 `{"manuals": str, "found": [entity_id], "missing": [entity_id]}`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_tools_get_device_manual.py`：

```python
"""Tests for get_device_manual tool (Task 6)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
async def _db(tmp_path):
    """临时 DB，让 prefs_get_by_scope 真跑通。"""
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    Database._write_lock = None
    with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
        await Database.init()
        yield


@pytest.mark.asyncio
async def test_get_device_manual_single():
    from app.tools import register_all_tools, ToolDeps
    from app.mcp.mcp_client_manager import MCPClientManager

    mgr = MCPClientManager()
    mock_ha_service = MagicMock()
    fake_dev = {
        "entity_id": "switch.gate", "state": "off", "domain": "switch",
        "attributes": {"friendly_name": "大门"},
    }
    mock_ha_service.get_all_devices = AsyncMock(return_value=[fake_dev])
    mock_ha_service.get_service_defs = AsyncMock(return_value={
        "switch": {"turn_on": {"fields": ["entity_id"]}, "turn_off": {"fields": ["entity_id"]}},
    })
    mock_ha_client = MagicMock()

    # 先写一条备注
    from app.core.database import Database
    await Database.get().emoji_pref_upsert("entity_note", "switch.gate", "ON=关门, OFF=开门")

    deps = ToolDeps(
        mcp_client_manager=mgr, camera_stream=MagicMock(), vision_client=MagicMock(),
        ha_service=mock_ha_service, ha_client_ref=[mock_ha_client],
    )
    register_all_tools(deps)

    tool = mgr.get_tool("ha_devices___get_device_manual")
    assert tool is not None
    result = await tool.handler({"entity_ids": "switch.gate"}, session=MagicMock())

    assert "ON=关门, OFF=开门" in result["manuals"]
    assert "备注" in result["manuals"]
    assert "switch.gate" in result["found"]
    assert result["missing"] == []


@pytest.mark.asyncio
async def test_get_device_manual_batch():
    """逗号分隔多 entity_id 一次返回。"""
    from app.tools import register_all_tools, ToolDeps
    from app.mcp.mcp_client_manager import MCPClientManager

    mgr = MCPClientManager()
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices = AsyncMock(return_value=[
        {"entity_id": "switch.gate", "state": "off", "domain": "switch", "attributes": {"friendly_name": "大门"}},
        {"entity_id": "light.lamp", "state": "on", "domain": "light", "attributes": {"friendly_name": "灯"}},
    ])
    mock_ha_service.get_service_defs = AsyncMock(return_value={
        "switch": {"turn_on": {"fields": ["entity_id"]}},
        "light": {"turn_on": {"fields": ["entity_id"]}},
    })
    deps = ToolDeps(
        mcp_client_manager=mgr, camera_stream=MagicMock(), vision_client=MagicMock(),
        ha_service=mock_ha_service, ha_client_ref=[MagicMock()],
    )
    register_all_tools(deps)

    tool = mgr.get_tool("ha_devices___get_device_manual")
    result = await tool.handler({"entity_ids": "switch.gate, light.lamp"}, session=MagicMock())

    assert "switch.gate" in result["manuals"]
    assert "light.lamp" in result["manuals"]
    assert set(result["found"]) == {"switch.gate", "light.lamp"}


@pytest.mark.asyncio
async def test_get_device_manual_missing_reported():
    """不存在的 entity_id 进 missing 列表，不报错。"""
    from app.tools import register_all_tools, ToolDeps
    from app.mcp.mcp_client_manager import MCPClientManager

    mgr = MCPClientManager()
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices = AsyncMock(return_value=[
        {"entity_id": "switch.gate", "state": "off", "domain": "switch", "attributes": {}},
    ])
    mock_ha_service.get_service_defs = AsyncMock(return_value={"switch": {"turn_on": {"fields": ["entity_id"]}}})
    deps = ToolDeps(
        mcp_client_manager=mgr, camera_stream=MagicMock(), vision_client=MagicMock(),
        ha_service=mock_ha_service, ha_client_ref=[MagicMock()],
    )
    register_all_tools(deps)

    tool = mgr.get_tool("ha_devices___get_device_manual")
    result = await tool.handler({"entity_ids": "switch.gate, light.ghost"}, session=MagicMock())

    assert "switch.gate" in result["found"]
    assert "light.ghost" in result["missing"]
```

> **注**：`MCPClientManager.get_tool(name)` 的 key 是 `"{client_id}___{tool_name}"`（见 `mcp_client_manager.py:32`），所以取 `ha_devices` 客户端的工具用 `mgr.get_tool("ha_devices___get_device_manual")`。

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_tools_get_device_manual.py -v`
Expected: FAIL —— `tool is None`（工具未注册）或 get_tool 报错。

- [ ] **Step 3: 实现 `_register_ha_get_device_manual`**

修改 `app/tools.py`。先补 import（L20 `from .services.entity_controls import resolve_controls` 那行加 `controls_to_text`）：

当前：
```python
from .services.entity_controls import resolve_controls
```
替换为：
```python
from .services.entity_controls import resolve_controls, controls_to_text
```

然后在 `register_all_tools`（L47）里注册新工具，当前：
```python
    # 3. HA 设备查询
    _register_ha_get_entities(deps)
    # 4. HA 服务调用
    _register_ha_call_service(deps)
```
替换为：
```python
    # 3. HA 设备查询
    _register_ha_get_entities(deps)
    # 3b. 设备说明书（按需拉单台详情+备注）
    _register_ha_get_device_manual(deps)
    # 4. HA 服务调用
    _register_ha_call_service(deps)
```

然后在 `_register_ha_get_entities` 函数之后、`_register_ha_call_service` 之前（L170 之后）插入新注册函数：

```python


def _register_ha_get_device_manual(deps: ToolDeps) -> None:
    async def handler(parameters: dict, session) -> dict:
        # 按需拉单台/多台设备的完整可控项明细 + 用户备注。
        # 阶段一：作为补充手段，LLM 控制不熟悉或有怪癖的设备前可主动调用看详情。
        try:
            raw = str(parameters.get("entity_ids", "") or "").strip()
            if not raw:
                return {"manuals": "", "found": [], "missing": [], "error": "entity_ids 不能为空"}
            eid_list = [e.strip() for e in raw.split(",") if e.strip()]
            devices = await deps.ha_service.get_all_devices()
            raw_svc_defs = await deps.ha_service.get_service_defs(
                deps.ha_client_ref[0], domains=set(d.get("domain", "") for d in devices)
            )
            # 备注按 entity_id 查（一次读全部，O(1) 查 dict）
            notes_map: dict[str, str] = {}
            try:
                from .core.database import Database  # app/tools.py 在 app/ 下，用单点
                notes_map = await Database.get().prefs_get_by_scope("entity_note")
            except Exception:  # noqa: BLE001
                logger.warning("get_device_manual: 备注读取失败", exc_info=True)

            dev_by_eid = {d["entity_id"]: d for d in devices}
            found: list[str] = []
            missing: list[str] = []
            blocks: list[str] = []
            for eid in eid_list:
                dev = dev_by_eid.get(eid)
                if not dev:
                    missing.append(eid)
                    continue
                found.append(eid)
                controls = resolve_controls(dev, raw_svc_defs)
                blocks.append(
                    controls_to_text(dev, controls, note=notes_map.get(eid))
                )
            return {
                "manuals": "\n\n".join(blocks) if blocks else "(无匹配设备)",
                "found": found,
                "missing": missing,
            }
        except Exception as e:
            logger.exception("get_device_manual failed")
            return {"manuals": "", "found": [], "missing": [], "error": str(e)}

    deps.mcp_client_manager.register_tool(MCPTool(
        client_id="ha_devices",
        tool_name="get_device_manual",
        description=(
            "查询单台或多台设备的详细操作手册（含 domain/service/param 明细和用户自定义备注）。"
            "控制不熟悉的设备、或设备有特殊语义（如继电器 ON=关门、需调 turn_off）时调用本工具。"
            "支持传一个或多个 entity_id（逗号分隔）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "entity_ids": {
                    "type": "string",
                    "description": "一个或多个 entity_id，逗号分隔",
                },
            },
            "required": ["entity_ids"],
        },
        handler=handler,
    ))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_tools_get_device_manual.py -v`
Expected: PASS（3 个测试）。

> 若 `mgr.get_tool(...)` 返回 None，检查 key 是否拼对（`ha_devices___get_device_manual`，三下划线）。

- [ ] **Step 5: 让 LLM 知道这个工具存在（GUIDELINES 提示）**

修改 `app/services/prompt_service.py` 的 `GUIDELINES`（L101 起）。在「## 工具」段第一条 `- 动作前先 get_entities ...` 那条之后加一句关于 get_device_manual 的提示。

当前（L109-110 附近）：
```python
    "## 工具\n"
    "- 动作前先 get_entities 看真实设备与可控项，domain/service/param/entity_id 都取自返回，不要自己拼造。\n"
```
替换为：
```python
    "## 工具\n"
    "- 动作前先 get_entities 看真实设备与可控项，domain/service/param/entity_id 都取自返回，不要自己拼造。\n"
    "- 设备有用户备注（特殊语义/怪癖，如继电器 ON 实为关门）时，备注已在设备列表里；需要单台详情或复核时调 get_device_manual。\n"
```

- [ ] **Step 6: Commit**

```bash
git add app/tools.py app/services/prompt_service.py tests/test_tools_get_device_manual.py
git commit -m "feat(tools): 新增 get_device_manual 工具按需拉设备详情+备注"
```

---

## Task 7: `get_entities` 工具也接入备注

**目标**：`get_entities` 工具返回的 `entities` 里 `_controls` 是 dict 形态（给 LLM 结构化用），但 LLM 调用它时也需要看到备注。最小改动：在 `get_entities` 返回里给每个 device/entity 附一个 `note` 字段。

**Files:**
- Modify: `app/tools.py:107-170`（`_register_ha_get_entities` handler）
- Test: 扩展 `tests/test_tools_get_device_manual.py` 或新建轻量断言

**Interfaces:**
- Consumes: `Database.get().prefs_get_by_scope("entity_note")`
- Produces: `get_entities` 返回的 `entities` 每项新增 `note: str`（无备注则 `""`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_tools_get_device_manual.py` 末尾新增：

```python


@pytest.mark.asyncio
async def test_get_entities_includes_note_field():
    """get_entities 返回的 entities 每项带 note 字段（Task 7）。"""
    from app.tools import register_all_tools, ToolDeps
    from app.mcp.mcp_client_manager import MCPClientManager

    mgr = MCPClientManager()
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices = AsyncMock(return_value=[
        {"entity_id": "switch.gate", "state": "off", "domain": "switch",
         "attributes": {"friendly_name": "大门"}},
    ])
    mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": [
        {"name": "大门", "model": None, "area_name": None,
         "entities": [{"entity_id": "switch.gate", "domain": "switch"}]},
    ]})
    mock_ha_service.get_service_defs = AsyncMock(return_value={
        "switch": {"turn_on": {"fields": ["entity_id"]}},
    })

    from app.core.database import Database
    await Database.get().emoji_pref_upsert("entity_note", "switch.gate", "ON=关门")

    deps = ToolDeps(
        mcp_client_manager=mgr, camera_stream=MagicMock(), vision_client=MagicMock(),
        ha_service=mock_ha_service, ha_client_ref=[MagicMock()],
    )
    register_all_tools(deps)

    tool = mgr.get_tool("ha_devices___get_entities")
    result = await tool.handler({}, session=MagicMock())

    gate = next(e for e in result["entities"] if e["entity_id"] == "switch.gate")
    assert gate.get("note") == "ON=关门"
```

- [ ] **Step 2: 运行测试验证失败**

Run: `python -m pytest tests/test_tools_get_device_manual.py::test_get_entities_includes_note_field -v`
Expected: FAIL —— `gate.get("note")` 为 None（字段未加）。

- [ ] **Step 3: 修改 `get_entities` handler**

在 `app/tools.py` 的 `_register_ha_get_entities` handler 内（L108-150），`for device in devices:` 循环之前读备注，循环内给每个 device 附 `note`。

定位当前（L108-122 附近）：
```python
    async def handler(_: dict, session) -> dict:
        try:
            devices = await deps.ha_service.get_all_devices()
            grouped = await deps.ha_service.get_all_devices_grouped()
            raw_svc_defs = await deps.ha_service.get_service_defs(
                deps.ha_client_ref[0], domains=set(d.get("domain", "") for d in devices)
            )
            services_info = {
                domain: {svc_name: svc_def["fields"] for svc_name, svc_def in svcs.items()}
                for domain, svcs in raw_svc_defs.items()
            }
            for device in devices:
                device["_controls"] = resolve_controls(device, raw_svc_defs)
```
替换为（在 `services_info = ...` 之后、循环之前插入备注读取；循环内附 `note`）：
```python
    async def handler(_: dict, session) -> dict:
        try:
            devices = await deps.ha_service.get_all_devices()
            grouped = await deps.ha_service.get_all_devices_grouped()
            raw_svc_defs = await deps.ha_service.get_service_defs(
                deps.ha_client_ref[0], domains=set(d.get("domain", "") for d in devices)
            )
            services_info = {
                domain: {svc_name: svc_def["fields"] for svc_name, svc_def in svcs.items()}
                for domain, svcs in raw_svc_defs.items()
            }
            # 用户备注：让 LLM 在 get_entities 返回里也能看到设备怪癖
            notes_map: dict[str, str] = {}
            try:
                from .core.database import Database  # app/tools.py 在 app/ 下，用单点
                notes_map = await Database.get().prefs_get_by_scope("entity_note")
            except Exception:  # noqa: BLE001
                logger.warning("get_entities: 备注读取失败", exc_info=True)
            for device in devices:
                device["_controls"] = resolve_controls(device, raw_svc_defs)
                device["note"] = notes_map.get(device["entity_id"], "")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `python -m pytest tests/test_tools_get_device_manual.py -v`
Expected: PASS（含 Task 6 的 3 个 + Task 7 的 1 个）。

- [ ] **Step 5: Commit**

```bash
git add app/tools.py tests/test_tools_get_device_manual.py
git commit -m "feat(tools): get_entities 返回附 note 字段"
```

---

## Task 8: 前端备注 textarea（HAListView）

**目标**：在设备详情页（已有别名编辑的地方）加备注 textarea，加载全部备注、编辑后 PUT 保存。前端无单测要求（项目前端无测试基础设施），手工验证。

**Files:**
- Modify: `frontend/src/views/HAListView.vue`
  - `<script setup>`：加 `entityNotes` ref + `loadEntityNotes` / `saveEntityNote` 函数 + `onMounted` 调用
  - `<template>`：在 `name-row` 之后加备注 textarea 行
  - `<style>`：加 `.note-row` 样式

**Interfaces:**
- Consumes: `GET /api/ha/entity-notes` → `{data: {notes: {entity_id: note}}}`；`PUT /api/ha/entity-notes` body `{entity_id, note}`
- Produces: 设备详情页备注输入交互

- [ ] **Step 1: `<script setup>` 加状态和函数**

修改 `frontend/src/views/HAListView.vue`。在别名状态声明之后（L27 `const nameInput = ref('')` 之后）加备注状态：

```javascript

// 实体备注（用户自定义，注入 AI 认知，影响调用决策——如继电器反转语义）
const entityNotes = ref({})          // {entity_id: note}
const noteInput = ref('')
const editingNote = ref(false)
```

然后在 `resetName` 函数之后（L115 之后、`function refreshDeviceEntityName` 之前）加备注函数：

```javascript

// ========================
//  Entity note (用户自定义备注，注入 AI 认知)
// ========================

async function loadEntityNotes() {
  try {
    const res = await fetch('/api/ha/entity-notes', { credentials: 'include' })
    const json = await res.json()
    entityNotes.value = json.data?.notes || {}
  } catch (e) {
    console.error('Failed to load entity notes:', e)
  }
}

function startEditNote() {
  if (!selectedEntity.value) return
  noteInput.value = entityNotes.value[selectedEntity.value.entity_id] || ''
  editingNote.value = true
}

async function saveNote() {
  if (!selectedEntity.value) return
  const eid = selectedEntity.value.entity_id
  const note = noteInput.value.trim()
  try {
    await fetch('/api/ha/entity-notes', {
      method: 'PUT',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: eid, note }),
    })
    if (note) {
      entityNotes.value[eid] = note
    } else {
      delete entityNotes.value[eid]
    }
  } catch (e) {
    console.error('Failed to save entity note:', e)
  }
  editingNote.value = false
}

function resetNote() {
  if (!selectedEntity.value) return
  noteInput.value = entityNotes.value[selectedEntity.value.entity_id] || ''
}
```

最后在 `onMounted` 里（L734 附近 `loadEntities()` 那行）加 `loadEntityNotes()` 调用：

当前（参考 L730 附近）：
```javascript
  loadEntities()
```
替换为：
```javascript
  loadEntities()
  loadEntityNotes()
```

> **注**：执行前 `grep -n "onMounted\|loadEntities()" frontend/src/views/HAListView.vue` 核对 onMounted 的真实结构和位置；若 `loadEntities()` 不是直接在 onMounted 顶层调用，按实际位置加 `loadEntityNotes()`。

- [ ] **Step 2: `<template>` 加备注行**

在设备详情的 `name-row`（L859-871 的 `info-row name-row` div）**之后**、状态行（L872 `<div class="info-row">` 状态）之前，插入备注行：

```html
                  <div class="info-row note-row">
                    <span class="info-label">备注</span>
                    <span v-if="!editingNote" class="info-value note-display" @click="startEditNote" title="点击给设备写备注（影响 AI 调用，如继电器反转语义）">
                      <span v-if="entityNotes[selectedEntity.entity_id]" class="note-text">{{ entityNotes[selectedEntity.entity_id] }}</span>
                      <span v-else class="note-empty">点此添加（让 AI 理解设备怪癖）</span>
                      <span class="edit-hint">✎</span>
                    </span>
                    <span v-else class="note-edit">
                      <textarea v-model="noteInput" class="note-input" rows="3" maxlength="200" placeholder="如：继电器 ON=关门，OFF=开门；用户说开门时调 turn_off" @keyup.esc="editingNote = false"></textarea>
                      <div class="note-btns">
                        <button class="name-btn name-btn--save" @click="saveNote">保存</button>
                        <button class="name-btn" @click="resetNote">还原</button>
                      </div>
                    </span>
                  </div>
```

- [ ] **Step 3: `<style>` 加备注样式**

在文件末尾的 `/* 实体别名编辑 */` 段（L1411 附近）之后追加：

```css
/* 实体备注编辑 */
.note-row {
  flex-direction: column;
  align-items: stretch;
  gap: 6px;
}
.note-display {
  cursor: pointer;
  padding: 6px 8px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
  min-height: 24px;
  white-space: pre-wrap;
  word-break: break-all;
}
.note-display:hover {
  background: rgba(255, 255, 255, 0.06);
}
.note-text {
  display: inline;
}
.note-empty {
  color: var(--text-muted, #888);
  font-size: 0.9em;
}
.note-input {
  width: 100%;
  background: var(--bg-input, rgba(0, 0, 0, 0.2));
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.1));
  border-radius: 6px;
  color: inherit;
  padding: 6px 8px;
  font-size: 0.92em;
  resize: vertical;
  font-family: inherit;
}
.note-btns {
  display: flex;
  gap: 6px;
  margin-top: 4px;
}
```

- [ ] **Step 4: 手工验证**

启动前后端，在设备列表打开一个设备详情，确认：
1. 「备注」行显示「点此添加（让 AI 理解设备怪癖）」
2. 点击 → 出现 textarea，输入"继电器 ON=关门, OFF=开门"，保存
3. 备注显示出来
4. 刷新页面，备注仍在（持久化）
5. 清空保存 → 删除，回到「点此添加」

- [ ] **Step 5: Commit**

```bash
git add frontend/src/views/HAListView.vue
git commit -m "feat(frontend): HAListView 设备详情加备注 textarea"
```

---

## Task 9: 端到端验收（手工 + 回归）

**目标**：跑全量后端测试确认零回归；手工跑继电器反转场景验证阶段一验收标准。

**Files:** 无代码改动（验收任务）。

- [ ] **Step 1: 全量后端回归**

Run: `python -m pytest tests/test_entity_controls.py tests/test_rule_service.py tests/test_ha_routes.py tests/test_tools_get_device_manual.py tests/test_main_catalog_notes.py tests/test_prompt_service.py -v`
Expected: PASS（所有测试，含原有 + 新增）。

- [ ] **Step 2: 全量回归（兜底）**

Run: `python -m pytest -x -q`
Expected: PASS（若有无关本任务的既有失败，记录但不算回归）。

- [ ] **Step 3: 手工验收继电器反转场景**

对照 spec §5.6 验收清单逐条确认：
1. 前端给 `switch.gate_relay` 加备注"这台是继电器，ON=关门, OFF=开门。用户说开门时调用 turn_off"
2. 主聊天说"开门" → 观察 LLM 调用 `call_service(domain=switch, service=turn_off)`（看后端日志 tool_call）
3. 主聊天说"关门" → 调 `turn_on`
4. 建自动化规则"如果检测到有人就开门" → 规则 JSON 的 actions 用 `turn_off`
5. 60 秒后或立即在 system prompt 里能看到备注行（看 `build_system_prompt` 输出或日志）
6. 备注为空的设备不显示备注行（零回归）
7. 重启服务，备注仍在

- [ ] **Step 4: Commit（如有验收过程的小修）**

```bash
git add -A
git commit -m "test: 阶段一端到端验收通过" --allow-empty
```

---

## 验收对齐（spec §5.6 覆盖检查）

| spec 验收项 | 对应任务 |
|------------|---------|
| 前端给设备加备注 | Task 8 |
| 主聊天"开门"调 turn_off | Task 2（备注入 prompt）+ Task 9 手工验 |
| 主聊天"关门"调 turn_on | Task 2 + Task 9 手工验 |
| 规则生成用 turn_off | Task 3 + Task 9 手工验 |
| get_device_manual 工具可调 | Task 6 |
| 空备注不影响现有行为 | Task 1（`test_note_omitted_when_none_or_empty`）+ Task 2（`test_refresh_catalog_no_notes_no_change`） |
| 备注持久化 | Task 5（emoji_pref_upsert 落 SQLite）+ Task 8 手工验重启 |
