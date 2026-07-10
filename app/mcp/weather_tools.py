"""天气查询工具 — 使用和风天气 API（带 SQLite 缓存）。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def get_weather_handler(parameters: dict, session) -> dict:
    """获取指定城市的天气信息。使用和风天气 API，15 分钟缓存。

    参数:
        location: 城市名称或经纬度（如"上海"或"121.47,31.23"），默认使用设置中的地点
    """
    from ..services.weather_service import get_weather

    location = str(parameters.get("location", "")).strip() or None
    return await get_weather(location)
