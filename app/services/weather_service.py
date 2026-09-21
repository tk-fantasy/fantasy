"""和风天气服务 — JWT 认证 + SQLite 缓存。

降级链（阶段3）：和风（生活指数特有，城市/区级定位）优先；未配置/失败时回落
HA weather 实体（met.no 出厂默认启用、零配置免 key，IP 级定位，无生活指数）。
两源输出同构字段，format_weather_* 与下游提示词/规则评估零感知。
"""
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

# HA service 引用（list[0] 热替换模式；main 启动 + sync_ha_runtime_refs 同步）
_ha_service_ref: list[Any] = [None]


def set_ha_service(svc: Any) -> None:
    """注入/热替换 HA service（weather 实体兜底数据源）。"""
    _ha_service_ref[0] = svc


def _qweather_configured() -> bool:
    """和风四要素（host/kid/sub/private_key）齐全才发请求。"""
    cfg = get_config("weather", {}) or {}
    return bool(cfg.get("host") and cfg.get("kid")
                and cfg.get("sub") and cfg.get("private_key"))


# HA weather state（英文 token）→ 中文，与和风 text 口径对齐
_HA_CONDITION_ZH = {
    "clear-night": "晴夜", "cloudy": "阴", "exceptional": "极端天气", "fog": "雾",
    "hail": "冰雹", "lightning": "雷", "lightning-rainy": "雷雨",
    "partlycloudy": "多云", "pouring": "暴雨", "rainy": "雨", "snowy": "雪",
    "snowy-rainy": "雨夹雪", "sunny": "晴", "windy": "大风", "windy-variant": "大风",
}


def _beaufort_from_kmh(kmh: Any) -> str:
    """km/h 风速 → 蒲福风级（HA wind_speed 单位是 km/h，和风 wind_scale 是风级）。"""
    try:
        v = float(kmh)
    except (TypeError, ValueError):
        return ""
    for level, upper in ((1, 5), (2, 11), (3, 19), (4, 28), (5, 38), (6, 49),
                         (7, 61), (8, 74), (9, 88), (10, 102), (11, 117)):
        if v < upper:
            return str(level)
    return "12"


async def _get_ha_weather() -> dict[str, Any] | None:
    """HA weather 实体兜底。不可用/无实体/失败返回 None（调用方继续降级）。

    实体选择：weather.ha_entity 显式配置优先，缺省自动取第一个 weather 实体。
    """
    svc = _ha_service_ref[0]
    if svc is None:
        return None
    try:
        entity = str(get_config("weather.ha_entity", "") or "").strip()
        if not entity:
            ents = await svc.get_entities_by_domains({"weather"})
            if not ents:
                return None
            entity = ents[0]["entity_id"]
        client = getattr(svc, "_client", None)
        if client is None:
            return None
        state = await client.get_state(entity)
        if not state:
            return None
        attrs = state.get("attributes") or {}
        cond = str(state.get("state", ""))
        return {
            "location": str(attrs.get("friendly_name") or entity),
            "location_id": entity,
            "temperature": attrs.get("temperature", ""),
            "feels_like": attrs.get("apparent_temperature", ""),
            "humidity": attrs.get("humidity", ""),
            "weather": _HA_CONDITION_ZH.get(cond, cond),
            "wind_dir": str(attrs.get("wind_direction", "") or ""),
            "wind_scale": _beaufort_from_kmh(attrs.get("wind_speed")),
            "wind_speed": attrs.get("wind_speed", ""),
            "visibility": "",
            "uv_index": attrs.get("uv_index", ""),
            "icon": "",
            "obs_time": state.get("last_updated", ""),
            "indices": [],  # met.no 无生活指数（和风特有）
            "source": "ha",
        }
    except Exception:
        logger.warning("HA weather fallback failed", exc_info=True)
        return None


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
        except Exception as e:  # noqa: BLE001
            last_error = e
            if attempt < _MAX_RETRIES:
                logger.warning("QWeather request failed (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES + 1, e)
                import asyncio
                await asyncio.sleep(1 * (attempt + 1))
    raise last_error


async def get_weather(location: str | None = None) -> dict[str, Any]:
    """获取天气信息（降级链：和风优先 → HA weather 兜底；SQLite 缓存 15 分钟）。

    - 和风已配置且成功 → 和风数据（含生活指数，定位城市/区级）
    - 和风未配置/请求失败且查的是家庭位置 → HA weather 实体（met.no 出厂
      默认零配置，IP 级定位，无生活指数）兜底
    - 显式指定了其他城市时不走 HA（HA 实体只有自己那一个位置）
    直接从 home 配置的省市区查询，不做 IP 定位。

    Args:
        location: 位置标识，格式 "经度,纬度" 或城市名。为 None 时使用 home 配置。

    Returns:
        天气数据字典（HA 路径带 source="ha" 标记）
    """
    db = Database.get()
    explicit = bool(location)

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
            # 没配家庭地址：和风没法定位，直接走 HA（HA 实体自带定位）
            ha = await _get_ha_weather()
            if ha is not None:
                return ha
            return {"error": "请先在设置中配置家庭地址（省市区），或在 HA 中启用天气集成",
                    "location": ""}

    cache_key = f"weather:cache:{location}"

    # 检查缓存（两源共用同一 key：都是「这个位置现在什么天」）
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

    async def _cache_and_return(result: dict) -> dict:
        await db.kv_set(cache_key, json.dumps(
            {"weather": result, "cached_at": time.time()}, ensure_ascii=False))
        return result

    # 第一优先：和风（配置齐全才发请求，未配置不白等重试超时）
    if _qweather_configured():
        try:
            result = await _fetch_qweather(location)
        except Exception as e:
            logger.exception("Failed to fetch weather from QWeather")
            result = {"error": f"获取天气失败: {e}", "location": location}
        if not result.get("error"):
            return await _cache_and_return(result)
        qweather_error = result
    else:
        qweather_error = {"error": "和风天气未配置", "location": location}

    # 降级：HA weather 实体（仅家庭位置；显式城市查不了别的位置）
    if not explicit:
        ha = await _get_ha_weather()
        if ha is not None:
            return await _cache_and_return(ha)
        return {"error": "天气服务不可用（和风未配置/失败，且 HA 无 weather 实体）",
                "location": location}
    return qweather_error


async def _fetch_qweather(location: str) -> dict[str, Any]:
    """和风天气数据获取（无缓存；错误以 {"error": ...} 返回，由调用方降级）。"""
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

    return {
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
        "source": "qweather",
    }


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
