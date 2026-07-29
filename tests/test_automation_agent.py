"""Tests for AutomationAgent — dhash 事件触发 + 定时器兜底核心模块。

覆盖 P0 重写后的新 API：
- 节流闸（trigger_evaluate ≥ min_trigger_interval，防 0-result 规则被 300/min 轰炸）
- 定时器兜底（_silent_tick_loop + 热切换 setter）
- 并发保护（_eval_running 丢弃重叠评估）
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.automation_agent import AutomationAgent


class TestAutomationAgentInit:
    def test_defaults(self):
        agent = AutomationAgent()
        assert agent._min_trigger_interval == 3.0
        assert agent._silent_enabled is True
        assert agent._silent_interval == 60.0
        assert agent._running is False
        assert agent._eval_count == 0
        assert agent._last_trigger_at == 0.0

    def test_custom_params(self):
        agent = AutomationAgent(
            min_trigger_interval=1.5,
            silent_eval_enabled=False,
            silent_eval_interval=30.0,
        )
        assert agent._min_trigger_interval == 1.5
        assert agent._silent_enabled is False
        assert agent._silent_interval == 30.0

    def test_silent_interval_clamped_to_min_5s(self):
        agent = AutomationAgent(silent_eval_interval=1.0)
        assert agent._silent_interval == 5.0  # max(5.0, 1.0)

    def test_trigger_interval_clamped_to_min_0_5s(self):
        agent = AutomationAgent(min_trigger_interval=0.1)
        assert agent._min_trigger_interval == 0.5  # max(0.5, 0.1)


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


class TestTriggerEvaluateThrottle:
    @pytest.mark.asyncio
    async def test_trigger_before_start_is_noop(self):
        agent = AutomationAgent()
        agent.trigger_evaluate()  # 不抛

    @pytest.mark.asyncio
    async def test_trigger_after_stop_is_noop(self):
        agent = AutomationAgent()
        await agent.start()
        await agent.stop()
        agent.trigger_evaluate()  # 不抛

    @pytest.mark.asyncio
    async def test_throttle_drops_rapid_repeats(self):
        """关键测试：3s 节流闸——连续两次 trigger 只评估一次。

        这是防 0-result 规则被连续运动 300/min 轰炸的核心：冷却只在
        result==1 后武装，挡不住一直返回 0 的规则，故靠 trigger 节流兜底。
        """
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(automation_service=svc, min_trigger_interval=3.0)
        await agent.start()
        try:
            agent.trigger_evaluate()
            agent.trigger_evaluate()  # 节流窗口内，应丢弃
            await asyncio.sleep(0.05)  # 让 call_soon_threadsafe 调度的协程跑完
            assert agent._eval_count == 1  # 只评估一次
            assert svc.evaluate.await_count == 1
        finally:
            await agent.stop()

    @pytest.mark.asyncio
    async def test_trigger_after_interval_passes(self):
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        # 用极小节流间隔加速测试
        agent = AutomationAgent(automation_service=svc, min_trigger_interval=0.5)
        await agent.start()
        try:
            agent.trigger_evaluate()
            await asyncio.sleep(0.05)
            assert agent._eval_count == 1
            await asyncio.sleep(0.7)  # 等节流窗口过去（>0.5s，留足余量防 flaky）
            agent.trigger_evaluate()
            await asyncio.sleep(0.05)
            assert agent._eval_count == 2
        finally:
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
        agent = AutomationAgent(automation_service=svc)
        await agent._run_evaluation_cycle()
        assert agent._eval_count == 1
        svc.evaluate.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_service_no_crash(self):
        agent = AutomationAgent(automation_service=None, camera_stream=None)
        await agent._run_evaluation_cycle()
        assert agent._eval_count == 1

    @pytest.mark.asyncio
    async def test_gets_frames_from_camera(self):
        camera = MagicMock()
        camera.get_recent_frames.return_value = [[1, 2], [3, 4]]
        svc = MagicMock()
        svc.evaluate = AsyncMock()
        agent = AutomationAgent(automation_service=svc, camera_stream=camera)
        await agent._run_evaluation_cycle()
        svc.evaluate.assert_called_once_with(frames=[[1, 2], [3, 4]])

    @pytest.mark.asyncio
    async def test_concurrent_guard_drops_overlap(self):
        """并发保护：评估进行中时，第二次调用直接丢弃（不计数）。"""
        svc = MagicMock()
        slow_eval = asyncio.Event()

        async def slow(*a, **kw):
            await slow_eval.wait()

        svc.evaluate = AsyncMock(side_effect=slow)
        agent = AutomationAgent(automation_service=svc)
        # 启动第一次评估（挂起在 slow 上，_eval_running 保持 True）
        t1 = asyncio.create_task(agent._run_evaluation_cycle())
        await asyncio.sleep(0.02)  # 让 t1 设 _eval_running=True
        # 第二次应被并发保护丢弃
        await agent._run_evaluation_cycle()
        assert agent._eval_count == 1  # 只第一次计数
        # 放行第一次
        slow_eval.set()
        await t1
