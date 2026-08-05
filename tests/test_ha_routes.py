"""Tests for ha_routes.py - HA 设备控制。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_container(**overrides):
    """构造一个 mock AppContainer，按需覆盖字段。"""
    c = MagicMock()
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class TestHAEntitiesRoute:
    """测试 /api/ha/entities 路由。"""

    @pytest.mark.asyncio
    async def test_ha_entities(self):
        """获取实体列表。"""
        from app.routes.ha_routes import ha_entities
        from app.services.ha_service import HAService

        mock_ha_service = MagicMock()
        mock_ha_service.get_all_devices = AsyncMock(return_value=[
            {"entity_id": "light.test", "state": "on", "domain": "light"},
        ])
        mock_ha_service.get_all_devices_grouped = AsyncMock(return_value={
            "devices": [
                {"entities": [{"entity_id": "light.test", "domain": "light"}]},
            ],
        })
        mock_ha_service.get_service_defs = AsyncMock(return_value=[])
        container = _mock_container(ha_service=mock_ha_service)

        with patch("app.services.entity_controls.resolve_controls") as mock_controls:
            mock_controls.return_value = {}
            # get_service_defs 内部调 container.ha_client.get_services
            container.ha_client.get_services = AsyncMock(return_value=[])
            result = await ha_entities(container=container)
            assert result.code == "ok"


class TestHAServicesRoute:
    """测试 /api/ha/services 路由。"""

    @pytest.mark.asyncio
    async def test_ha_services(self):
        """获取服务列表。"""
        from app.routes.ha_routes import ha_services
        from app.services.ha_service import HAService

        mock_ha_service = MagicMock(wraps=HAService)
        container = _mock_container(ha_service=mock_ha_service)
        container.ha_client.get_services = AsyncMock(return_value=[
            {"domain": "light", "services": {
                "turn_on": {"fields": {"entity_id": {"required": False}}},
                "turn_off": {"fields": {"entity_id": {"required": False}}},
            }},
        ])
        result = await ha_services(container=container)
        assert result.code == "ok"
        assert "light" in result.data


class TestHACallServiceRoute:
    """测试 /api/ha/call_service 路由。"""

    @pytest.mark.asyncio
    async def test_ha_call_service(self):
        """调用服务成功。"""
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container()
        container.ha_client.call_service = AsyncMock(return_value={"result": "ok"})

        payload = HAServiceCallRequest(
            domain="light",
            service="turn_on",
            entity_id="light.test",
            data={}
        )
        result = await ha_call_service(payload, container=container)
        assert result.code == "ok"

    @pytest.mark.asyncio
    async def test_ha_call_service_invalidates_cache(self):
        """调用服务后应清掉 HAService 状态缓存，确保前端重拉拿到最新状态。"""
        from app.routes.ha_routes import ha_call_service
        from app.schema.api_schemas import HAServiceCallRequest

        container = _mock_container()
        container.ha_client.call_service = AsyncMock(return_value={"result": "ok"})
        container.ha_service.invalidate_states_cache = MagicMock()

        payload = HAServiceCallRequest(
            domain="light",
            service="turn_on",
            entity_id="light.test",
            data={}
        )
        await ha_call_service(payload, container=container)
        container.ha_service.invalidate_states_cache.assert_called_once()


class TestHAConfigRoute:
    """测试 /api/ha/config 路由。"""

    @pytest.mark.asyncio
    async def test_get_ha_config(self):
        """获取 HA 配置。"""
        from app.routes.ha_routes import get_ha_config

        with patch("app.routes.ha_routes.get_config") as mock_get_config:
            mock_get_config.return_value = {
                "url": "http://localhost:8123",
                "token": "test-token-12345678"
            }
            result = await get_ha_config()
            assert result.code == "ok"
            assert result.data["url"] == "http://localhost:8123"


class TestHAHistoryRoute:
    """测试 /api/ha/history 路由。"""

    @pytest.mark.asyncio
    async def test_ha_history_returns_data(self):
        """查询历史成功，返回 history 数组。"""
        from app.routes.ha_routes import ha_history

        container = _mock_container()
        container.ha_client.get_history = AsyncMock(return_value=[
            [{"entity_id": "sensor.temp", "state": "26.5", "last_updated": "2026-07-13T00:00:00+00:00"}],
        ])
        result = await ha_history(
            filter_entity_id="sensor.temp", hours=24, container=container,
        )
        assert result.code == "ok"
        assert result.data["count"] == 1
        assert len(result.data["history"]) == 1
        # 验证传给 client 的参数含 timestamp/end_time
        call_kwargs = container.ha_client.get_history.call_args.kwargs
        assert call_kwargs["filter_entity_id"] == "sensor.temp"
        assert call_kwargs["timestamp"]  # 非空 ISO8601
        assert call_kwargs["end_time"]
        assert call_kwargs["minimal"]  # truthy（直接调用路由时是 Query(True)，FastAPI 运行时会解包为 True）

    @pytest.mark.asyncio
    async def test_ha_history_empty_result(self):
        """无历史数据时返回空数组而非报错。"""
        from app.routes.ha_routes import ha_history

        container = _mock_container()
        container.ha_client.get_history = AsyncMock(return_value=[])
        result = await ha_history(
            filter_entity_id="sensor.nodata", hours=6, container=container,
        )
        assert result.code == "ok"
        assert result.data["count"] == 0


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
