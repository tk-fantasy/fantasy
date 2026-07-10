"""Tests for Dispatcher with LangGraph Agent."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.dispatcher import Dispatcher
from app.schema.chat_schema import Event, Nlp, UI
from app.services.session_store import SessionStore


def _make_dispatcher() -> tuple[Dispatcher, MagicMock, MagicMock]:
    """创建测试用的 Dispatcher，使用 mock agent。"""
    store = SessionStore()

    # Mock LangGraph agent
    agent = MagicMock()

    # Mock camera
    camera = MagicMock()
    camera.get_state.return_value = {"action": "idle"}

    # Mock HA catalog provider
    ha_catalog = MagicMock(return_value="")

    dispatcher = Dispatcher(
        session_store=store,
        agent=agent,
        camera_stream=camera,
        ha_catalog_provider=ha_catalog,
    )
    return dispatcher, agent, store


class TestDispatcher:
    def test_dispatcher_creation(self):
        """测试 Dispatcher 可以正常创建。"""
        dispatcher, _, store = _make_dispatcher()
        assert dispatcher is not None
        assert dispatcher._session_store is store

    def test_dispatcher_has_stream_method(self):
        """测试 Dispatcher 有 dispatch_stream 方法。"""
        dispatcher, _, _ = _make_dispatcher()
        assert hasattr(dispatcher, "dispatch_stream")
        assert hasattr(dispatcher, "dispatch")


class TestUIStatus:
    """测试 UI.Status 阶段状态推送。"""

    def test_ui_status_schema(self):
        """测试 UI.Status schema 定义。"""
        status = UI.Status(phase="thinking")
        assert status.phase == "thinking"
        assert status.detail == ""

    def test_ui_status_with_detail(self):
        """测试 UI.Status 带 detail。"""
        status = UI.Status(phase="executing", detail="call_service")
        assert status.phase == "executing"
        assert status.detail == "call_service"

    def test_ui_status_phases(self):
        """测试所有阶段值。"""
        for phase in ["thinking", "executing", "retrying", "finalizing"]:
            status = UI.Status(phase=phase)
            assert status.phase == phase

    @pytest.mark.asyncio
    async def test_dispatch_stream_sends_status(self):
        """测试 dispatch_stream 发送状态推送。"""
        dispatcher, agent, store = _make_dispatcher()

        # Mock agent 返回空流
        async def mock_stream(*args, **kwargs):
            return
            yield  # make it an async generator

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            ws_send = AsyncMock()
            event = Event.build_event(
                Nlp.Request(query="test"),
                request_id="req-1",
                session_id="sess-1",
            )
            await dispatcher.dispatch_stream(event, ws_send)

            # 检查是否发送了 thinking 状态
            calls = ws_send.call_args_list
            status_calls = [
                c for c in calls
                if c[0][0].get("header", {}).get("name") == "Status"
                and c[0][0].get("header", {}).get("namespace") == "UI"
            ]
            assert len(status_calls) >= 1
            # 第一个应该是 thinking
            first_status = status_calls[0][0][0]["payload"]
            assert first_status["phase"] == "thinking"


def _tool_start_event(tool_name: str, args: dict, run_id: str = "r1"):
    return {"type": "tool_start", "tool_name": tool_name, "tool_args": args, "run_id": run_id}


def _tool_end_event(tool_name: str, result: str, *, error: bool, run_id: str = "r1"):
    return {"type": "tool_end", "tool_name": tool_name, "result": result, "error": error, "run_id": run_id}


class TestSilentFailureFallback:
    """验证 Q4/Q5 静默收尾兜底：重试轮空转时强制生成失败说明 + Finish 反映真实。"""

    @pytest.mark.asyncio
    async def test_silent_failure_forces_fallback_and_failed_finish(self):
        """主轮一个工具失败；重试轮模型空转（无工具、无文本）→ 应兜底失败说明 + Finish(success=False)。"""
        dispatcher, agent, store = _make_dispatcher()

        main_round = [
            _tool_start_event("call_service", {"domain": "cover"}, run_id="r1"),
            _tool_end_event("call_service", "Error: 400 Bad Request", error=True, run_id="r1"),
        ]
        retry_round: list[dict] = []  # 重试轮空转：hook 剔光、模型无文本

        rounds = [main_round, retry_round]

        async def mock_stream(*args, **kwargs):
            if rounds:
                for ev in rounds.pop(0):
                    yield ev

        with patch("app.agents.dispatcher.run_agent_streaming", side_effect=mock_stream), \
             patch.object(dispatcher._validator, "should_retry", return_value=False):
            event = Event.build_event(
                Nlp.Request(query="把窗帘设到200%"),
                request_id="req-silent",
                session_id="sess-silent",
            )
            instructions = await dispatcher.dispatch(event)

            # 兜底：必有 ToastStream 失败说明
            toast = [i for i in instructions if i.header.name == "ToastStream"]
            assert toast, "静默失败应兜底生成 ToastStream"
            stream_text = toast[0].payload.get("stream") if isinstance(toast[0].payload, dict) else toast[0].payload.stream
            assert "未能完成" in stream_text
            # Finish 反映真实：仍存在未解决失败 → success=False
            finish = [i for i in instructions if i.header.name == "Finish"]
            assert finish
            finish_success = finish[0].payload.get("success") if isinstance(finish[0].payload, dict) else finish[0].payload.success
            assert finish_success is False
            # 重试提示也应有（REST 现在补了 UI.Status retrying）
            statuses = [i for i in instructions if i.header.name == "Status"]
            assert any(
                (s.payload.get("phase") if isinstance(s.payload, dict) else s.payload.phase) == "retrying"
                for s in statuses
            )
