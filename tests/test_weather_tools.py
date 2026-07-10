"""Tests for weather tools (now using QWeather cache)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestGetWeatherHandler:
    @pytest.mark.asyncio
    async def test_default_location_delegates_to_service(self):
        """测试无参数时委托给 weather_service.get_weather(None)。"""
        from app.mcp.weather_tools import get_weather_handler

        mock_weather = {"location": "上海市宝山区", "temperature": "25", "weather": "晴天"}

        with patch("app.services.weather_service.get_weather", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_weather

            result = await get_weather_handler({}, None)
            mock_get.assert_called_once_with(None)
            assert result["temperature"] == "25"

    @pytest.mark.asyncio
    async def test_custom_location(self):
        """测试指定位置。"""
        from app.mcp.weather_tools import get_weather_handler

        mock_weather = {"location": "北京", "temperature": "20", "weather": "多云"}

        with patch("app.services.weather_service.get_weather", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_weather
            result = await get_weather_handler({"location": "北京"}, None)
            assert result["location"] == "北京"

    @pytest.mark.asyncio
    async def test_empty_location_delegates_none_to_service(self):
        """测试空位置时传 None 给 weather_service，由其返回错误。"""
        from app.mcp.weather_tools import get_weather_handler

        mock_error = {"error": "请先在设置中配置家庭地址（省市区）", "location": ""}

        with patch("app.services.weather_service.get_weather", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_error

            result = await get_weather_handler({"location": ""}, None)
            mock_get.assert_called_once_with(None)
            assert "error" in result

    @pytest.mark.asyncio
    async def test_error_propagation(self):
        """测试错误传播。"""
        from app.mcp.weather_tools import get_weather_handler

        mock_weather = {"error": "获取天气失败: timeout", "location": "上海"}

        with patch("app.services.weather_service.get_weather", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_weather
            result = await get_weather_handler({"location": "上海"}, None)
            assert "error" in result
