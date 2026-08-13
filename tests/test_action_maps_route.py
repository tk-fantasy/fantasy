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
