"""天气路由 — 代理和风天气 API。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from ..core.api_models import ApiResponse
from ..core.config import get_config, update_config_section
from ..schema.api_schemas import WeatherConfigRequest
from ..services.config_probes import probe_weather
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

    保存前 probe：用户填了新 private_key 时，用候选凭证生成 JWT 并真连一次
    和风 API，失败拒绝落盘。避免错凭证存进去后查天气才发现不工作。
    """
    host = payload.host.strip()
    kid = payload.kid.strip()
    sub = payload.sub.strip()
    private_key = payload.private_key.strip()

    # 确定 probe 用的 private_key：用户填了新的用它，否则保留现有的
    if not private_key:
        existing = get_config("weather", {})
        private_key = existing.get("private_key", "")

    # 有 private_key 才 probe（没 key 时连 JWT 都生成不了，没必要 probe）
    if private_key:
        result = await probe_weather(host, kid, sub, private_key)
        if not result.ok:
            logger.warning("Weather config save rejected: %s (%s)", result.reason, result.detail)
            return ApiResponse(
                code="probe_failed",
                message=result.detail,
                data={"saved": False, "section": "weather", **result.to_dict()},
            )

    weather_cfg = {
        "host": host,
        "kid": kid,
        "sub": sub,
        "private_key": private_key,
    }

    update_config_section("weather", weather_cfg)
    logger.info("Weather config updated: host=%s, kid=%s", host, kid)

    return ApiResponse(data={"saved": True})


@router.post("/weather/test")
async def test_weather_connection() -> ApiResponse[dict]:
    """测试和风天气连接（用当前已保存的 host/kid/sub/private_key）。

    前端「测试连接」按钮调用。
    """
    weather_cfg = get_config("weather", {})
    host = str(weather_cfg.get("host", "") or "").strip()
    kid = str(weather_cfg.get("kid", "") or "").strip()
    sub = str(weather_cfg.get("sub", "") or "").strip()
    private_key = str(weather_cfg.get("private_key", "") or "").strip()
    if not host:
        return ApiResponse(
            code="probe_failed",
            message="未配置天气 host",
            data={"connected": False, "reason": "bad_format", "detail": "未配置天气 host"},
        )
    result = await probe_weather(host, kid, sub, private_key)
    if not result.ok:
        logger.warning("Weather test failed: %s (%s)", result.reason, result.detail)
    return ApiResponse(
        code="probe_failed" if not result.ok else "ok",
        message=result.detail,
        data={"connected": result.ok, **result.to_dict()},
    )
