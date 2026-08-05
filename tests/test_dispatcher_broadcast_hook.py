"""Dispatcher 广播钩子测试：final_content 产出后广播到 sink_manager。

聚焦验证钩子被触发且向后兼容（不传 sink_manager 不崩）。
完整 e2e（真实 spawn 插件）见 test_integration_e2e.py。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.dispatcher import Dispatcher
from app.schema.chat_schema import Event, Nlp
from app.services.session_store import SessionStore


def _make_dispatcher(sink_manager=None) -> Dispatcher:
    store = SessionStore()
    agent = MagicMock()
    camera = MagicMock()
    camera.get_state.return_value = {"action": "idle"}
    ha_catalog = MagicMock(return_value="")
    return Dispatcher(
        session_store=store, agent=agent, camera_stream=camera,
        ha_catalog_provider=ha_catalog, sink_manager=sink_manager,
    )


def _token_stream(text: str):
    """构造一个只产出 token 的 mock stream。"""
    async def gen(*a, **kw):
        yield {"type": "token", "content": text}
    return gen


class TestBroadcastHook:
    @pytest.mark.asyncio
    async def test_final_content_triggers_broadcast(self):
        sink_manager = MagicMock()
        sink_manager.broadcast = AsyncMock()
        dispatcher = _make_dispatcher(sink_manager=sink_manager)

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_token_stream("床头灯已打开")), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            event = Event.build_event(
                Nlp.Request(query="打开床头灯"),
                request_id="req-1", session_id="sess-1",
            )
            await dispatcher.dispatch_stream(event, ws_send)

        sink_manager.broadcast.assert_awaited_once()
        args = sink_manager.broadcast.call_args
        # 第一个位置参数是 final_content
        assert "床头灯已打开" in str(args.args[0])
        # 第二个是 request_id
        assert args.args[1] == "req-1"

    @pytest.mark.asyncio
    async def test_no_sink_manager_does_not_crash(self):
        """未注入 sink_manager 时正常工作（向后兼容）。"""
        dispatcher = _make_dispatcher(sink_manager=None)
        assert dispatcher._sink_manager is None

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_token_stream("你好")), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            event = Event.build_event(
                Nlp.Request(query="你好"),
                request_id="req-2", session_id="sess-2",
            )
            # 不应抛异常
            await dispatcher.dispatch_stream(event, ws_send)

    @pytest.mark.asyncio
    async def test_broadcast_failure_does_not_break_main_flow(self):
        """sink_manager 广播抛异常时，主流程（Finish）仍正常完成。"""
        sink_manager = MagicMock()
        sink_manager.broadcast = AsyncMock(side_effect=RuntimeError("sink 挂了"))
        dispatcher = _make_dispatcher(sink_manager=sink_manager)

        with patch("app.agents.dispatcher.run_agent_streaming",
                   side_effect=_token_stream("回复")), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            event = Event.build_event(
                Nlp.Request(query="测试"),
                request_id="req-3", session_id="sess-3",
            )
            await dispatcher.dispatch_stream(event, ws_send)
            # 验证 Finish 仍发出来了
            finish_calls = [
                c for c in ws_send.call_args_list
                if c[0][0].get("header", {}).get("name") == "Finish"
            ]
            assert len(finish_calls) >= 1
