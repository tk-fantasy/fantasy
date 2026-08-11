"""飞书 WebSocket 长连接客户端 —— Phase 4。

飞书用户私聊/群聊 @机器人 → 长连接收到事件 → 调宿主 Dispatcher → 回复发飞书。

使用 lark-oapi 的 WebSocket 长连接模式（不需要公网 URL、不需要 ngrok）。
ws_client.start() 是同步阻塞的，在后台 daemon 线程跑。
事件回调在线程中执行，通过 run_coroutine_threadsafe 调宿主的 async dispatch。
"""

import asyncio
import json
import logging
import re
import threading
from typing import Callable, Awaitable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    CreateMessageRequest,
    CreateMessageRequestBody,
)

logger = logging.getLogger(__name__)

# 去掉 @mention 的正则（群聊消息含 @_user_1）
_AT_MENTION_RE = re.compile(r"@_user_\d+")


class FeishuBot:
    """飞书 WebSocket 长连接 bot。

    start(dispatch_fn, loop) 在后台线程启动长连接。
    收到消息后通过 dispatch_fn 调宿主 LLM，拿到回复后用 lark client 发消息。
    stop() 清理连接。
    """

    def __init__(self, app_id: str, app_secret: str,
                 verification_token: str = "", encrypt_key: str = ""):
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        self._dispatch_fn: Callable[..., Awaitable[str]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: lark.ws.Client | None = None
        self._thread: threading.Thread | None = None
        self._lark_client: lark.Client | None = None  # 用于发消息
        # run_coroutine_threadsafe 返回的 future 需保留引用，避免被 GC 取消
        self._pending_tasks: set = set()

    def start(self, dispatch_fn: Callable[..., Awaitable[str]],
              loop: asyncio.AbstractEventLoop) -> None:
        """启动飞书长连接。

        Args:
            dispatch_fn: async (query, session_id, user_id) -> str（宿主 LLM 处理）
            loop: 主线程的 asyncio event loop（事件回调通过它调 dispatch）
        """
        self._dispatch_fn = dispatch_fn
        self._loop = loop

        # lark client 用于发消息（线程安全，可在线程间复用）
        self._lark_client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .build()
        )

        # 事件分发器
        event_handler = (
            lark.EventDispatcherHandler.builder(
                self._verification_token, self._encrypt_key)
            .register_p2_im_message_receive_v1(self._on_message_receive)
            .build()
        )

        # WebSocket 客户端
        self._ws_client = lark.ws.Client(
            app_id=self._app_id,
            app_secret=self._app_secret,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # 后台线程启动长连接（start() 同步阻塞，daemon=True 随主进程退出）
        # 关键：lark-oapi ws.client 模块级有全局 loop 变量（import 时用 get_event_loop() 拿的），
        # 主线程已 running → 拿到的是主线程 loop → 线程内 run_until_complete 报 "already running"。
        # 解法：线程内创建独立 loop，覆盖 lark-oapi 的模块级 loop 变量。
        def _run_ws():
            import lark_oapi.ws.client as ws_mod
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            ws_mod.loop = new_loop  # 覆盖 lark-oapi 模块级 loop
            self._ws_client.start()

        self._thread = threading.Thread(target=_run_ws, daemon=True)
        self._thread.start()
        logger.info("飞书 WebSocket 长连接已启动")

    def stop(self) -> None:
        """停止飞书长连接。"""
        # lark ws.Client 可能没有显式 stop 方法，daemon 线程随主进程退出
        if self._ws_client and hasattr(self._ws_client, "close"):
            try:
                self._ws_client.close()
            except Exception:
                pass
        logger.info("飞书长连接已停止")

    def _on_message_receive(self, data: P2ImMessageReceiveV1) -> None:
        """收到飞书消息事件（在 ws 线程中执行）。

        关键：不在 ws 线程里等待 LLM 结果！
        之前用 future.result(timeout=120) 阻塞 ws 线程，导致心跳停跳，
        飞书判定掉线→断连→重连→重推未确认消息→重复回复。
        现在改为 fire-and-forget：ws 线程只投递任务到主 loop，立即返回，
        让 ws 心跳正常维持。LLM 结果在主 loop 的 task 里拿，拿到后发飞书。
        """
        chat_id = None
        try:
            msg = data.event.message
            chat_id = msg.chat_id
            user_id = data.event.sender.sender_id.open_id

            # 只处理文本消息
            if msg.message_type != "text":
                return

            # 提取消息内容
            content = json.loads(msg.content)
            raw_text = content.get("text", "")

            # 去掉 @mention（群聊时消息含 @_user_1）
            query = _AT_MENTION_RE.sub("", raw_text).strip()
            if not query:
                return

            logger.info("飞书收到消息: chat_id=%s, query=%s", chat_id, query[:100])

            # fire-and-forget：投递到主 loop，不等结果，ws 线程立即返回保心跳。
            # 保留返回的 future 引用到 _pending_tasks，完成后再移除——
            # 否则 future 可能被 GC 取消，导致飞书消息静默丢失。
            session_id = f"feishu_{chat_id}"
            fut = asyncio.run_coroutine_threadsafe(
                self._handle_and_reply(query, session_id, f"feishu_{user_id}", chat_id),
                self._loop,
            )
            self._pending_tasks.add(fut)
            fut.add_done_callback(self._pending_tasks.discard)

        except Exception as e:
            logger.warning("飞书消息处理失败: %s", e)
            # 出错时尝试通知用户（_send_message 是 async，本同步回调不能直接 await，
            # 投递到主 loop 执行）
            if chat_id and self._loop:
                fut = asyncio.run_coroutine_threadsafe(
                    self._send_message(chat_id, "抱歉，处理消息时出错了。"),
                    self._loop,
                )
                self._pending_tasks.add(fut)
                fut.add_done_callback(self._pending_tasks.discard)

    async def _handle_and_reply(self, query: str, session_id: str,
                                user_id: str, chat_id: str) -> None:
        """在主 loop 中处理消息并回复（由 _on_message_receive 投递）。

        放到主 loop 跑：不阻塞 ws 线程，心跳正常，不会重连重推。
        支持斜杠命令：
          /clear  清空当前飞书会话上下文
          /help   显示可用命令
        """
        # 斜杠命令优先处理（不经过 LLM）
        cmd = query.strip().lower()
        if cmd == "/clear":
            await self._clear_session(session_id)
            await self._send_message(chat_id, "✅ 会话上下文已清空，重新开始对话。")
            return
        if cmd == "/help":
            await self._send_message(chat_id, (
                "🔧 可用命令：\n"
                "/clear - 清空对话上下文\n"
                "/help  - 显示此帮助\n\n"
                "直接发消息即可与 Aether 对话、控制智能家居。"
            ))
            return

        try:
            reply = await self._dispatch_fn(query, session_id, user_id)
            if reply:
                await self._send_message(chat_id, reply)
        except Exception as e:
            logger.warning("飞书消息处理失败: %s", e)
            try:
                await self._send_message(chat_id, "抱歉，处理消息时出错了。")
            except Exception:
                pass

    async def _clear_session(self, session_id: str) -> None:
        """清空指定 session 的历史（调宿主 session_store）。"""
        try:
            from app.container import get_container
            container = get_container()
            await container.session_store.clear_messages(session_id)
            logger.info("飞书 session %s 已清空", session_id)
        except Exception as e:
            logger.warning("清空 session 失败: %s", e)

    async def _send_message(self, chat_id: str, text: str) -> None:
        """用 lark client 异步发消息到指定 chat_id。

        用 acreate（原生异步）而非 create（同步阻塞）：_handle_and_reply 跑在主
        asyncio loop 上，同步 create 的 HTTP 往返会阻塞整个 loop，影响摄像头帧
        处理和其他用户消息。acreate 让 lark 的 HTTP 调用让出 loop。
        """
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("text")
                .content(json.dumps({"text": text}))
                .build()
            )
            .build()
        )
        resp = await self._lark_client.im.v1.message.acreate(req)
        if not resp.success():
            logger.warning("飞书发消息失败: code=%s, msg=%s",
                           resp.code, resp.msg)
