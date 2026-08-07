"""WS 循环打断行为测试。

验证：
1. 收到 interrupt 消息 → cancel current_task + interrupt_all
2. 发新消息时自动打断旧的
3. mode=aether 走 dispatch_stream
4. mode!=aether 走 route_inbound（不硬编码模式名）
5. 无集成平台时 interrupt 也不崩
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_container(dispatch_stream_fn=None, route_inbound_fn=None,
                    interrupt_all_fn=None, integration_enabled=True):
    """构造 mock container。"""
    container = MagicMock()
    container.dispatcher = MagicMock()
    container.dispatcher.dispatch_stream = dispatch_stream_fn or AsyncMock()

    if integration_enabled:
        container.integration_layer = MagicMock()
        container.integration_layer.route_inbound = route_inbound_fn or AsyncMock(
            return_value={"ok": True, "executed": "text"})
        container.integration_layer.sink_manager = MagicMock()
        container.integration_layer.sink_manager.interrupt_all = interrupt_all_fn or AsyncMock()
    else:
        container.integration_layer = None

    return container


class FakeWebSocket:
    """模拟 WebSocket，queue 驱动 receive_json。"""
    def __init__(self, messages):
        self._messages = list(messages)
        self.sent = []

    async def accept(self):
        pass

    async def receive_json(self):
        await asyncio.sleep(0)  # 模拟真实 I/O 让出事件循环（让并发 task 得以启动）
        if not self._messages:
            # 阻塞直到被 cancel
            await asyncio.sleep(100)
        return self._messages.pop(0)

    async def send_json(self, data):
        self.sent.append(data)


def test_interrupt_cancels_task_and_interrupts_sinks():
    """收到 interrupt → cancel current_task + interrupt_all。"""
    dispatch_started = asyncio.Event()

    async def slow_dispatch(event, ws_send, user_id=""):
        dispatch_started.set()
        await asyncio.sleep(100)  # 模拟长时间思考

    interrupt_calls = []
    async def interrupt_all():
        interrupt_calls.append(True)

    container = _make_container(
        dispatch_stream_fn=slow_dispatch,
        interrupt_all_fn=interrupt_all,
    )

    ws = FakeWebSocket([
        {"type": "chat", "query": "hello", "session_id": "s1"},
        {"type": "interrupt"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.wait_for(dispatch_started.wait(), timeout=2.0)
        await asyncio.sleep(0.1)  # 让 interrupt 处理
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    assert len(interrupt_calls) >= 1  # 调了 interrupt_all


def test_new_message_auto_interrupts_old():
    """发新消息 → 自动 cancel 旧 task。"""
    first_dispatch = asyncio.Event()
    second_calls = []

    call_count = [0]
    async def dispatch(event, ws_send, user_id=""):
        call_count[0] += 1
        if call_count[0] == 1:
            first_dispatch.set()
            await asyncio.sleep(100)
        else:
            second_calls.append(event.payload.get("query"))

    container = _make_container(dispatch_stream_fn=dispatch)

    ws = FakeWebSocket([
        {"type": "chat", "query": "first", "session_id": "s1"},
        {"type": "chat", "query": "second", "session_id": "s1"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.wait_for(first_dispatch.wait(), timeout=2.0)
        await asyncio.sleep(0.2)  # 让第二条消息处理
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    assert len(second_calls) == 1  # 第二条消息的 dispatch 被调了
    assert second_calls[0] == "second"


def test_non_aether_mode_routes_to_inbound():
    """mode != aether → 走 route_inbound（不硬编码模式名）。"""
    route_calls = []
    async def route_inbound(text, mode):
        route_calls.append((text, mode))
        return {"ok": True, "executed": text}

    container = _make_container(route_inbound_fn=route_inbound)

    ws = FakeWebSocket([
        {"type": "chat", "mode": "xiaoai_direct", "query": "播放音乐",
         "session_id": "s1"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())

    assert len(route_calls) == 1
    assert route_calls[0] == ("播放音乐", "xiaoai_direct")


def test_interrupt_with_no_integration_layer_still_works():
    """无集成平台时 interrupt 也不崩（纯框架打断）。"""
    container = _make_container(integration_enabled=False)

    async def slow_dispatch(event, ws_send, user_id=""):
        await asyncio.sleep(100)

    container.dispatcher.dispatch_stream = slow_dispatch

    ws = FakeWebSocket([
        {"type": "chat", "query": "hello", "session_id": "s1"},
        {"type": "interrupt"},
    ])

    from app.routes.ws_routes import _chat_loop

    async def go():
        task = asyncio.create_task(_chat_loop(ws, container, "u1"))
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.new_event_loop().run_until_complete(go())
    # 不崩就算通过
