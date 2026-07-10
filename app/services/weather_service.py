"""和风天气服务 — JWT 认证 + SQLite 缓存。"""
from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from cryptography.hazmat.primitives import serialization

from ..core.config import get_config
from ..core.database import Database
from ..clients.http_client import new_client

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 15 * 60  # 15 分钟
_TIMEOUT = 15
_MAX_RETRIES = 2


def _generate_jwt() -> str:
    """生成和风天气 JWT token（Ed25519 签名）。
    
    和风天气要求：
    - JWT header 里包含 alg="EdDSA" 和 kid
    - JWT payload 里包含 sub, iat, exp（不包含 iss）
    - 使用 Ed25519 私钥签名
    """
    weather_cfg = get_config("weather", {})
    private_key_b64 = weather_cfg.get("private_key", "")
    kid = weather_cfg.get("kid", "")
    sub = weather_cfg.get("sub", "")

    if not private_key_b64:
        raise ValueError("和风天气 private_key 未配置")

    # 构造 PEM 格式密钥
    if not private_key_b64.startswith("-----"):
        key_lines = [private_key_b64[i:i+64] for i in range(0, len(private_key_b64), 64)]
        private_key_pem = (
            b"-----BEGIN PRIVATE KEY-----\n"
            + "\n".join(key_lines).encode()
            + b"\n-----END PRIVATE KEY-----"
        )
    else:
        private_key_pem = private_key_b64.encode()

    # 加载私钥
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    
    # 手动构造 JWT（和风天气要求 header 里带 kid）
    def b64u(d):
        return base64.urlsafe_b64encode(d).rstrip(b"=").decode()
    
    # Header: alg + kid
    header = b64u(json.dumps({"alg": "EdDSA", "kid": kid}, separators=(",", ":")).encode())
    
    # Payload: sub + iat + exp（不包含 iss）
    now = int(time.time())
    payload = b64u(json.dumps({
        "sub": sub,
        "iat": now - 30,  # 稍微提前30秒，避免时钟偏差
        "exp": now + 900,  # 15分钟有效期
    }, separators=(",", ":")).encode())
    
    # 签名
    signing_input = f"{header}.{payload}".encode()
    signature = b64u(private_key.sign(signing_input))
    
    return f"{header}.{payload}.{signature}"


