"""Tests for weather service."""
from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetWeather:
    @pytest.mark.asyncio
    async def test_cache_hit(self):
        """测试缓存命中时直接返回。"""
        from app.services.weather_service import get_weather

        mock_db = MagicMock()
        cached_data = {
            "weather": {"location": "上海", "temperature": "25", "weather": "晴天"},
            "cached_at": time.time(),  # 刚刚缓存的
        }
        mock_db.kv_get = AsyncMock(return_value=json.dumps(cached_data))

        with patch("app.services.weather_service.Database") as MockDB:
            MockDB.get.return_value = mock_db
            result = await get_weather("上海")
            assert result["location"] == "上海"
            assert result["temperature"] == "25"
            # 不应该调用 API
            mock_db.kv_set.assert_not_called()

    @pytest.mark.asyncio
    async def test_cache_expired(self):
        """测试缓存过期时重新请求 API。"""
        from app.services.weather_service import get_weather

        mock_db = MagicMock()
        # 过期的缓存
        cached_data = {
            "weather": {"location": "上海"},
            "cached_at": time.time() - 20 * 60,  # 20 分钟前
        }
        mock_db.kv_get = AsyncMock(return_value=json.dumps(cached_data))
        mock_db.kv_set = AsyncMock()

        # Mock responses for the three API calls: geo lookup, weather now, indices
        mock_geo_data = {
            "location": [{"id": "101020100", "name": "上海", "adm1": "上海市"}]
        }
        mock_weather_data = {
            "now": {
                "temp": "28",
                "feelsLike": "30",
                "humidity": "65",
                "text": "多云",
                "windDir": "东南",
                "windScale": "3",
                "windSpeed": "15",
                "vis": "10",
                "uvIndex": "5",
                "icon": "101",
                "obsTime": "2024-01-01T12:00:00",
            }
        }
        mock_indices_data = {"daily": []}

        with patch("app.services.weather_service.Database") as MockDB, \
             patch("app.services.weather_service._qweather_request", new_callable=AsyncMock) as mock_api:
            MockDB.get.return_value = mock_db
            # Return different values for each call
            mock_api.side_effect = [mock_geo_data, mock_weather_data, mock_indices_data]

            result = await get_weather("121.47,31.23")
            assert result["temperature"] == "28"
            assert result["weather"] == "多云"
            # 应该写入缓存
            mock_db.kv_set.assert_called_once()

    @pytest.mark.asyncio
    async def test_api_error(self):
        """测试 API 请求失败时返回错误。"""
        from app.services.weather_service import get_weather

        mock_db = MagicMock()
        mock_db.kv_get = AsyncMock(return_value=None)  # 无缓存

        with patch("app.services.weather_service.Database") as MockDB, \
             patch("app.services.weather_service._qweather_request", new_callable=AsyncMock) as mock_api:
            MockDB.get.return_value = mock_db
            mock_api.side_effect = Exception("API Error")

            result = await get_weather("上海")
            assert "error" in result


class TestIpLocate:
    @pytest.mark.asyncio
    async def test_ip_locate_success(self):
        """测试 IP 定位成功。"""
        from app.services.weather_service import ip_locate

        mock_geo = {
            "location": {
                "name": "宝山",
                "adm1": "上海",
                "adm2": "上海",
                "lat": "31.40",
                "lon": "121.49",
                "id": "101020300",
            }
        }

        with patch("app.services.weather_service._qweather_request", new_callable=AsyncMock) as mock_api:
            mock_api.return_value = mock_geo
            result = await ip_locate()
            assert result["name"] == "宝山"
            assert result["adm1"] == "上海"

    @pytest.mark.asyncio
    async def test_ip_locate_error(self):
        """测试 IP 定位失败。"""
        from app.services.weather_service import ip_locate

        with patch("app.services.weather_service._qweather_request", new_callable=AsyncMock) as mock_api:
            mock_api.side_effect = Exception("Network error")
            result = await ip_locate()
            assert "error" in result
