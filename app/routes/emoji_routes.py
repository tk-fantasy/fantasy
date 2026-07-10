"""Emoji 搜索与偏好路由。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.database import Database
from ..core.exceptions import AppException
from ..schema.api_schemas import EmojiPreferenceRequest

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/emoji/search")
async def search_emoji(
    q: str = Query(..., description="搜索关键词"),
    top_k: int = Query(default=20, ge=1, le=50, description="返回数量"),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """搜索与关键词最相似的 emoji。"""
    service = container.emoji_service
    if not service.is_loaded:
        if service.is_loading:
            return ApiResponse(data={"status": "loading", "results": []})
        return ApiResponse(data={"status": "not_loaded", "results": []})

    results = await service.search(q, top_k=top_k)
    return ApiResponse(data={"status": "ok", "results": results})


@router.get("/emoji/preferences")
async def get_emoji_preferences() -> ApiResponse[list[dict]]:
    """获取全部 emoji 偏好。"""
    db = Database.get()
    prefs = await db.emoji_prefs_all()
    return ApiResponse(data=prefs)


@router.put("/emoji/preferences")
async def save_emoji_preference(
    payload: EmojiPreferenceRequest,
) -> ApiResponse[dict]:
    """保存/更新一条 emoji 偏好。"""
    scope = payload.scope
    key = payload.key
    emoji_char = payload.emoji_char

    if not scope or not key or not emoji_char:
        raise AppException("缺少必要参数", code="missing_params", http_status=400)

    db = Database.get()
    await db.emoji_pref_upsert(scope, key, emoji_char)
    return ApiResponse(data={"scope": scope, "key": key, "emoji_char": emoji_char})


@router.delete("/emoji/preferences/{scope}/{key}")
async def delete_emoji_preference(
    scope: str,
    key: str,
) -> ApiResponse[dict]:
    """删除一条 emoji 偏好（恢复默认）。"""
    db = Database.get()
    deleted = await db.emoji_pref_delete(scope, key)
    return ApiResponse(data={"deleted": deleted, "scope": scope, "key": key})
