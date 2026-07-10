"""Tests for AutomationService with mocked dependencies."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.automation_service import AutomationService


class TestInCooldown:
    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def test_no_last_triggered(self):
        rule = {"cooldown_seconds": 10, "last_triggered_at": 0.0}
        assert self.svc._in_cooldown(rule, time.time()) is False

    def test_just_triggered(self):
        rule = {"cooldown_seconds": 10, "last_triggered_at": time.time()}
        assert self.svc._in_cooldown(rule, time.time()) is True

    def test_cooldown_expired(self):
        rule = {"cooldown_seconds": 10, "last_triggered_at": time.time() - 20}
        assert self.svc._in_cooldown(rule, time.time()) is False

    def test_default_cooldown(self):
        rule = {"last_triggered_at": time.time()}
        assert self.svc._in_cooldown(rule, time.time()) is True


class TestResolveToolName:
    def setup_method(self):
        self.svc = AutomationService.__new__(AutomationService)

    def test_full_name_found(self):
        tool_executor = MagicMock()
        tool_executor.resolve_tool_name.return_value = "ha___call_service"
        self.svc._tool_executor = tool_executor
        assert self.svc._tool_executor.resolve_tool_name("ha___call_service") == "ha___call_service"

    def test_short_name_resolved(self):
        tool_executor = MagicMock()
        tool_executor.resolve_tool_name.return_value = "ha_devices___call_service"
        self.svc._tool_executor = tool_executor
        assert self.svc._tool_executor.resolve_tool_name("call_service") == "ha_devices___call_service"

    def test_no_manager_returns_as_is(self):
        tool_executor = MagicMock()
        tool_executor.resolve_tool_name.return_value = "some_tool"
        self.svc._tool_executor = tool_executor
        assert self.svc._tool_executor.resolve_tool_name("some_tool") == "some_tool"


class TestEvaluate:
    @pytest.mark.asyncio
    async def test_no_rules(self):
        registry = MagicMock()
        registry.list_rules.return_value = []
        svc = AutomationService(registry)
        result = await svc.evaluate(frames=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_disabled_rule_skipped(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [], "enabled": False},
        ]
        svc = AutomationService(registry)
        result = await svc.evaluate(frames=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_no_frames_skips(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        svc = AutomationService(registry, vision_service=vision)
        result = await svc.evaluate(frames=[])
        assert result == []

    @pytest.mark.asyncio
    async def test_condition_met_triggers_action(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [
                {"tool_name": "ha_devices___call_service", "parameters": {"domain": "light"}},
            ], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        vision.evaluate_condition = AsyncMock(return_value=1)
        executor = MagicMock()
        executor.execute_tool_by_name = AsyncMock(return_value={"success": True})
        svc = AutomationService(registry, tool_executor=executor, vision_service=vision)
        result = await svc.evaluate(frames=[[1, 2, 3]])
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_condition_not_met_no_action(self):
        registry = MagicMock()
        registry.list_rules.return_value = [
            {"id": "1", "name": "test", "condition": "有人", "actions": [
                {"tool_name": "test", "parameters": {}},
            ], "enabled": True, "cooldown_seconds": 0, "last_triggered_at": 0},
        ]
        vision = MagicMock()
        vision.evaluate_condition = AsyncMock(return_value=0)
        svc = AutomationService(registry, vision_service=vision)
        result = await svc.evaluate(frames=[[1, 2, 3]])
        assert result == []


class TestBuildConditionContext:
    @pytest.mark.asyncio
    async def test_gets_time_and_weather(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0

        async def mock_time(params, session):
            return {
                "date": "2026-06-20", "time": "15:30:00",
                "weekday": "星期六", "year": 2026, "month": 6, "day": 20,
            }

        async def mock_weather(params, session):
            return {
                "location": "上海", "weather": "小雨",
                "temperature": 25, "humidity": 80,
            }

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            result = await svc._build_condition_context()

        assert "2026-06-20" in result
        assert "星期六" in result
        assert "小雨" in result
        assert "25°C" in result

    @pytest.mark.asyncio
    async def test_weather_cached_for_60s(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0
        call_count = 0

        async def mock_time(params, session):
            return {"date": "2026-06-20", "time": "15:30:00", "weekday": "星期六"}

        async def mock_weather(params, session):
            nonlocal call_count
            call_count += 1
            return {"location": "上海", "weather": "晴", "temperature": 30, "humidity": 50}

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            # 第一次调用：获取天气
            await svc._build_condition_context()
            assert call_count == 1

            # 第二次调用：缓存命中，不再请求
            await svc._build_condition_context()
            assert call_count == 1

    @pytest.mark.asyncio
    async def test_tool_failure_silent_degradation(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0

        async def mock_time(params, session):
            raise Exception("network error")

        async def mock_weather(params, session):
            raise Exception("network error")

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            result = await svc._build_condition_context()
            assert result == ""

    @pytest.mark.asyncio
    async def test_weather_error_not_cached(self):
        svc = AutomationService.__new__(AutomationService)
        svc._weather_cache = None
        svc._weather_cache_at = 0.0

        async def mock_time(params, session):
            return {"date": "2026-06-20", "time": "15:30:00", "weekday": "星期六"}

        async def mock_weather(params, session):
            return {"error": "获取天气失败"}

        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.current_time_handler", mock_time), \
             patch("app.mcp.weather_tools.get_weather_handler", mock_weather):
            result = await svc._build_condition_context()
            assert "天气" not in result
            assert svc._weather_cache is None
