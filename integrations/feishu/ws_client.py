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
import time
from typing import Any, Callable, Awaitable

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    CreateMessageRequest,
    CreateMessageRequestBody,
)

logger = logging.getLogger(__name__)

# 去掉 @mention 的正则（群聊消息含 @_user_1）
_AT_MENTION_RE = re.compile(r"@_user_\d+")

# 事件去重窗口：飞书断连会重推未确认消息，同一 event_id 5 分钟内只处理一次
_EVENT_DEDUP_TTL = 300.0

# 摄像头绑定问答的等待窗口：超过就认为用户已经岔开话题，不再拦截其消息。
# 草稿本身的 TTL 是 10 分钟（pending_rules.PENDING_TTL_SECONDS），这里刻意更短——
# 用户隔很久才回一句「门口」时，那句话更可能是新指令而不是在回答绑定问题。
_CAMERA_ASK_TTL = 300.0

# 用户想表达「全部摄像头（全局）」的说法。显式全局是合法的，但必须是他自己说的，
# 不能由插件或模型替他决定——未绑定的视觉规则会在每一路画面上都触发。
_GLOBAL_WORDS = {"全部", "全局", "所有", "全部摄像头", "所有摄像头", "都", "all"}


class FeishuBot:
    """飞书 WebSocket 长连接 bot。

    start(dispatch_fn, loop) 在后台线程启动长连接。
    收到消息后通过 dispatch_fn 调宿主 LLM，拿到回复后用 lark client 发消息。
    stop() 清理连接。
    """

    def __init__(self, app_id: str, app_secret: str,
                 verification_token: str = "", encrypt_key: str = "",
                 notify_chat_id: str = ""):
        self._app_id = app_id
        self._app_secret = app_secret
        self._verification_token = verification_token
        self._encrypt_key = encrypt_key
        # 主动推送（告警/周报）目标：配置优先，否则用最近活跃会话
        self._notify_chat_id = (notify_chat_id or "").strip()
        self._last_chat_id = ""
        self._dispatch_fn: Callable[..., Awaitable[str]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_client: lark.ws.Client | None = None
        self._thread: threading.Thread | None = None
        self._lark_client: lark.Client | None = None  # 用于发消息
        # run_coroutine_threadsafe 返回的 future 需保留引用，避免被 GC 取消
        self._pending_tasks: set = set()
        # 入站缓冲管道（per-chat 串行 + 合并窗口 + 限流，通用组件）；start() 构造
        self._pipeline: "InboundPipeline | None" = None
        # 事件去重表：event_id → 首见时刻（monotonic），防飞书重推重复处理
        self._seen_events: dict[str, float] = {}
        # 摄像头绑定问答状态：session_id → {"pending_id", "chat_id", "asked_at"}。
        # 视觉规则必须绑一路摄像头才能落库（核心 confirm_pending 会拦），但飞书没有
        # 界面可选，所以由插件自己问一轮。见 _maybe_prompt_camera / _try_camera_answer。
        self._awaiting_camera: dict[str, dict] = {}

    def start(self, dispatch_fn: Callable[..., Awaitable[str]],
              loop: asyncio.AbstractEventLoop) -> None:
        """启动飞书长连接。

        Args:
            dispatch_fn: async (query, session_id, user_id) -> str（宿主 LLM 处理）
            loop: 主线程的 asyncio event loop（事件回调通过它调 dispatch）
        """
        self._dispatch_fn = dispatch_fn
        self._loop = loop

        # 入站缓冲管道：普通消息经它排队（per-chat 串行保序、合并窗口、
        # 全局限流、超时反馈）。配置可调（integration.feishu.*），默认即可用。
        # 参数失败按默认值兜底——缓冲是纯增强，不能因配置读不到而阻断启动。
        try:
            from app.core.config import get_config

            def _cfg(key: str, default):
                try:
                    return get_config(f"integration.feishu.{key}", default)
                except Exception:  # noqa: BLE001
                    return default
        except Exception:  # noqa: BLE001 — 宿主模块不可用时全默认值
            def _cfg(key: str, default):
                return default

        from app.integration.inbound_pipeline import InboundPipeline
        self._pipeline = InboundPipeline(
            self._handle_chat, self._notify_chat,
            merge_window=float(_cfg("merge_window_seconds", 2.0)),
            max_concurrency=int(_cfg("max_concurrency", 4)),
            lane_queue_size=int(_cfg("lane_queue_size", 8)),
            handler_timeout=float(_cfg("handler_timeout", 120.0)),
            processing_hint_after=float(_cfg("processing_hint_after", 15.0)),
        )

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
        """停止飞书长连接 + 收尾缓冲管道（best-effort，宿主停机是同步调用）。"""
        # lark ws.Client 可能没有显式 stop 方法，daemon 线程随主进程退出
        if self._ws_client and hasattr(self._ws_client, "close"):
            try:
                self._ws_client.close()
            except Exception:
                pass
        if self._pipeline is not None and self._loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(self._pipeline.stop(), self._loop)
                fut.result(timeout=3)
            except Exception:
                pass  # loop 已停/正在停：worker 是普通 task，随进程退出回收
            self._pipeline = None
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
            # 记录最近活跃会话：notify 主动推送的默认目标（配置 notify_chat_id 时不用它）
            self._last_chat_id = chat_id

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

            # 断连重推去重：同一 event_id 只处理一次（放行 /help 等幂等命令无害，
            # 统一去重更简单，漏一条重推命令的代价可忽略）
            event_id = str(getattr(getattr(data, "header", None), "event_id", "") or "")
            if event_id and self._is_duplicate(event_id):
                logger.info("飞书重推事件已去重: %s", event_id[:16])
                return

            logger.info("飞书收到消息: chat_id=%s, query=%s", chat_id, query[:100])

            # fire-and-forget：投递到主 loop，不等结果，ws 线程立即返回保心跳。
            # 保留返回的 future 引用到 _pending_tasks，完成后再移除——
            # 否则 future 可能被 GC 取消，导致飞书消息静默丢失。
            if query.startswith("/"):
                # 斜杠命令直达（即时响应，不进管道排队/合并）
                fut = asyncio.run_coroutine_threadsafe(
                    self._handle_and_reply(
                        query, f"feishu_{chat_id}", f"feishu_{user_id}", chat_id),
                    self._loop,
                )
            else:
                # 普通消息进缓冲管道：同会话串行保序、合并窗口、全局限流、超时反馈；
                # 管道缺失（异常退化）时回退旧的直处理路径
                if self._pipeline is not None:
                    coro = self._pipeline.submit(chat_id, query,
                                                 {"chat_id": chat_id, "open_id": user_id})
                else:
                    coro = self._handle_and_reply(
                        query, f"feishu_{chat_id}", f"feishu_{user_id}", chat_id)
                fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
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

    # ------------------------------------------------------------------
    # 摄像头绑定问答（渠道专属；核心只提供渠道无关的 pending_rules 原语）
    #
    # 为什么在插件里做：视觉规则必须显式绑一路摄像头才能落库（核心
    # confirm_pending 会拦，否则规则会在**每一路**画面上都触发），网页端有选择器，
    # 飞书没有界面 —— 只能自己问一轮。宿主核心对此零感知、零渠道分支。
    #
    # 插件 import 核心服务是本项目既定的解耦方向（main.py 的 _register_notifier、
    # 本文件的 _clear_session 都是这么做的）：核心不许 import 插件，插件可以 import 核心。
    # ------------------------------------------------------------------

    @staticmethod
    def _camera_choices() -> list[dict]:
        """启用的摄像头 [{"id","name"}]；宿主未装配/取不到时返回空表。"""
        try:
            from app.container import get_container
            manager = getattr(get_container(), "camera_manager", None)
            cameras = manager.list_cameras() if manager is not None else None
        except Exception as e:  # noqa: BLE001 — 拿不到列表就别拦着聊天主流程
            logger.warning("飞书取摄像头列表失败: %s", e)
            return []
        return [{"id": str(c.get("id", "")), "name": str(c.get("name") or c.get("id") or "")}
                for c in (cameras or [])
                if isinstance(c, dict) and c.get("enabled") is not False and c.get("id")]

    @staticmethod
    async def _draft_needing_camera(session_id: str) -> tuple[str, Any] | None:
        """该会话是否有一条「待确认且还缺摄像头绑定」的规则草稿。

        返回 (pending_id, session)；没有则 None。多条草稿时不猜（locate_pending
        只在唯一草稿时兜底），交给模型/用户自己理清。
        """
        try:
            from app.container import get_container
            from app.services.pending_rules import (
                KIND_AUTOMATION_RULE, locate_pending, needs_camera)
            session = await get_container().session_store.get_session(session_id)
            if session is None:
                return None
            pid, entry, _err = locate_pending(session, "", KIND_AUTOMATION_RULE)
            if entry is None or pid is None:
                return None
            rule = entry.get("rule")
            if not isinstance(rule, dict) or not needs_camera(rule):
                return None
            if entry.get("camera_chosen"):
                return None  # 用户已显式选过全局，不该再问
            return pid, session
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书查待确认草稿失败: %s", e)
            return None

    @staticmethod
    def _resolve_camera(text: str, choices: list[dict]) -> tuple[str | None, bool, list[str]]:
        """把用户的话解析成 camera_id。

        Returns:
            (camera_id, 是否解析成功, 歧义候选名)。camera_id="" 表示显式全局。
        """
        raw = (text or "").strip()
        if not raw:
            return None, False, []
        low = raw.lower()
        if low in _GLOBAL_WORDS or raw in _GLOBAL_WORDS:
            return "", True, []
        # 精确名字 / 精确 id
        for c in choices:
            if raw == c["name"] or low == c["id"].lower():
                return c["id"], True, []
        # 包含关系（用户常说「门口那个」「用门口摄像头」）
        hits = [c for c in choices
                if (c["name"] and c["name"] in raw) or (raw in c["name"] and len(raw) >= 2)]
        if len(hits) == 1:
            return hits[0]["id"], True, []
        if len(hits) > 1:
            return None, False, [c["name"] for c in hits]
        # 兜底走项目现成的模糊匹配（与设备消歧同一套口径）
        try:
            from app.utils.text_match import match_devices
            fuzzy = match_devices(raw, [{"entity_id": c["id"], "name": c["name"]}
                                        for c in choices])
        except Exception:  # noqa: BLE001
            fuzzy = []
        if len(fuzzy) == 1:
            return str(fuzzy[0].get("entity_id", "")), True, []
        if len(fuzzy) > 1:
            return None, False, [str(f.get("name") or f.get("entity_id") or "") for f in fuzzy]
        return None, False, []

    def _drop_camera_ask(self, session_id: str) -> None:
        self._awaiting_camera.pop(session_id, None)

    def _sweep_camera_asks(self) -> None:
        """懒清理过期的问答状态（没有后台线程，搭消息处理的顺风车）。"""
        now = time.monotonic()
        for sid in [k for k, v in self._awaiting_camera.items()
                    if now - v.get("asked_at", 0.0) > _CAMERA_ASK_TTL]:
            self._awaiting_camera.pop(sid, None)

    async def _maybe_prompt_camera(self, session_id: str, chat_id: str) -> None:
        """本轮 LLM 回完后：若留下一条缺摄像头的视觉规则草稿，追问绑哪一路。

        刻意放在 dispatch 之后而不是拦在之前 —— 规则解析是模型的活，插件只补
        模型补不了的那一步（无界面时的选择）。失败一律静默降级：网页端仍能建，
        语音端最坏是确认时被核心拦下并如实告知，不会静默落一条危险规则。
        """
        try:
            found = await self._draft_needing_camera(session_id)
            if found is None:
                return
            pending_id, _session = found
            choices = self._camera_choices()
            if not choices:
                # 一路摄像头都没有：视觉规则不可能触发，直接说清，别让用户白答一轮
                await self._send_message(
                    chat_id, "⚠️ 这条规则要看摄像头画面，但当前没有可用摄像头，"
                             "创建后也不会触发。请先在摄像头设置里添加并启用一路。")
                return
            names = "、".join(c["name"] for c in choices)
            self._awaiting_camera[session_id] = {
                "pending_id": pending_id, "chat_id": chat_id,
                "asked_at": time.monotonic(),
            }
            await self._send_message(
                chat_id,
                f"📷 这条规则要靠摄像头画面判断，得指定看哪一路：{names}\n"
                f"回复摄像头名字即可；想让每一路都触发就回复「全部」。")
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书摄像头追问失败（不影响主回复）: %s", e)

    async def _try_camera_answer(self, session_id: str, query: str, chat_id: str) -> bool:
        """用户在回答「看哪路摄像头」吗？是就吃掉这条消息（不进 LLM）。

        Returns:
            True = 已处理，调用方不要再 dispatch。
        """
        self._sweep_camera_asks()
        state = self._awaiting_camera.get(session_id)
        if state is None:
            return False
        choices = self._camera_choices()
        camera_id, ok, _ambiguous = self._resolve_camera(query, choices)
        if not ok:
            # 认不出（含歧义）就把消息还给 LLM，别吞掉用户的指令。草稿仍在 TTL 内，
            # 之后确认时会被核心 confirm_pending 拦下并如实说明缺摄像头。
            self._drop_camera_ask(session_id)
            logger.info("飞书摄像头答复未识别（%s），交回 LLM", query[:40])
            return False
        self._drop_camera_ask(session_id)
        try:
            from app.container import get_container
            from app.services.pending_rules import set_pending_camera
            session = await get_container().session_store.get_session(session_id)
            if session is None:
                await self._send_message(chat_id, "会话已失效，请重新说一遍需求。")
                return True
            result = set_pending_camera(session, state["pending_id"], camera_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("飞书写入摄像头绑定失败: %s", e)
            await self._send_message(chat_id, f"绑定摄像头时出错了：{e}")
            return True
        if not result.get("ok"):
            await self._send_message(
                chat_id, f"❌ {result.get('error', '绑定失败')}，请重新说一遍需求。")
            return True
        label = "全部摄像头（每一路画面都会触发）" if camera_id == "" else next(
            (c["name"] for c in choices if c["id"] == camera_id), camera_id)
        await self._send_message(
            chat_id,
            f"✅ 已绑定：{label}\n回复「确认」我就创建这条规则（10 分钟内有效）。")
        return True

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
            self._drop_camera_ask(session_id)
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

        # 正在等用户回答「看哪路摄像头」时先吃掉这条消息，不进 LLM
        if await self._try_camera_answer(session_id, query, chat_id):
            return

        try:
            reply = await self._dispatch_fn(query, session_id, user_id)
            if reply:
                await self._send_message(chat_id, reply)
            await self._maybe_prompt_camera(session_id, chat_id)
        except Exception as e:
            logger.warning("飞书消息处理失败: %s", e)
            try:
                await self._send_message(chat_id, "抱歉，处理消息时出错了。")
            except Exception:
                pass

    def _is_duplicate(self, event_id: str) -> bool:
        """飞书重推去重：event_id 首见登记，TTL 内再见到即丢弃。懒清理过期项。"""
        now = time.monotonic()
        expired = [k for k, t in self._seen_events.items() if now - t > _EVENT_DEDUP_TTL]
        for k in expired:
            self._seen_events.pop(k, None)
        if event_id in self._seen_events:
            return True
        self._seen_events[event_id] = now
        return False

    async def _handle_chat(self, chat_key: str, query: str, meta: dict) -> None:
        """管道车道回调：调宿主 LLM 并把回复发回对应会话（含错误兜底）。

        chat_key 即 chat_id；meta 携带 open_id 组装 per-user 身份
        （feishu_<chat_id> / feishu_<open_id>，与旧直处理路径口径一致）。
        """
        chat_id = str(meta.get("chat_id") or chat_key)
        session_id = f"feishu_{chat_id}"
        user_id = f"feishu_{meta.get('open_id', '')}"
        # 摄像头绑定问答的拦截要覆盖这条路径：普通消息默认走管道（_handle_chat），
        # 只有斜杠命令走 _handle_and_reply，漏了这里等于问答对群聊/主路径完全失效
        if await self._try_camera_answer(session_id, query, chat_id):
            return
        try:
            reply = await self._dispatch_fn(query, session_id, user_id)
            if reply:
                await self._send_message(chat_id, reply)
            await self._maybe_prompt_camera(session_id, chat_id)
        except Exception as e:
            logger.warning("飞书消息处理失败: %s", e)
            try:
                await self._send_message(chat_id, "抱歉，处理消息时出错了。")
            except Exception:  # noqa: BLE001
                pass

    async def _notify_chat(self, chat_key: str, text: str) -> None:
        """管道提示回调（处理中/超时/积压丢弃）：直接发文本到对应会话。"""
        await self._send_message(chat_key, text)

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

    async def notify(self, message: str, level: str = "warning") -> None:
        """主动推送一条通知（alert_service Notifier 协议：async (message, level)）。

        推送目标：配置的 notify_chat_id 优先，否则取最近一次和机器人聊天的
        会话；两者都没有（部署后无人说过话）→ 丢弃并记 debug——没有可用的
        投递地址，静默即可，告警主流程不受影响（dispatch 侧已兜底单渠道失败）。
        """
        target = self._notify_chat_id or self._last_chat_id
        if not target:
            logger.debug("飞书通知无投递目标（未配置 notify_chat_id 且无历史会话）: %s",
                         message[:80])
            return
        prefix = {"warning": "⚠️ ", "error": "🚨 "}.get(level, "")
        await self._send_message(target, f"{prefix}{message}")