async def _qweather_request(path: str, params: dict[str, str] | None = None) -> dict:
    """请求和风天气 API（带重试）。"""
    weather_cfg = get_config("weather", {})
    host = weather_cfg.get("host", "")
    if not host:
        raise ValueError("和风天气 host 未配置")

    token = _generate_jwt()
    url = f"https://{host}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
    }

    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with new_client(timeout=_TIMEOUT) as client:
                resp = await client.get(url, headers=headers, params=params or {})
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                logger.warning("QWeather request failed (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES + 1, e)
                import asyncio
                await asyncio.sleep(1 * (attempt + 1))
    raise last_error


async def get_weather(location: str | None = None) -> dict[str, Any]:
    """获取天气信息（带 SQLite 缓存）。

    直接从 home 配置的省市区查询，不做 IP 定位。

    Args:
        location: 位置标识，格式 "经度,纬度" 或城市名。为 None 时使用 home 配置。

    Returns:
        天气数据字典
    """
    db = Database.get()

    # 确定查询位置
    if not location:
        home_cfg = get_config("home", {})
        province = home_cfg.get("province", "")
        city = home_cfg.get("city", "")
        district = home_cfg.get("district", "")
        
        if city:
            # 直辖市（省市同名）只用市名，否则用省+市
            if province and province != city:
                location = f"{province}{city}"
            else:
                location = city
        else:
            return {"error": "请先在设置中配置家庭地址（省市区）", "location": ""}

    cache_key = f"weather:cache:{location}"

    # 检查缓存
    cached = await db.kv_get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            cached_at = data.get("cached_at", 0)
            if time.time() - cached_at < _CACHE_TTL_SECONDS:
                logger.debug("Weather cache hit for %s", location)
                return data.get("weather", {})
        except (json.JSONDecodeError, TypeError):
            pass

    # 缓存过期或不存在，请求和风天气 API
    try:
        # 用 GeoAPI 查询 Location ID
        geo_data = await _qweather_request(
            "/geo/v2/city/lookup",
            {"location": location},
        )
        locations = geo_data.get("location", [])
        if isinstance(locations, dict):
            locations = [locations]
        if not locations:
            return {"error": f"找不到地点: {location}", "location": location}

        loc_info = locations[0]
        location_id = loc_info.get("id", "")
        location_name = loc_info.get("name", location)
        adm1 = loc_info.get("adm1", "")
        # 避免冗余，如"上海市上海"或"上海市宝山区"
        # 如果 adm1 包含 location_name 或反过来，只显示一个
        if adm1 and location_name:
            if adm1 in location_name or location_name in adm1:
                full_name = location_name
            else:
                full_name = f"{adm1}{location_name}"
        else:
            full_name = location_name

        # 并行请求天气预报和天气指数
        import asyncio
        weather_task = _qweather_request(
            "/v7/weather/now",
            {"location": location_id},
        )
        indices_task = _qweather_request(
            "/v7/indices/1d",
            {"location": location_id, "type": "1,2,3,5,9,15,16"},
        )
        weather_data, indices_data = await asyncio.gather(weather_task, indices_task, return_exceptions=True)

        now_weather = weather_data.get("now", {}) if not isinstance(weather_data, Exception) else {}
        daily_indices = indices_data.get("daily", []) if not isinstance(indices_data, Exception) else []

        result = {
            "location": full_name,
            "location_id": location_id,
            "temperature": now_weather.get("temp", ""),
            "feels_like": now_weather.get("feelsLike", ""),
            "humidity": now_weather.get("humidity", ""),
            "weather": now_weather.get("text", ""),
            "wind_dir": now_weather.get("windDir", ""),
            "wind_scale": now_weather.get("windScale", ""),
            "wind_speed": now_weather.get("windSpeed", ""),
            "visibility": now_weather.get("vis", ""),
            "uv_index": now_weather.get("uvIndex", ""),
            "icon": now_weather.get("icon", ""),
            "obs_time": now_weather.get("obsTime", ""),
            "indices": [
                {
                    "type": item.get("type", ""),
                    "name": item.get("name", ""),
                    "level": item.get("level", ""),
                    "category": item.get("category", ""),
                    "text": item.get("text", ""),
                }
                for item in daily_indices
            ],
        }

        # 写入缓存
        cache_data = {"weather": result, "cached_at": time.time()}
        await db.kv_set(cache_key, json.dumps(cache_data, ensure_ascii=False))

        return result

    except Exception as e:
        logger.exception("Failed to fetch weather from QWeather")
        return {"error": f"获取天气失败: {e}", "location": location}


async def ip_locate() -> dict[str, Any]:
    """根据 IP 自动定位。"""
    try:
        geo_data = await _qweather_request(
            "/geo/v2/city/lookup",
            {"location": "auto"},
        )
        loc = geo_data.get("location", {})
        if isinstance(loc, list) and loc:
            loc = loc[0]
        return {
            "name": loc.get("name", ""),
            "adm1": loc.get("adm1", ""),
            "adm2": loc.get("adm2", ""),
            "lat": loc.get("lat", ""),
            "lon": loc.get("lon", ""),
            "id": loc.get("id", ""),
        }
    except Exception as e:
        logger.exception("IP locate failed")
        return {"error": f"定位失败: {e}"}


async def city_lookup(query: str) -> dict[str, Any]:
    """城市搜索：根据城市名查询 Location ID。

    Args:
        query: 城市名称（如"北京"、"上海浦东"）

    Returns:
        匹配的城市列表
    """
    try:
        geo_data = await _qweather_request(
            "/geo/v2/city/lookup",
            {"location": query},
        )
        locations = geo_data.get("location", [])
        if not isinstance(locations, list):
            locations = [locations] if locations else []
        
        result = []
        for loc in locations:
            result.append({
                "name": loc.get("name", ""),
                "adm1": loc.get("adm1", ""),
                "adm2": loc.get("adm2", ""),
                "lat": loc.get("lat", ""),
                "lon": loc.get("lon", ""),
                "id": loc.get("id", ""),
            })
        return {"cities": result}
    except Exception as e:
        logger.exception("City lookup failed")
        return {"error": f"城市查询失败: {e}", "cities": []}


async def get_weather_indices(location: str) -> dict[str, Any]:
    """获取天气生活指数（运动、洗车等）。

    Args:
        location: 城市名、Location ID 或 "经度,纬度"

    Returns:
        生活指数列表
    """
    db = Database.get()
    cache_key = f"weather:indices:{location}"

    # 检查缓存
    cached = await db.kv_get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
            cached_at = data.get("cached_at", 0)
            if time.time() - cached_at < _CACHE_TTL_SECONDS:
                return data.get("indices", {})
        except (json.JSONDecodeError, TypeError):
            pass

    try:
        # 如果 location 不是纯数字（Location ID），先查询 GeoAPI 获取 ID
        location_id = location
        if not location.replace(",", "").replace(".", "").isdigit():
            geo_data = await _qweather_request(
                "/geo/v2/city/lookup",
                {"location": location},
            )
            locations = geo_data.get("location", [])
            if isinstance(locations, list) and locations:
                location_id = locations[0].get("id", location)
            elif isinstance(locations, dict) and locations:
                location_id = locations.get("id", location)

        # type=1,2,3,5,9,15,16 表示各种生活指数
        indices_data = await _qweather_request(
            "/v7/indices/1d",
            {"location": location_id, "type": "1,2,3,5,9,15,16"},
        )
        daily = indices_data.get("daily", [])

        result = {
            "location": location,
            "location_id": location_id,
            "indices": [],
        }

        for item in daily:
            result["indices"].append({
                "type": item.get("type", ""),
                "name": item.get("name", ""),
                "level": item.get("level", ""),
                "category": item.get("category", ""),
                "text": item.get("text", ""),
            })

        # 写入缓存
        cache_data = {"indices": result, "cached_at": time.time()}
        await db.kv_set(cache_key, json.dumps(cache_data, ensure_ascii=False))

        return result
    except Exception as e:
        logger.exception("Failed to fetch weather indices")
        return {"error": f"获取指数失败: {e}", "location": location, "indices": []}


def format_weather_brief(weather_data: dict) -> str:
    """将天气数据格式化为简短字符串，供 prompt/规则评估使用。

    示例输出: "上海市 多云 25°C 湿度60%"
    """
    if not weather_data or weather_data.get("error"):
        return ""
    location = weather_data.get("location", "")
    weather = weather_data.get("weather", "")
    temp = weather_data.get("temperature", "")
    humidity = weather_data.get("humidity", "")
    return f"{location} {weather} {temp}°C 湿度{humidity}%"


def format_weather_detail(weather_data: dict) -> str:
    """将天气数据格式化为详细字符串，供系统提示词使用。

    示例输出: "当前天气：上海市 多云 25°C (体感 24°C) 湿度60% 东南风 3级"
    """
    if not weather_data or weather_data.get("error"):
        return ""
    return (
        f"当前天气：{weather_data.get('location', '')} "
        f"{weather_data.get('weather', '')} "
        f"{weather_data.get('temperature', '')}°C "
        f"(体感 {weather_data.get('feels_like', '')}°C) "
        f"湿度 {weather_data.get('humidity', '')}% "
        f"{weather_data.get('wind_dir', '')} {weather_data.get('wind_scale', '')}级"
    )
