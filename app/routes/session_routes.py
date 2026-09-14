"""会话路由 — 会话管理（聊天走 /ws/chat WebSocket）。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user, require_owned_session
from ..core.exceptions import AppException

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/sessions")
async def create_session(
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    session = await container.session_store.create_session(user_id=current_user["user_id"])
    return ApiResponse(data=session.summary())


@router.get("/sessions")
async def list_sessions(
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    return ApiResponse(data=await container.session_store.list_summaries(user_id=current_user["user_id"]))


@router.delete("/sessions")
async def delete_all_sessions(
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """删除当前用户��所有会话。"""
    count = await container.session_store.delete_all(user_id=current_user["user_id"])
    return ApiResponse(data={"deleted": True, "count": count})


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    session = await require_owned_session(container, session_id, current_user)
    return ApiResponse(data=session.detail())


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    await require_owned_session(container, session_id, current_user)
    deleted = await container.session_store.delete_session(session_id)
    if not deleted:
        raise AppException("会话不存在", code="session_not_found", http_status=404)
    return ApiResponse(data={"id": session_id, "deleted": True})


@router.post("/sessions/{session_id}/undo")
async def undo_message(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """撤销最后一条用户-助手消息对。"""
    session = await require_owned_session(container, session_id, current_user)
    success = await container.session_store.undo_last_message(session_id)
    if not success:
        raise AppException("无法撤销（消息不足）", code="undo_failed", http_status=400)
    return ApiResponse(data={"undone": True, "messages": session.visible_messages()})


@router.post("/sessions/{session_id}/clear")
async def clear_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """清空会话消息但保留会话元数据。"""
    await require_owned_session(container, session_id, current_user)
    success = await container.session_store.clear_messages(session_id)
    if not success:
        raise AppException("会话不存在", code="session_not_found", http_status=404)
    return ApiResponse(data={"cleared": True})


@router.post("/sessions/{session_id}/compress")
async def compress_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """手动触发会话压缩（生成摘要）。

    与自动压缩共用同一套阈值判断：未达阈值时如实返回 compressed=False，
    不做任何压缩（自动压缩每轮对话前也会做同样检查，无需手动提前）。
    """
    session = await require_owned_session(container, session_id, current_user)
    should, _ = container.summarization_service.should_compress(session)
    if not should:
        return ApiResponse(data={
            "compressed": False,
            "reason": "below_threshold",
            "summaries": session.summaries,
            "message_count": len(session.model_messages),
        })
    await container.summarization_service.refresh_summaries(session, user_id=current_user["user_id"])
    await container.session_store.store_session(session)
    return ApiResponse(data={
        "compressed": True,
        "summaries": session.summaries,
        "message_count": len(session.model_messages),
    })
