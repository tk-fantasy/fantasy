"""Tests for AutomationAgent — 视觉/非视觉双静默兜底核心模块。

多路模式下 dhash 运动事件由 CameraManager._on_automation_trigger 自闭环驱动
(只评 vision 规则,见 test_camera_manager.py),本 agent 负责:
- 视觉兜底(_silent_tick_loop + 热切换 setter,遍历各路只评 vision)
- 非视觉兜底(_nonvision_tick_loop,无帧只评 time/weather,与摄像头解耦)
- 并发保护(_eval_running / _nonvision_eval_running 各自丢弃重叠评估)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.automation_agent import AutomationAgent


class TestAutomationAgentInit:
    def test_defaults(self):
        agent = AutomationAgent()
        assert agent._silent_enabled is True
        assert agent._silent_interval == 60.0
        assert agent._nonvision_enabled is True
        assert agent._nonvision_interval == 30.0
        assert agent._running is False
        assert agent._eval_count == 0
        assert agent._nonvision_eval_count == 0

    def test_custom_params(self):
        agent = AutomationAgent(
            silent_eval_enabled=False,
            silent_eval_interval=30.0,
            nonvision_silent_enabled=False,
            nonvision_silent_interval=10.0,
        )
        assert agent._silent_enabled is False
        assert agent._silent_interval == 30.0
        assert agent._nonvision_enabled is False
        assert agent._nonvision_interval == 10.0

    def test_silent_interval_clamped_to_min_5s(self):
        agent = AutomationAgent(silent_eval_interval=1.0, nonvision_silent_interval=1.0)
        assert agent._silent_interval == 5.0  # max(5.0, 1.0)
        assert agent._nonvision_interval == 5.0


class TestAutomationAgentStartStop:
    @pytest.mark.asyncio
    async def test_start_sets_running(self):
        agent = AutomationAgent()
        await agent.start()
        assert agent._running is True
        assert agent._loop is not None
        await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running(self):
        agent = AutomationAgent()
        await agent.start()
        await agent.stop()
        assert agent._running is False

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self):
        agent = AutomationAgent()
        await agent.start()
        task1 = agent._silent_task
        await agent.start()  # 第二次应直接 return
        assert agent._silent_task is task1  # 不重建
        await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        agent = AutomationAgent()
        await agent.stop()  # 不抛

    @pytest.mark.asyncio
    async def test_start_with_silent_disabled_no_tick(self):
        agent = AutomationAgent(silent_eval_enabled=False)
        await agent.start()
        assert agent._silent_task is None  # 关闭时不启 tick
        await agent.stop()


class TestSilentTick:
    @pytest.mark.asyncio
    async def test_silent_tick_evaluates_on_interval(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(
            automation_service=svc, silent_eval_enabled=True, silent_eval_interval=5.0
        )
        agent._silent_interval = 0.2  # 绕过 5s 下限加速测试（下限 clamp 另有专项测试）
        await agent.start()
        try:
            await asyncio.sleep(0.5)  # 至少一个 tick
            assert agent._eval_count >= 1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_silent_disabled_no_tick(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(automation_service=svc, silent_eval_enabled=False)
        await agent.start()
        try:
            await asyncio.sleep(0.3)
            assert agent._eval_count == 0
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_silent_enabled_toggles_tick(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(
            automation_service=svc, silent_eval_enabled=False, silent_eval_interval=5.0
        )
        agent._silent_interval = 0.2  # 绕过 5s 下限加速测试
        await agent.start()
        try:
            assert agent._silent_task is None
            agent.set_silent_enabled(True)
            await asyncio.sleep(0.05)  # 让 call_soon_threadsafe 调度 _apply_silent_enabled
            assert agent._silent_task is not None
            await asyncio.sleep(0.35)
            assert agent._eval_count >= 1
            agent.set_silent_enabled(False)
            await asyncio.sleep(0.05)
            assert agent._silent_task is None
        finally:
            await agent.stop()


class TestRunEvaluationCycle:
    @pytest.mark.asyncio
    async def test_increments_eval_count(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        mgr = MagicMock()
        mgr.list_cameras.return_value = [{"id": "cam_x"}]
        mgr.get_recent_frames = MagicMock(return_value=[[1, 2]])
        agent = AutomationAgent(automation_service=svc, camera_manager=mgr)
        await agent._run_evaluation_cycle()
        assert agent._eval_count == 1
        svc.evaluate.assert_called_once_with(
            frames=[[1, 2]], camera_id="cam_x", rule_types=("vision",)
        )

    @pytest.mark.asyncio
    async def test_no_service_no_crash(self):
        agent = AutomationAgent(automation_service=None)
        await agent._run_evaluation_cycle()
        assert agent._eval_count == 1

    @pytest.mark.asyncio
    async def test_gets_frames_from_camera(self):
        mgr = MagicMock()
        mgr.list_cameras.return_value = [{"id": "cam_x"}]
        mgr.get_recent_frames = MagicMock(return_value=[[1, 2], [3, 4]])
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(automation_service=svc, camera_manager=mgr)
        await agent._run_evaluation_cycle()
        svc.evaluate.assert_called_once_with(
            frames=[[1, 2], [3, 4]], camera_id="cam_x", rule_types=("vision",)
        )

    @pytest.mark.asyncio
    async def test_concurrent_guard_drops_overlap(self):
        """并发保护：评估进行中时，第二次调用直接丢弃（不计数）。"""
        svc = MagicMock()
        slow_eval = asyncio.Event()

        async def slow(*a, **kw):
            await slow_eval.wait()

        svc.evaluate = AsyncMock(side_effect=slow)
        mgr = MagicMock()
        mgr.list_cameras.return_value = [{"id": "cam_x"}]
        mgr.get_recent_frames = MagicMock(return_value=[[1, 2]])
        agent = AutomationAgent(automation_service=svc, camera_manager=mgr)
        # 启动第一次评估（挂起在 slow 上，_eval_running 保持 True）
        t1 = asyncio.create_task(agent._run_evaluation_cycle())
        await asyncio.sleep(0.02)  # 让 t1 设 _eval_running=True
        # 第二次应被并发保护丢弃
        await agent._run_evaluation_cycle()
        assert agent._eval_count == 1  # 只第一次计数
        # 放行第一次
        slow_eval.set()
        await t1


class TestNonvisionTick:
    """非视觉兜底循环:无帧评估 time/weather,与摄像头完全解耦。"""

    @pytest.mark.asyncio
    async def test_nonvision_tick_evaluates_on_interval(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(
            automation_service=svc, nonvision_silent_enabled=True, nonvision_silent_interval=5.0
        )
        agent._nonvision_interval = 0.2  # 绕过 5s 下限加速测试
        await agent.start()
        try:
            await asyncio.sleep(0.5)  # 至少一个 tick
            assert agent._nonvision_eval_count >= 1
            # 无帧 + camera_id 空串 + 只评 time/weather
            svc.evaluate.assert_called_with(
                frames=None, camera_id="", rule_types=("time", "weather")
            )
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_nonvision_disabled_no_tick(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(automation_service=svc, nonvision_silent_enabled=False)
        await agent.start()
        try:
            await asyncio.sleep(0.3)
            assert agent._nonvision_eval_count == 0
            assert agent._nonvision_task is None
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_nonvision_no_camera_manager_still_evaluates(self):
        """非视觉循环不依赖 camera_manager:无 manager 也照常评估。"""
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(automation_service=svc, camera_manager=None)
        await agent._run_nonvision_cycle()
        assert agent._nonvision_eval_count == 1
        svc.evaluate.assert_called_once_with(
            frames=None, camera_id="", rule_types=("time", "weather")
        )

    @pytest.mark.asyncio
    async def test_nonvision_tick_independent_of_vision_tick(self):
        """视觉兜底关闭时,非视觉循环照常跑(两循环互不依赖)。"""
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(
            automation_service=svc,
            silent_eval_enabled=False,
            nonvision_silent_enabled=True,
            nonvision_silent_interval=5.0,
        )
        agent._nonvision_interval = 0.2
        await agent.start()
        try:
            await asyncio.sleep(0.5)
            assert agent._silent_task is None
            assert agent._nonvision_eval_count >= 1
            assert agent._eval_count == 0
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_set_nonvision_enabled_toggles_tick(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(
            automation_service=svc, nonvision_silent_enabled=False, nonvision_silent_interval=5.0
        )
        agent._nonvision_interval = 0.2
        await agent.start()
        try:
            assert agent._nonvision_task is None
            agent.set_nonvision_silent_enabled(True)
            await asyncio.sleep(0.05)
            assert agent._nonvision_task is not None
            await asyncio.sleep(0.35)
            assert agent._nonvision_eval_count >= 1
            agent.set_nonvision_silent_enabled(False)
            await asyncio.sleep(0.05)
            assert agent._nonvision_task is None
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_nonvision_concurrent_guard_drops_overlap(self):
        """非视觉并发保护:评估进行中时,第二次 tick 直接丢弃(不计数)。"""
        svc = MagicMock()
        slow_eval = asyncio.Event()

        async def slow(*a, **kw):
            await slow_eval.wait()

        svc.evaluate = AsyncMock(side_effect=slow)
        agent = AutomationAgent(automation_service=svc)
        t1 = asyncio.create_task(agent._run_nonvision_cycle())
        await asyncio.sleep(0.02)
        await agent._run_nonvision_cycle()
        assert agent._nonvision_eval_count == 1
        slow_eval.set()
        await t1
