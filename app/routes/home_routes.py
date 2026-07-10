"""家庭信息路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends

from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.database import Database
from ..schema.api_schemas import HomeInfoRequest

router = APIRouter()


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

    # 保存
    await db.user_setting_set(current_user["user_id"], "home_info", json.dumps(home, ensure_ascii=False))

    return ApiResponse(data={
        "home_name": home.get("home_name", ""),
        "owner_name": home.get("owner_name", ""),
        "province": home.get("province", ""),
        "city": home.get("city", ""),
        "district": home.get("district", ""),
    })
