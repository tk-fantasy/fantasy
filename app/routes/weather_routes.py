"""天气路由 — 代理和风天气 API。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section
from ..schema.api_schemas import WeatherConfigRequest
from ..services.weather_service import get_weather, ip_locate, city_lookup, get_weather_indices

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/weather")
async def weather(
    location: str = Query(default=None, description="位置，格式：经度,纬度 或城市名"),
) -> ApiResponse[dict]:
    """获取天气信息（后端代理，带 15 分钟缓存）。"""
    data = await get_weather(location)
    return ApiResponse(data=data)


@router.get("/weather/locate")
async def weather_locate() -> ApiResponse[dict]:
    """IP 自动定位。"""
    data = await ip_locate()
    return ApiResponse(data=data)


@router.get("/weather/city")
async def weather_city(
    q: str = Query(..., description="城市名称"),
) -> ApiResponse[dict]:
    """城市搜索：根据名称查询 Location ID。"""
    data = await city_lookup(q)
    return ApiResponse(data=data)


@router.get("/weather/indices")
async def weather_indices(
    location: str = Query(..., description="Location ID 或 经度,纬度"),
) -> ApiResponse[dict]:
    """获取天气生活指数（运动、洗车等）。"""
    data = await get_weather_indices(location)
    return ApiResponse(data=data)


@router.get("/weather/config")
async def get_weather_config() -> ApiResponse[dict]:
    """获取天气 API 配置（隐藏 private_key）。"""
    weather_cfg = get_config("weather", {})
    return ApiResponse(data={
        "host": weather_cfg.get("host", ""),
        "kid": weather_cfg.get("kid", ""),
        "sub": weather_cfg.get("sub", ""),
        "has_private_key": bool(weather_cfg.get("private_key", "")),
    })


@router.post("/weather/config")
async def set_weather_config(
    payload: WeatherConfigRequest,
) -> ApiResponse[dict]:
    """保存天气 API 配置。

    和风天气需要 4 个参数：host, kid, sub, private_key
    注册获取：https://console.qweather.com
    """
    host = payload.host.strip()
    kid = payload.kid.strip()
    sub = payload.sub.strip()
    private_key = payload.private_key.strip()

    # 构建配置（保留原有的 private_key 如果未提供新值）
    weather_cfg = {
        "host": host,
        "kid": kid,
        "sub": sub,
    }

    if private_key:
        weather_cfg["private_key"] = private_key
    else:
        # 保留原有的 private_key
        existing = get_config("weather", {})
        if existing.get("private_key"):
            weather_cfg["private_key"] = existing["private_key"]

    update_config_section("weather", weather_cfg)
    logger.info("Weather config updated: host=%s, kid=%s", host, kid)

    return ApiResponse(data={"saved": True})
