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


async def _cancel_current(task: asyncio.Task | None, container) -> None:
    """取消当前活跃 task + 中断所有 sink 播报。"""
    if task is not None and not task.done():
        task.cancel()
        try:
            await task  # 等 CancelledError 传播完毕（Dispatcher 内部已处理）
        except (asyncio.CancelledError, Exception):
            pass  # task 内部异常已自己处理
    # 停所有 sink（即使 task 已结束，小爱可能还在念）
    layer = getattr(container, "integration_layer", None)
    if layer is not None and layer.sink_manager is not None:
        await layer.sink_manager.interrupt_all()


async def _run_dispatch(container, event, ws_send, user_id: str) -> None:
    """包装 dispatch_stream，确保异常不逃逸到 WS 循环。"""
    try:
        await container.dispatcher.dispatch_stream(event, ws_send, user_id=user_id)
    except asyncio.CancelledError:
        pass  # Dispatcher 内部已 emit Finish + interrupt
    except Exception:
        pass  # Dispatcher 内部已有异常处理


async def _handle_direct(websocket, container, payload, rid: str, user_id: str) -> None:
    """直通模式：文字路由到 inbound_router 插件（通用，不硬编码任何插件）。"""
    from ..schema.chat_schema import Dialog, Instruction

    set_request_id(rid)
    session_id = payload.get("session_id", "")
    try:
        text = payload.get("query", "")
        mode = payload.get("mode", "")
        layer = getattr(container, "integration_layer", None)
        if layer is None:
            await websocket.send_json(
                Instruction.build_instruction(
                    Dialog.Finish(success=False, message="直通失败"),
                    rid, session_id,
                ).model_dump()
            )
            return
        result = await layer.route_inbound(text, mode)
        msg = "已转交处理" if result.get("ok") else result.get("error", "直通失败")
        await websocket.send_json(
            Instruction.build_instruction(
                Dialog.Finish(success=result.get("ok", False), message=msg),
                rid, session_id,
            ).model_dump()
        )
    except Exception:
        await websocket.send_json(
            Instruction.build_instruction(
                Dialog.Finish(success=False, message="直通执行失败"),
                rid, session_id,
            ).model_dump()
        )
    finally:
        set_request_id("-")


async def _chat_loop(websocket, container, user_id: str) -> None:
    """聊天 WS 主循环（task 式，支持打断 + mode 路由）。

    current_task 是局部变量：一个连接同时只有一个活跃 task。
    收 interrupt / 新消息时 cancel 旧的 + interrupt_all。
    """
    current_task: asyncio.Task | None = None
    while True:
        payload = await websocket.receive_json()

        if payload.get("type") == "pong":
            continue

        if payload.get("type") == "interrupt":
            await _cancel_current(current_task, container)
            current_task = None
            continue

        if payload.get("type") == "chat":
            # 自动打断旧的（类 ChatGPT 体验）
            await _cancel_current(current_task, container)

            mode = payload.get("mode", "aether")
            rid = payload.get("request_id") or new_request_id()

            if mode == "aether":
                set_request_id(rid)
                event = Event.build_event(
                    Nlp.Request(query=payload.get("query", "")),
                    request_id=rid,
                    session_id=payload.get("session_id"),
                )
                current_task = asyncio.create_task(
                    _run_dispatch(container, event, websocket.send_json, user_id)
                )
                set_request_id("-")
            else:
                # 任意非默认模式：通用路由到 inbound_router（不硬编码模式名）
                current_task = asyncio.create_task(
                    _handle_direct(websocket, container, payload, rid, user_id)
                )


@router.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    """WebSocket 聊天端点。"""
    from ..core import ws_registry
    from ..main import _ws_verify_token, _ws_heartbeat
    user_id = await _ws_verify_token(websocket)
    if user_id is None:
        return
    container = get_container()
    await websocket.accept()
    # 注册到在线表：定时任务（message/reminder）执行后把回复推给该用户
    # 当前在线的连接，文字与小爱语音同步（不在线则只写会话历史）。
    ws_registry.register(user_id, websocket)

    heartbeat_task = asyncio.create_task(_ws_heartbeat(websocket))
    try:
        await _chat_loop(websocket, container, user_id)
    except WebSocketDisconnect:
        logger.info("Chat websocket disconnected")
    finally:
        ws_registry.unregister(user_id, websocket)
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
            client, chat_model = await rag_service.build_llm_client(user_id=user_id)

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
                except Exception:
                    # 异常原文可能含上游 base_url 等内部信息，客户端只收固定文案
                    logger.exception("Doc chat LLM stream failed")
                    token_queue.put(("error", "模型调用失败，请稍后重试或检查模型配置"))

            _stream_executor.submit(_run_stream)

            # 3. 从 queue 读取并推送到 WebSocket
            try:
                while True:
                    kind, content = await loop.run_in_executor(None, token_queue.get)
                    if kind == "done":
                        # 通知前端流结束,触发 finalizeStreaming() 隐藏闪烁光标
                        await websocket.send_json({"type": "done"})
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
