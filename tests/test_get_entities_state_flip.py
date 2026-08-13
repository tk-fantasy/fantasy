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
