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
