"""家庭信息路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.config import update_config_section
from ..core.database import Database
from ..schema.api_schemas import HomeInfoRequest

router = APIRouter()


async def _sync_home_to_global_config(home: dict) -> None:
    """把家庭信息镜像到全局 config.json 的 home 段。

    原因：这是「家庭软件」，家庭地址是全局共享配置，不应 per-user 隔离。
    但历史代码把它存进了 user_settings 表（per-user），而 weather_service.get_weather()
    读的是全局 config 的 home 段，两边不通导致天气组件永远空白。
    这里在写 DB 的同时镜像一份到 config，让 get_weather 能读到。
    DB 仍然保留，是为了不破坏 setup_routes / 其他读 user_settings.home_info 的代码。
    """
    update_config_section("home", {
        "home_name": home.get("home_name", ""),
        "owner_name": home.get("owner_name", ""),
        "province": home.get("province", ""),
        "city": home.get("city", ""),
        "district": home.get("district", ""),
    })


@router.get("/home/info")
async def get_home_info(current_user: dict = Depends(get_current_user)) -> ApiResponse[dict]:
    """获取当前用户的家庭信息。"""
    db = Database.get()
    settings = await db.user_settings_all(current_user["user_id"])
    home_json = settings.get("home_info", "{}")
    try:
        home = json.loads(home_json)
    except json.JSONDecodeError:
        home = {}

    return ApiResponse(data={
        "home_name": home.get("home_name", ""),
        "owner_name": home.get("owner_name", ""),
        "province": home.get("province", ""),
        "city": home.get("city", ""),
        "district": home.get("district", ""),
    })


@router.post("/home/info")
async def set_home_info(
    payload: HomeInfoRequest,
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """更新当前用户的家庭信息。"""
    db = Database.get()

    # 读取现有设置
    settings = await db.user_settings_all(current_user["user_id"])
    home_json = settings.get("home_info", "{}")
    try:
        home = json.loads(home_json)
    except json.JSONDecodeError:
        home = {}

    # 更新字段（只更新非空值）
    if payload.home_name:
        home["home_name"] = payload.home_name.strip()
    if payload.owner_name:
        home["owner_name"] = payload.owner_name.strip()
    if payload.province:
        home["province"] = payload.province.strip()
    if payload.city:
        home["city"] = payload.city.strip()
    if payload.district:
        home["district"] = payload.district.strip()

    # 保存（DB 保留兼容旧逻辑 + 镜像到全局 config 供 weather_service 读取）
    await db.user_setting_set(current_user["user_id"], "home_info", json.dumps(home, ensure_ascii=False))
    await _sync_home_to_global_config(home)

    return ApiResponse(data={
        "home_name": home.get("home_name", ""),
        "owner_name": home.get("owner_name", ""),
        "province": home.get("province", ""),
        "city": home.get("city", ""),
        "district": home.get("district", ""),
    })
