"""Dispatcher CancelledError 处理 + broadcasting status 测试。

验证打断（task.cancel()）时：
1. emit 了 Dialog.Finish(success=False)；
2. 调了 sink_manager.interrupt_all()；
3. CancelledError 被吞掉（task 正常结束，不向外抛）。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.dispatcher import Dispatcher
from app.schema.chat_schema import Event, Nlp
from app.services.session_store import SessionStore


def _make_dispatcher(sink_manager=None) -> Dispatcher:
    """构造一个 Dispatcher，依赖尽量用真实/轻量 mock（沿用现有 broadcast hook 测试的套路）。"""
    store = SessionStore()
    agent = MagicMock()
    camera = MagicMock()
    camera.get_state.return_value = {"action": "idle"}
    camera.list_cameras.return_value = []
    ha_catalog = MagicMock(return_value="")
    return Dispatcher(
        session_store=store,
        agent=agent,
        camera_manager=camera,
        ha_catalog_provider=ha_catalog,
        sink_manager=sink_manager,
    )


def _endless_stream():
    """mock run_agent_streaming 成一个永不产出、永久阻塞的 async generator（模拟长时间思考）。

    末尾的 `yield` 在 `while True` 之后不可达，仅用于让 gen 编译为 async generator
    function（缺 yield 会被当成普通 coroutine，无法被 `async for` 消费，且会立即抛错）。
    主轮的 `async for` 因此阻塞在内部 sleep 上，cancel 时在该 await 处抛 CancelledError。
    """
    async def gen(*a, **kw):
        while True:
            await asyncio.sleep(0.01)
        yield  # pragma: no cover - 不可达，仅为使 gen 成为 async generator
    return gen


class TestDispatcherCancel:
    @pytest.mark.asyncio
    async def test_cancel_emits_finish_and_interrupts(self):
        """cancel task 后 Dispatcher emit Finish(success=False) + interrupt_all。"""
        sink_manager = MagicMock()
        sink_manager.interrupt_all = AsyncMock()
        dispatcher = _make_dispatcher(sink_manager=sink_manager)

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_endless_stream()), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            event = Event.build_event(
                Nlp.Request(query="test"),
                request_id="req-1", session_id="sess-1",
            )
            task = asyncio.create_task(
                dispatcher.dispatch_stream(event, ws_send)
            )
            await asyncio.sleep(0.05)  # 让它开始思考
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # 不应该逃逸——Dispatcher 应吞掉

        # 验证 emit 了 Finish(success=False)
        sent = [c[0][0] for c in ws_send.call_args_list]
        finish_msgs = [m for m in sent
                       if m.get("header", {}).get("namespace") == "Dialog"
                       and m.get("header", {}).get("name") == "Finish"]
        assert len(finish_msgs) >= 1
        assert finish_msgs[-1]["payload"]["success"] is False

        # 验证调了 interrupt_all
        sink_manager.interrupt_all.assert_awaited()

    @pytest.mark.asyncio
    async def test_cancel_no_sink_manager_still_emits_finish(self):
        """无 sink_manager 时 cancel 仍 emit Finish（纯框架打断，无插件也工作）。"""
        dispatcher = _make_dispatcher(sink_manager=None)

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_endless_stream()), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            event = Event.build_event(
                Nlp.Request(query="test"),
                request_id="req-2", session_id="sess-2",
            )
            task = asyncio.create_task(
                dispatcher.dispatch_stream(event, ws_send)
            )
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        sent = [c[0][0] for c in ws_send.call_args_list]
        finish_msgs = [m for m in sent
                       if m.get("header", {}).get("namespace") == "Dialog"
                       and m.get("header", {}).get("name") == "Finish"]
        assert len(finish_msgs) >= 1
        assert finish_msgs[-1]["payload"]["success"] is False
