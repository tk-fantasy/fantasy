"""Emoji 搜索与偏好路由。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
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


@router.post("/emoji/rebuild")
async def rebuild_emoji_index(
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """触发 emoji 向量索引重建（后台异步执行）。

    索引文件存在时，从现有索引读取 emoji 元数据（char/code/name），
    用 embed client 重新生成每个 emoji 的语义向量；
    索引文件不存在时，从内置种子（app/data/emoji_seed.json）加载元数据，
    首次创建完整索引。需先在设置页配置全局 embed LLM Key。
    """
    service = container.emoji_service

    if service.rebuild_status["running"]:
        raise AppException("索引重建正在进行中，请勿重复触发",
                           code="rebuild_in_progress", http_status=409)

    embed_client = container.embed_client
    if not embed_client.enabled:
        raise AppException("Embed 模型未配置或未启用，请先在设置页配置 LLM Key",
                           code="embed_not_configured", http_status=400)

    # 后台执行重建（不阻塞响应）
    from ..main import _background_task_mgr
    _background_task_mgr.spawn(service.rebuild_index(), name="emoji_rebuild")

    return ApiResponse(data={"status": "started", "message": "索引重建已开始，请轮询进度"})


@router.get("/emoji/rebuild/status")
async def get_rebuild_status(
    container: AppContainer = Depends(get_container),
    current_user: dict = Depends(get_current_user),
) -> ApiResponse[dict]:
    """查询 emoji 索引重建进度。"""
    service = container.emoji_service
    return ApiResponse(data=service.rebuild_status)
