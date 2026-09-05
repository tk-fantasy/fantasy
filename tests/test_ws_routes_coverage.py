"""Coverage tests for ws_routes.py — 聊天/文档助手 WebSocket 端点。

两层打法：
1. 直调内部协程（_cancel_current/_run_dispatch/_handle_direct/_receive_payload/
   _chat_loop/doc_chat_ws 断连分支），mock websocket 边界，验证行为契约；
2. 通过 TestClient websocket_connect 走完整端点（chat_ws / doc_chat_ws），
   patch app.main 的 _ws_verify_token/_ws_heartbeat 边界，断言真实收发的帧。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.routes import ws_routes


def _mock_ws():
    ws = MagicMock(spec=WebSocket)
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.receive_json = AsyncMock()
    return ws


def _mock_container():
    c = MagicMock()
    c.integration_layer = None
    return c


# ===================== 内部协程直调 =====================

class TestCancelCurrent:
    async def test_cancels_running_task_and_interrupts_sinks(self):
        """活跃 task 被 cancel + 所有 sink 被打断。"""
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)

        task = asyncio.create_task(slow())
        await started.wait()  # 确保 task 已在 sleep

        container = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = AsyncMock()

        await ws_routes._cancel_current(task, container)

        assert task.cancelled()
        container.integration_layer.sink_manager.interrupt_all.assert_awaited_once()

    async def test_skips_done_task(self):
        """task 已结束不再 cancel，但仍打断 sink（小爱可能还在念）。"""
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        container = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = AsyncMock()

        await ws_routes._cancel_current(task, container)
        container.integration_layer.sink_manager.interrupt_all.assert_awaited_once()

    async def test_no_integration_layer_is_safe(self):
        """integration_layer 为 None（未装配）时不抛异常。"""
        await ws_routes._cancel_current(None, _mock_container())


class TestRunDispatch:
    async def test_swallows_generic_exception(self):
        """dispatch_stream 逃逸的意外异常被记日志，不杀死 WS 循环。"""
        container = MagicMock()
        container.dispatcher.dispatch_stream = AsyncMock(side_effect=ValueError("boom"))
        with patch("app.routes.ws_routes.logger") as mock_logger:
            await ws_routes._run_dispatch(container, {"e": 1}, AsyncMock(), "u1")
        mock_logger.exception.assert_called_once()

    async def test_swallows_cancelled(self):
        """打断（CancelledError）静默消化。"""
        container = MagicMock()
        container.dispatcher.dispatch_stream = AsyncMock(
            side_effect=asyncio.CancelledError()
        )
        await ws_routes._run_dispatch(container, {"e": 1}, AsyncMock(), "u1")  # 不抛
        container.dispatcher.dispatch_stream.assert_awaited_once()


class TestHandleDirect:
    async def test_no_layer_sends_failure(self):
        """integration_layer 未装配 → Finish(success=False, 直通失败)。"""
        ws = _mock_ws()
        await ws_routes._handle_direct(ws, _mock_container(), {"query": "x"}, "r1", "u1")
        sent = ws.send_json.await_args.args[0]
        assert sent["payload"]["success"] is False
        assert sent["payload"]["message"] == "直通失败"
        assert sent["header"]["request_id"] == "r1"

    async def test_routes_to_inbound_router_ok(self):
        """通用模式路由到 inbound_router，成功回执。"""
        ws = _mock_ws()
        container = MagicMock()
        container.integration_layer.route_inbound = AsyncMock(
            return_value={"ok": True}
        )
        await ws_routes._handle_direct(
            ws, container, {"query": "开灯", "session_id": "s1"}, "r2", "u1"
        )
        container.integration_layer.route_inbound.assert_awaited_once_with("开灯", "")
        sent = ws.send_json.await_args.args[0]
        assert sent["payload"]["success"] is True
        assert sent["payload"]["message"] == "已转交处理"
        assert sent["header"]["session_id"] == "s1"

    async def test_routes_error_message_passthrough(self):
        ws = _mock_ws()
        container = MagicMock()
        container.integration_layer.route_inbound = AsyncMock(
            return_value={"ok": False, "error": "插件离线"}
        )
        await ws_routes._handle_direct(ws, container, {"query": "x"}, "r3", "u1")
        sent = ws.send_json.await_args.args[0]
        assert sent["payload"]["success"] is False
        assert sent["payload"]["message"] == "插件离线"

    async def test_exception_sends_generic_failure(self):
        ws = _mock_ws()
        container = MagicMock()
        container.integration_layer.route_inbound = AsyncMock(
            side_effect=RuntimeError("x")
        )
        await ws_routes._handle_direct(ws, container, {"query": "x"}, "r4", "u1")
        sent = ws.send_json.await_args.args[0]
        assert sent["payload"]["success"] is False
        assert sent["payload"]["message"] == "直通执行失败"


class TestReceivePayload:
    async def test_malformed_json_returns_none(self):
        ws = _mock_ws()
        ws.receive_json = AsyncMock(side_effect=ValueError("not json"))
        assert await ws_routes._receive_payload(ws) is None

    async def test_non_object_frame_returns_none(self):
        ws = _mock_ws()
        ws.receive_json = AsyncMock(return_value=[1, 2])
        assert await ws_routes._receive_payload(ws) is None

    async def test_valid_frame_passthrough(self):
        ws = _mock_ws()
        ws.receive_json = AsyncMock(return_value={"type": "pong"})
        assert await ws_routes._receive_payload(ws) == {"type": "pong"}


class TestChatLoop:
    async def test_ignores_junk_handles_interrupt_then_disconnect(self):
        """畸形帧/pong 跳过；interrupt 打断 sink；断开退出循环。"""
        ws = _mock_ws()
        ws.receive_json = AsyncMock(side_effect=[
            None,                    # 畸形帧 → 跳过
            {"type": "pong"},        # 心跳 → 跳过
            {"type": "interrupt"},   # 打断
            WebSocketDisconnect(),   # 断开
        ])
        container = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = AsyncMock()

        with pytest.raises(WebSocketDisconnect):
            await ws_routes._chat_loop(ws, container, "u1")
        container.integration_layer.sink_manager.interrupt_all.assert_awaited_once()

    async def test_chat_frames_spawn_dispatch_and_direct_tasks(self):
        """aether 模式走 dispatch_stream，其他模式走 inbound_router 直通。"""
        ws = _mock_ws()
        frames = [
            {"type": "chat", "query": "开灯", "mode": "aether",
             "request_id": "r-a", "session_id": "s1"},
            {"type": "chat", "query": "暂停", "mode": "xiai_rock", "request_id": "r-b"},
        ]

        async def receive():
            # 真实挂起点：让上一帧创建的后台 task 先跑完（否则被新帧自动打断）
            await asyncio.sleep(0)
            if not frames:
                raise WebSocketDisconnect()
            return frames.pop(0)

        ws.receive_json = receive
        container = MagicMock()
        # 新 chat 帧自动打断旧回合（类 ChatGPT 体验）
        container.integration_layer.sink_manager.interrupt_all = AsyncMock()
        dispatch = AsyncMock()
        container.dispatcher.dispatch_stream = dispatch
        container.integration_layer.route_inbound = AsyncMock(return_value={"ok": True})

        with pytest.raises(WebSocketDisconnect):
            await ws_routes._chat_loop(ws, container, "user-9")
        await asyncio.sleep(0.01)  # 让后台 task 跑完

        dispatch.assert_awaited_once()
        event = dispatch.await_args.args[0]
        assert event.payload["query"] == "开灯"
        assert dispatch.await_args.kwargs["user_id"] == "user-9"
        assert event.header.request_id == "r-a"
        container.integration_layer.route_inbound.assert_awaited_once_with("暂停", "xiai_rock")
        assert ws.send_json.await_count == 1  # 直通回执发出


# ===================== 完整端点（TestClient） =====================

@pytest.fixture()
def ws_env(monkeypatch):
    """patch 认证/心跳边界 + 容器，返回 (TestClient, container)。"""
    import app.container as container_mod
    import app.main as main_mod

    container = _mock_container()
    monkeypatch.setattr(container_mod, "_container", container)

    async def fake_verify(websocket):
        return "u1"

    async def fake_heartbeat(websocket):
        await asyncio.Event().wait()

    monkeypatch.setattr(main_mod, "_ws_verify_token", fake_verify)
    monkeypatch.setattr(main_mod, "_ws_heartbeat", fake_heartbeat)

    app = FastAPI()
    app.include_router(ws_routes.router)
    return TestClient(app), container


class TestChatWsEndpoint:
    def test_full_round_token_and_direct_mode(self, ws_env):
        """chat 帧收到 dispatch 推的 token；非 aether 模式收到直通 Finish。"""
        client, container = ws_env

        async def fake_dispatch(event, send, user_id):
            assert user_id == "u1"
            await send({"type": "token", "content": "你好呀"})

        container.dispatcher.dispatch_stream = fake_dispatch
        container.integration_layer = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = AsyncMock()
        container.integration_layer.route_inbound = AsyncMock(return_value={"ok": True})

        from app.core import ws_registry
        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "chat", "query": "在吗", "mode": "aether",
                          "request_id": "r-1", "session_id": "s1"})
            frame1 = ws.receive_json()
            assert frame1 == {"type": "token", "content": "你好呀"}

            ws.send_json({"type": "chat", "query": "关灯", "mode": "voice",
                          "request_id": "r-2"})
            frame2 = ws.receive_json()
            assert frame2["payload"]["success"] is True
            assert frame2["payload"]["message"] == "已转交处理"
            assert frame2["header"]["request_id"] == "r-2"

            # 连接期间注册在在线表
            assert any(ws_reg for ws_reg in ws_registry._sockets.get("u1", set()))

        # 断开后注销
        assert ws_registry._sockets.get("u1") in (None, set())

    def test_interrupt_cancels_running_turn(self, ws_env):
        """interrupt 帧取消进行中的回合并打断 sink。"""
        client, container = ws_env

        async def slow_dispatch(event, send, user_id):
            await asyncio.sleep(30)

        container.dispatcher.dispatch_stream = slow_dispatch
        container.integration_layer = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = AsyncMock()

        with client.websocket_connect("/ws/chat") as ws:
            ws.send_json({"type": "chat", "query": "长任务", "mode": "aether"})
            ws.send_json({"type": "interrupt"})

        container.integration_layer.sink_manager.interrupt_all.assert_awaited()


class TestDocChatWsEndpoint:
    @staticmethod
    def _ready_container(container, chunks=None, create_error=None):
        rag = MagicMock()
        rag.is_ready = True
        rag.search = AsyncMock(return_value="检索到的上下文")
        fake_client = MagicMock()
        if create_error is not None:
            fake_client.chat.completions.create = MagicMock(
                side_effect=create_error
            )
        else:
            fake_client.chat.completions.create = MagicMock(
                return_value=[
                    SimpleNamespace(
                        choices=[SimpleNamespace(delta=SimpleNamespace(content=t))]
                    )
                    for t in (chunks or ["答案"])
                ]
            )
        rag.build_llm_client = AsyncMock(return_value=(fake_client, "test-model"))
        container.rag_service = rag
        return rag

    def test_stream_tokens_then_done(self, ws_env):
        """正常回合：token 帧流式推送，结束时发 done 帧。"""
        client, container = ws_env
        rag = self._ready_container(container, chunks=["文", "档"])

        with client.websocket_connect("/ws/doc/chat") as ws:
            ws.send_json({"query": "什么是Aether", "request_id": "d-1"})
            assert ws.receive_json() == {"type": "token", "content": "文"}
            assert ws.receive_json() == {"type": "token", "content": "档"}
            assert ws.receive_json() == {"type": "done"}

        rag.search.assert_awaited_once_with("什么是Aether")

    def test_llm_error_frame(self, ws_env):
        """上游 LLM 异常：客户端只收固定文案，不泄露内部信息。"""
        client, container = ws_env
        self._ready_container(container, create_error=RuntimeError("http://secret"))

        with client.websocket_connect("/ws/doc/chat") as ws:
            ws.send_json({"query": "问题"})
            frame = ws.receive_json()
            assert frame["type"] == "error"
            assert "模型调用失败" in frame["message"]
            assert "secret" not in json.dumps(frame)

    def test_rag_not_ready(self, ws_env):
        """索引未就绪：直接回 error 帧，不搜索。"""
        client, container = ws_env
        rag = MagicMock()
        rag.is_ready = False
        rag.search = AsyncMock()
        container.rag_service = rag

        with client.websocket_connect("/ws/doc/chat") as ws:
            ws.send_json({"query": "问题"})
            frame = ws.receive_json()
            assert frame == {"type": "error", "message": "RAG 索引未就绪，请稍后刷新页面重试"}

        rag.search.assert_not_awaited()

    def test_pong_and_empty_query_ignored(self, ws_env):
        """pong / 畸形帧 / 空 query 不触发检索。"""
        client, container = ws_env
        rag = self._ready_container(container)

        with client.websocket_connect("/ws/doc/chat") as ws:
            ws.send_json({"type": "pong"})
            ws.send_json([1, 2])  # 非对象帧 → 忽略
            ws.send_json({"query": "   "})

        rag.search.assert_not_awaited()


class TestDocChatDisconnectBranch:
    @staticmethod
    def _stoppable_stream(texts, stop_probe_delay: float = 0.3):
        """带 close() 的流：第二次取值前延迟，保证读端先置停止标志。close 永远失败。"""
        import time as _time

        items = [
            SimpleNamespace(
                choices=[SimpleNamespace(delta=SimpleNamespace(content=t))]
            )
            for t in texts
        ]

        class _Stream:
            def __init__(self):
                self._items = list(items)
                self._yielded_once = False

            def __iter__(self):
                return self

            def __next__(self):
                if not self._items:
                    raise StopIteration
                if self._yielded_once:
                    _time.sleep(stop_probe_delay)  # 给读端时间先置 stream_stop
                self._yielded_once = True
                return self._items.pop(0)

            def close(self):
                raise RuntimeError("close failed")  # 覆盖 except 分支

        return _Stream()

    async def test_runtime_error_mid_stream_stops_consuming(self, monkeypatch):
        """发送中连接坏掉（RuntimeError）：置停止标志、解阻塞、退出不抛。"""
        import app.container as container_mod
        import app.main as main_mod

        ws = _mock_ws()
        ws.receive_json = AsyncMock(side_effect=[
            {"query": "问题"},     # 唯一一条业务消息
            WebSocketDisconnect(),
        ])
        ws.send_json = AsyncMock(side_effect=RuntimeError("connection closed"))

        container = MagicMock()
        monkeypatch.setattr(container_mod, "_container", container)

        async def fake_verify(websocket):
            return "u1"

        async def fake_heartbeat(websocket):
            await asyncio.Event().wait()

        monkeypatch.setattr(main_mod, "_ws_verify_token", fake_verify)
        monkeypatch.setattr(main_mod, "_ws_heartbeat", fake_heartbeat)

        rag = MagicMock()
        rag.is_ready = True
        rag.search = AsyncMock(return_value="ctx")
        fake_client = MagicMock()
        fake_client.chat.completions.create = MagicMock(
            return_value=self._stoppable_stream(["长答案", "尾段"])
        )
        rag.build_llm_client = AsyncMock(return_value=(fake_client, "m"))
        container.rag_service = rag

        # 不抛异常即通过；token 帧发送失败后循环终止
        await ws_routes.doc_chat_ws(ws)
        rag.search.assert_awaited_once_with("问题")

        # 等后台消费线程走完 stop→close 分支，避免悬挂到后续测试
        await asyncio.sleep(0.4)