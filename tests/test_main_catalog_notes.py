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


@pytest.mark.asyncio
async def test_refresh_catalog_hides_disabled_and_skips_controls(tmp_path):
    """禁用实体对 AI 不可见：catalog 无该行（不再标 ⛔），controls 明细也不含。"""
    from app.core.database import Database
    Database._instance = None
    Database._db = None
    with patch("app.core.database.DB_PATH", tmp_path / "t.db"):
        await Database.init()
    await Database.get().emoji_pref_upsert("entity_operable", "lock.tong_suo", "0")

    fake_dev = {
        "entity_id": "lock.tong_suo", "state": "locked", "domain": "lock",
        "attributes": {"friendly_name": "童锁"}, "name": "童锁",
        "area_id": "a1", "area_name": "儿童房",
    }
    mock_ha_service = MagicMock()
    mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={"devices": [
        {"name": "童锁", "model": None, "manufacturer": None, "sw_version": None,
         "area_id": "a1", "area_name": "儿童房", "summary": "童锁",
         "entities": [fake_dev]},
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
    # 禁止 = 隐藏：实体行不出现，也不再输出 ⛔ 标记
    assert "lock.tong_suo" not in catalog
    assert "⛔" not in catalog
    # controls 明细不含禁用项
    assert "lock.tong_suo" not in controls
