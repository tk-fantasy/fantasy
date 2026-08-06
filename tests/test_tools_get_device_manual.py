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
        mcp_client_manager=mgr, vision_client=MagicMock(),
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
        mcp_client_manager=mgr, vision_client=MagicMock(),
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
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=mock_ha_service, ha_client_ref=[MagicMock()],
    )
    register_all_tools(deps)

    tool = mgr.get_tool("ha_devices___get_device_manual")
    result = await tool.handler({"entity_ids": "switch.gate, light.ghost"}, session=MagicMock())

    assert "switch.gate" in result["found"]
    assert "light.ghost" in result["missing"]


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
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=mock_ha_service, ha_client_ref=[MagicMock()],
    )
    register_all_tools(deps)

    tool = mgr.get_tool("ha_devices___get_entities")
    result = await tool.handler({}, session=MagicMock())

    gate = next(e for e in result["entities"] if e["entity_id"] == "switch.gate")
    assert gate.get("note") == "ON=关门"
