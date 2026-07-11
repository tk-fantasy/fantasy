"""会话路由 — 聊天和会话管理。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends

from ..container import AppContainer, get_container
from ..core.api_models import ApiResponse
from ..core.auth import get_current_user
from ..core.exceptions import AppException
from ..schema.api_schemas import ChatRequest
from ..schema.chat_schema import Event, Nlp

logger = logging.getLogger(__name__)

router = APIRouter()


async def _check_session_owner(
    container: AppContainer, session_id: str, current_user: dict
):
    """校验 session 归属当前用户，返回 session 对象；不归属则抛 403。"""
    session = await container.session_store.get_session(session_id)
    if session is None:
        raise AppException("会话不存在", code="session_not_found", http_status=404)
    if session.user_id and session.user_id != current_user["user_id"]:
        raise AppException("无权访问该会话", code="forbidden", http_status=403)
    return session


@router.post("/chat")
async def chat(
    payload: ChatRequest,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[list[dict]]:
    if container.dispatcher is None:
        raise AppException("Dispatcher 未就绪", code="dispatcher_not_ready", http_status=503)
    request_id = payload.request_id
    session_id = payload.session_id
    query = payload.query
    logger.info("Received chat request", extra={"session_id": session_id, "query": query[:120]})
    event = Event.build_event(Nlp.Request(query=query), request_id=request_id, session_id=session_id)
    instructions = await container.dispatcher.dispatch(event, user_id=current_user["user_id"])
    return ApiResponse(data=[instruction.model_dump() for instruction in instructions])


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
    session = await _check_session_owner(container, session_id, current_user)
    return ApiResponse(data=session.detail())


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    await _check_session_owner(container, session_id, current_user)
    deleted = await container.session_store.delete_session(session_id)
    if not deleted:
        raise AppException("会话不存在", code="session_not_found", http_status=404)
    return ApiResponse(data={"id": session_id, "deleted": True})


@router.post("/sessions/{session_id}/fork")
async def fork_session(
    session_id: str,
    payload: dict = Body(default={}),
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    await _check_session_owner(container, session_id, current_user)
    message_id = str((payload or {}).get("message_id", ""))
    forked = await container.session_store.fork_session(session_id, message_id, user_id=current_user["user_id"])
    if forked is None:
        raise AppException("源会话不存在", code="session_not_found", http_status=404)
    return ApiResponse(data=forked.summary())


@router.post("/sessions/{session_id}/undo")
async def undo_message(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    container: AppContainer = Depends(get_container),
) -> ApiResponse[dict]:
    """撤销最后一条用户-助手消息对。"""
    session = await _check_session_owner(container, session_id, current_user)
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
    await _check_session_owner(container, session_id, current_user)
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
    """手动触发会话压缩（生成摘要）。"""
    session = await _check_session_owner(container, session_id, current_user)
    await container.summarization_service.refresh_summaries(session)
    await container.session_store.store_session(session)
    return ApiResponse(data={
        "compressed": True,
        "summaries": session.summaries,
        "message_count": len(session.model_messages),
    })
