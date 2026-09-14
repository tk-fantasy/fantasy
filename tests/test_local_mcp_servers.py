"""Tests for local MCP server tools."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.local_mcp_servers import (
    current_time_handler,
    describe_state_handler,
)


class TestCurrentTimeHandler:
    @pytest.mark.asyncio
    async def test_default_timezone_beijing(self):
        result = await current_time_handler({}, None)
        assert "datetime" in result
        assert "date" in result
        assert "time" in result
        assert "weekday" in result
        assert result["tz_offset_hours"] == 8

    @pytest.mark.asyncio
    async def test_custom_timezone(self):
        result = await current_time_handler({"tz_offset_hours": 0}, None)
        assert result["tz_offset_hours"] == 0

    @pytest.mark.asyncio
    async def test_weekday_names(self):
        result = await current_time_handler({}, None)
        valid_weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
        assert result["weekday"] in valid_weekdays


class TestDescribeStateHandler:
    @pytest.mark.asyncio
    async def test_no_session(self):
        result = await describe_state_handler({}, None)
        assert "visual_state" in result
        assert result["visual_state"] is None

    @pytest.mark.asyncio
    async def test_with_session_data(self):
        session = MagicMock()
        session.latest_visual_state = {"action": "idle", "feedback": "平静"}

        result = await describe_state_handler({}, session)
        assert result["visual_state"] == {"action": "idle", "feedback": "平静"}



