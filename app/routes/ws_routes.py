"""WebSocket 路由 — 聊天和文档助手 WebSocket 端点。"""
from __future__ import annotations

import asyncio
import logging
from queue import Queue as _Queue

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..container import get_container
from ..core.tracing import new_request_id, set_request_id
from ..schema.chat_schema import Event, Nlp

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    """WebSocket 聊天端点。"""
    from ..main import _ws_verify_token, _ws_heartbeat
    user_id = await _ws_verify_token(websocket)
    if user_id is None:
        return
    container = get_container()
    await websocket.accept()

    heartbeat_task = asyncio.create_task(_ws_heartbeat(websocket))
    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") == "pong":
                continue
            # 每条消息独立 request_id
            rid = payload.get("request_id") or new_request_id()
            set_request_id(rid)
            logger.info(
                "Received websocket chat event",
                extra={"session_id": payload.get("session_id"), "query": payload.get("query", "")[:120]},
            )
            event = Event.build_event(
                Nlp.Request(query=payload.get("query", "")),
                request_id=rid,
                session_id=payload.get("session_id"),
            )
            # 使用流式推送，传递 user_id
            await container.dispatcher.dispatch_stream(event, websocket.send_json, user_id=user_id)
            set_request_id("-")
    except WebSocketDisconnect:
        logger.info("Chat websocket disconnected")
    finally:
        heartbeat_task.cancel()


@router.websocket("/ws/doc/chat")
async def doc_chat_ws(websocket: WebSocket):
    """WebSocket 文档助手端点 — RAG 流水线 + 流式推送。"""
    from ..main import _ws_verify_token, _ws_heartbeat, _stream_executor
    from ..services.prompt_service import RAG_SYSTEM_PROMPT_TEMPLATE
    container = get_container()
    rag_service = container.rag_service
    user_id = await _ws_verify_token(websocket)
    if user_id is None:
        return
    await websocket.accept()

    loop = asyncio.get_running_loop()
    heartbeat_task = asyncio.create_task(_ws_heartbeat(websocket))
    try:
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") == "pong":
                continue

            query = payload.get("query", "").strip()
            if not query:
                continue

            # 每条消息独立 request_id
            rid = payload.get("request_id") or new_request_id()
            set_request_id(rid)

            if rag_service is None or not rag_service.is_ready:
                await websocket.send_json({"type": "error", "message": "RAG 索引未就绪，请稍后刷新页面重试"})
                continue

            # 1. RAG 搜索
            context = await rag_service.search(query)
            system = RAG_SYSTEM_PROMPT_TEMPLATE.format(context=context)

            # 2. LLM 流式调用（线程池 + Queue 传 token）
            client, chat_model = rag_service.build_llm_client(user_id=user_id)

            token_queue: _Queue = _Queue()

            def _run_stream():
                try:
                    stream = client.chat.completions.create(
                        model=chat_model,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": query}
                        ],
                        stream=True
                    )
                    for chunk in stream:
                        if chunk.choices[0].delta.content:
                            token_queue.put(("token", chunk.choices[0].delta.content))
                    token_queue.put(("done", None))
                except Exception as e:
                    token_queue.put(("error", str(e)))

            _stream_executor.submit(_run_stream)

            # 3. 从 queue 读取并推送到 WebSocket
            try:
                while True:
                    kind, content = await loop.run_in_executor(None, token_queue.get)
                    if kind == "done":
                        break
                    if kind == "error":
                        await websocket.send_json({"type": "error", "message": content})
                        break
                    await websocket.send_json({"type": "token", "content": content})
            except WebSocketDisconnect:
                break
            finally:
                set_request_id("-")

    except WebSocketDisconnect:
        logger.info("Doc chat websocket disconnected")
    finally:
        heartbeat_task.cancel()
