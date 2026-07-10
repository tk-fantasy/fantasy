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

        mock_ha_service = MagicMock(wraps=HAService)
        mock_ha_service.get_all_devices = AsyncMock(return_value=[
            {"entity_id": "light.test", "state": "on", "domain": "light"},
        ])
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
