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
