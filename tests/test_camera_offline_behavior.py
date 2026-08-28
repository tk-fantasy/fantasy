"""摄像头离线行为回归：MJPEG 占位图 / 自动化跳过离线路 / vision_chat 如实告知。

背景：摄像头拔掉后（1）MJPEG 一直挂断连前的最后画面，（2）视觉自动化兜底
循环继续用缓冲里的旧帧跑 VLM，（3）vision_chat 把旧帧当实时画面喂给模型
（AI 幻觉源）。三处消费方都必须尊重 camera_opened。
"""
from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.camera_stream import CameraStream, _OFFLINE_JPEG


# ---------------------------------------------------------------------------
# 1. MJPEG：离线超过宽限期 → NO SIGNAL 占位图；宽限期内继续显示缓存帧
# ---------------------------------------------------------------------------

def _make_stream(camera_opened: bool, hold: float) -> CameraStream:
    s = CameraStream.__new__(CameraStream)
    s._running = True
    s._lock = threading.Lock()
    s._latest_jpeg = b"stale-cached-frame"
    s._state = SimpleNamespace(camera_opened=camera_opened)
    s._OFFLINE_FRAME_HOLD_SECONDS = hold
    return s


class TestMjpegOfflinePlaceholder:
    def test_offline_beyond_grace_sends_placeholder(self):
        """离线且超过宽限 → 推 NO SIGNAL 占位图，不再发缓存帧。"""
        s = _make_stream(camera_opened=False, hold=0.0)
        gen = s.mjpeg_generator()
        first = next(gen)
        gen.close()
        assert _OFFLINE_JPEG in first
        assert b"stale-cached-frame" not in first

    def test_offline_within_grace_keeps_cached_frame(self):
        """离线但在宽限期内 → 继续显示缓存帧（瞬时掉帧不闪断的初衷）。"""
        s = _make_stream(camera_opened=False, hold=3600.0)
        gen = s.mjpeg_generator()
        first = next(gen)
        gen.close()
        assert b"stale-cached-frame" in first
        assert _OFFLINE_JPEG not in first

    def test_online_streams_real_frames(self):
        """在线 → 正常发帧，与离线逻辑无关。"""
        s = _make_stream(camera_opened=True, hold=0.0)
        gen = s.mjpeg_generator()
        first = next(gen)
        gen.close()
        assert b"stale-cached-frame" in first
        assert _OFFLINE_JPEG not in first


# ---------------------------------------------------------------------------
# 2. 自动化兜底：离线的路跳过视觉评估
# ---------------------------------------------------------------------------

class TestAutomationSkipsOfflineCameras:
    @pytest.mark.asyncio
    async def test_offline_camera_not_evaluated(self):
        from app.agents.automation_agent import AutomationAgent

        agent = AutomationAgent.__new__(AutomationAgent)
        agent._eval_running = False
        agent._eval_count = 0
        agent._automation_service = MagicMock(evaluate=AsyncMock())
        agent._camera_manager = SimpleNamespace(
            list_cameras=lambda: [{"id": "cam_online"}, {"id": "cam_offline"}],
            get_state=lambda cid: {"camera_opened": cid == "cam_online"},
            get_recent_frames=lambda cid, n: [object()],  # 离线路也有旧帧，但必须被跳过
        )
        await agent._run_evaluation_cycle()
        assert agent._automation_service.evaluate.await_count == 1
        assert agent._automation_service.evaluate.await_args.kwargs["camera_id"] == "cam_online"

    @pytest.mark.asyncio
    async def test_all_offline_no_evaluation(self):
        from app.agents.automation_agent import AutomationAgent

        agent = AutomationAgent.__new__(AutomationAgent)
        agent._eval_running = False
        agent._eval_count = 0
        agent._automation_service = MagicMock(evaluate=AsyncMock())
        agent._camera_manager = SimpleNamespace(
            list_cameras=lambda: [{"id": "cam1"}],
            get_state=lambda cid: {"camera_opened": False},
            get_recent_frames=lambda cid, n: [object()],
        )
        await agent._run_evaluation_cycle()
        agent._automation_service.evaluate.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. vision_chat：离线如实告知（即使缓冲有旧帧也不喂给模型）
# ---------------------------------------------------------------------------

def _register_vision_chat_with(deps):
    from app.tools import _register_vision_chat
    deps.mcp_client_manager.register_tool = MagicMock()
    _register_vision_chat(deps)
    for call in deps.mcp_client_manager.register_tool.call_args_list:
        tool = call.args[0] if call.args else call.kwargs.get("tool")
        if getattr(tool, "tool_name", None) == "vision_chat":
            return tool
    raise AssertionError("vision_chat not registered")


def _offline_deps(online: bool):
    return SimpleNamespace(
        mcp_client_manager=SimpleNamespace(),
        camera_manager=SimpleNamespace(
            _active_display_id="",
            list_cameras=lambda: [{"id": "cam1"}],
            get_state=lambda cid: {"camera_opened": online},
            get_recent_frames=lambda cid, n: [object()],  # 旧帧必须在离线时被拦住
            get_frame=lambda cid: object(),
        ),
        vision_client=SimpleNamespace(
            ask_about_frames=AsyncMock(return_value="画面里有一个人"),
            model="test-vlm",
        ),
    )


class TestVisionChatOfflineHonesty:
    @pytest.mark.asyncio
    async def test_offline_returns_honest_answer(self):
        tool = _register_vision_chat_with(_offline_deps(online=False))
        result = await tool.handler({"question": "画面里有人吗"}, None)
        assert result["has_frame"] is False
        assert result.get("camera_offline") is True
        assert "离线" in result["answer"]
        # 模型根本不该被调用
        # （deps.vision_client.ask_about_frames 是 AsyncMock，未 await）

    @pytest.mark.asyncio
    async def test_online_still_analyzes(self):
        deps = _offline_deps(online=True)
        tool = _register_vision_chat_with(deps)
        result = await tool.handler({"question": "画面里有人吗"}, None)
        assert result["has_frame"] is True
        assert result["answer"] == "画面里有一个人"
        deps.vision_client.ask_about_frames.assert_awaited_once()
