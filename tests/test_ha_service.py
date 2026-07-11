"""Tests for HAService with mocked HomeAssistantClient."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ha_service import HAService


def _make_service(devices: list[dict] | None = None) -> tuple[HAService, MagicMock]:
    client = MagicMock()
    client.get_states = AsyncMock(return_value=devices or [])
    client._base_url = "http://localhost:8123"
    client._token = "test-token"
    return HAService(client=client), client


class TestHAService:
    @pytest.mark.asyncio
    async def test_get_all_devices(self):
        devices = [
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "Bed Light"}},
            {"entity_id": "climate.ac", "state": "cool", "attributes": {"friendly_name": "AC"}},
        ]
        svc, _ = _make_service(devices)
        # Mock area maps to return empty (no area assignments)
        svc._area_map = {}
        svc._entity_area_map = {}
        svc._area_cache_at = 9999999999  # Far future to avoid refresh
        result = await svc.get_all_devices()
        # Devices without area_id are filtered out
        assert len(result) == 0

    @pytest.mark.asyncio
    async def test_get_all_devices_with_area(self):
        devices = [
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "Bed Light"}},
        ]
        svc, _ = _make_service(devices)
        # Mock area maps
        svc._area_map = {"bedroom": "Bedroom"}
        svc._entity_area_map = {"light.bed": "bedroom"}
        svc._area_cache_at = 9999999999
        result = await svc.get_all_devices()
        assert len(result) == 1
        assert result[0]["entity_id"] == "light.bed"
        assert result[0]["area_name"] == "Bedroom"

    @pytest.mark.asyncio
    async def test_empty_devices(self):
        svc, _ = _make_service([])
        svc._area_map = {}
        svc._entity_area_map = {}
        svc._area_cache_at = 9999999999
        result = await svc.get_all_devices()
        assert result == []


class TestInvalidateStatesCache:
    """测试缓存失效逻辑。"""

    @pytest.mark.asyncio
    async def test_invalidate_forces_refetch(self):
        """invalidate_states_cache 后，下次 _get_states_cached 重新拉取。"""
        svc, client = _make_service([
            {"entity_id": "light.bed", "state": "on", "attributes": {}},
        ])
        # 第一次拉取 → client.get_states 被调用一次
        await svc._get_states_cached()
        assert client.get_states.call_count == 1

        # 在 TTL 内再拉 → 命中缓存，不重新请求
        await svc._get_states_cached()
        assert client.get_states.call_count == 1

        # 失效缓存 → 再次拉取会重新请求 HA
        svc.invalidate_states_cache()
        await svc._get_states_cached()
        assert client.get_states.call_count == 2
