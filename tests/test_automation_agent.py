"""Tests for AutomationAgent — the rewritten core module."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.automation_agent import AutomationAgent


class TestAutomationAgentInit:
    def test_defaults(self):
        agent = AutomationAgent()
        assert agent._eval_interval == 10.0
        assert agent._running is False
        assert agent._eval_count == 0

    def test_custom_interval(self):
        agent = AutomationAgent(eval_interval=5.0)
        assert agent._eval_interval == 5.0


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
        task1 = agent._task
        await agent.start()
        assert agent._task is task1
        await agent.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self):
        agent = AutomationAgent()
        await agent.stop()  # should not raise


class TestTriggerEvaluate:
    @pytest.mark.asyncio
    async def test_trigger_before_start_is_noop(self):
        agent = AutomationAgent()
        agent.trigger_evaluate()  # should not raise

    @pytest.mark.asyncio
    async def test_trigger_after_stop_is_noop(self):
        agent = AutomationAgent()
        await agent.start()
        await agent.stop()
        agent.trigger_evaluate()  # should not raise


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
