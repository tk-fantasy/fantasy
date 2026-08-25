"""Tests for call_service operable authorization (Task 2)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

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
    session = MagicMock()
    session.current_query = "开灯"
    with patch("app.tools.call_with_probe", new=AsyncMock(return_value={})):
        result = await tool.handler(
            {"domain": "light", "service": "turn_on", "entity_id": "light.bed"}, session
        )
    assert result["success"] is True


@pytest.mark.asyncio
async def test_get_entities_hides_disabled_entity(tmp_path, monkeypatch):
    """禁止设备对 AI 不可见：get_entities 不返回黑名单内实体（隐藏而非标记）。"""
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
        {"entity_id": "lock.tong_suo", "domain": "lock", "state": "locked",
         "name": "童锁", "attributes": {}, "area_id": "a1", "area_name": "儿童房"},
        {"entity_id": "light.bed", "domain": "light", "state": "off",
         "name": "床头灯", "attributes": {}, "area_id": "a1", "area_name": "卧室"},
    ])
    ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": [
        {"device_id": "d1", "name": "童锁", "model": None, "manufacturer": None,
         "sw_version": None, "area_id": "a1", "area_name": "儿童房", "summary": "童锁",
         "entities": [{"entity_id": "lock.tong_suo", "domain": "lock", "name": "童锁",
                       "state": "locked", "attributes": {}}]},
        {"device_id": "d2", "name": "床头灯", "model": None, "manufacturer": None,
         "sw_version": None, "area_id": "a1", "area_name": "卧室", "summary": "床头灯",
         "entities": [{"entity_id": "light.bed", "domain": "light", "name": "床头灯",
                       "state": "off", "attributes": {}}]},
    ]})
    ha_service.get_service_defs = AsyncMock(return_value={})
    deps = ToolDeps(
        mcp_client_manager=mgr, vision_client=MagicMock(),
        ha_service=ha_service, ha_client_ref=[MagicMock()],
    )
    _register_ha_get_entities(deps)
    tool = mgr.get_tool("ha_devices___get_entities")
    with patch("app.services.entity_controls.resolve_controls", return_value={}):
        result = await tool.handler({}, MagicMock())
    by_id = {e["entity_id"] for e in result["entities"]}
    # 黑名单内实体被隐藏；其余正常可见且 ai_operable 恒 True
    assert "lock.tong_suo" not in by_id
    assert "light.bed" in by_id
    assert all(e["ai_operable"] is True for e in result["entities"])
