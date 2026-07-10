"""Tests for weather routes."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


class TestWeatherRoute:
    @pytest.mark.asyncio
    async def test_weather_default(self):
        """测试默认天气查询。"""
        from app.routes.weather_routes import weather

        mock_data = {"location": "上海", "temperature": "25", "weather": "晴天"}
        with patch("app.routes.weather_routes.get_weather", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_data
            result = await weather(location=None)
            assert result.code == "ok"
            assert result.data["location"] == "上海"
            mock_get.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_weather_with_location(self):
        """测试指定位置天气查询。"""
        from app.routes.weather_routes import weather

        mock_data = {"location": "北京", "temperature": "20"}
        with patch("app.routes.weather_routes.get_weather", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_data
            result = await weather(location="北京")
            assert result.data["location"] == "北京"
            mock_get.assert_called_once_with("北京")


class TestWeatherLocateRoute:
    @pytest.mark.asyncio
    async def test_locate(self):
        """测试 IP 定位路由。"""
        from app.routes.weather_routes import weather_locate

        mock_data = {"name": "宝山", "adm1": "上海", "lat": "31.40", "lon": "121.49"}
        with patch("app.routes.weather_routes.ip_locate", new_callable=AsyncMock) as mock_locate:
            mock_locate.return_value = mock_data
            result = await weather_locate()
            assert result.code == "ok"
            assert result.data["name"] == "宝山"
