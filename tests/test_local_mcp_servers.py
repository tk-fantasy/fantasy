"""Tests for local MCP server tools."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.local_mcp_servers import (
    current_time_handler,
    describe_state_handler,
    create_verify_condition_handler,
    create_verify_action_handler,
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
        assert "latest_tool_result" in result
        assert result["visual_state"] is None
        assert result["latest_tool_result"] is None

    @pytest.mark.asyncio
    async def test_with_session_data(self):
        session = MagicMock()
        session.latest_visual_state = {"action": "idle", "feedback": "平静"}
        session.latest_tool_result = {"tool": "test", "result": "ok"}
        
        result = await describe_state_handler({}, session)
        assert result["visual_state"] == {"action": "idle", "feedback": "平静"}
        assert result["latest_tool_result"] == {"tool": "test", "result": "ok"}


class TestVerifyConditionHandler:
    @pytest.mark.asyncio
    async def test_auto_detect_time(self):
        camera_stream = MagicMock()
        vision_client = MagicMock()
        ha_client = MagicMock()
        
        handler = create_verify_condition_handler(camera_stream, vision_client, ha_client)
        result = await handler({"condition": "现在是几点", "condition_type": "auto"}, None)
        assert result["type"] == "time"
        assert "current_time" in result

    @pytest.mark.asyncio
    async def test_auto_detect_weather(self):
        camera_stream = MagicMock()
        vision_client = MagicMock()
        ha_client = MagicMock()

        handler = create_verify_condition_handler(camera_stream, vision_client, ha_client)
        from unittest.mock import patch
        with patch("app.mcp.local_mcp_servers.get_weather_handler", new_callable=lambda: AsyncMock(return_value={"location": "上海", "weather": "晴"})):
            result = await handler({"condition": "今天天气如何", "condition_type": "auto"}, None)
        assert result["type"] == "weather"

    @pytest.mark.asyncio
    async def test_auto_detect_vision(self):
        camera_stream = MagicMock()
        camera_stream.get_latest_frame.return_value = None
        vision_client = MagicMock()
        ha_client = MagicMock()
        
        handler = create_verify_condition_handler(camera_stream, vision_client, ha_client)
        result = await handler({"condition": "画面中有人", "condition_type": "auto"}, None)
        assert result["type"] == "vision"
        assert result["camera_connected"] is False

    @pytest.mark.asyncio
    async def test_auto_detect_device(self):
        camera_stream = MagicMock()
        vision_client = MagicMock()
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "床头灯"}},
        ])

        handler = create_verify_condition_handler(camera_stream, vision_client, ha_client)
        result = await handler({"condition": "设备状态是否开启", "condition_type": "auto"}, None)
        assert result["type"] == "device"
        assert "devices" in result

    @pytest.mark.asyncio
    async def test_explicit_time_type(self):
        camera_stream = MagicMock()
        vision_client = MagicMock()
        ha_client = MagicMock()
        
        handler = create_verify_condition_handler(camera_stream, vision_client, ha_client)
        result = await handler({"condition": "任意条件", "condition_type": "time"}, None)
        assert result["type"] == "time"

    @pytest.mark.asyncio
    async def test_vision_with_camera_connected(self):
        camera_stream = MagicMock()
        camera_stream.get_latest_frame.return_value = MagicMock()
        vision_client = MagicMock()
        vision_client.ask_about_frame = AsyncMock(return_value="是的，画面中有人")
        ha_client = MagicMock()

        handler = create_verify_condition_handler(camera_stream, vision_client, ha_client)
        result = await handler({"condition": "画面中有人吗", "condition_type": "vision"}, None)
        assert result["type"] == "vision"
        assert result["camera_connected"] is True
        assert "vision_judgment" in result


class TestVerifyActionHandler:
    @pytest.mark.asyncio
    async def test_missing_entity_id(self):
        ha_client = MagicMock()
        handler = create_verify_action_handler(ha_client)
        result = await handler({}, None)
        assert result["verified"] is False
        assert "error" in result

    @pytest.mark.asyncio
    async def test_entity_not_found(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[])
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "light.nonexistent"}, None)
        assert result["verified"] is False
        assert "不存在" in result["error"]

    @pytest.mark.asyncio
    async def test_verify_on_state(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "床头灯"}},
        ])
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "light.bed", "expected_state": "on"}, None)
        assert result["verified"] is True
        assert result["current_state"] == "on"

    @pytest.mark.asyncio
    async def test_verify_off_state(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": "light.bed", "state": "off", "attributes": {"friendly_name": "床头灯"}},
        ])
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "light.bed", "expected_state": "off"}, None)
        assert result["verified"] is True
        assert result["current_state"] == "off"

    @pytest.mark.asyncio
    async def test_verify_failed(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": "light.bed", "state": "off", "attributes": {"friendly_name": "床头灯"}},
        ])
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "light.bed", "expected_state": "on"}, None)
        assert result["verified"] is False

    @pytest.mark.asyncio
    async def test_no_expected_state_verified(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": "light.bed", "state": "on", "attributes": {"friendly_name": "床头灯"}},
        ])
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "light.bed"}, None)
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_fuzzy_match_without_domain(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(return_value=[
            {"entity_id": "light.bedroom", "state": "on", "attributes": {"friendly_name": "卧室灯"}},
        ])
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "bedroom", "expected_state": "on"}, None)
        assert result["verified"] is True

    @pytest.mark.asyncio
    async def test_ha_exception_handled(self):
        ha_client = MagicMock()
        ha_client.get_states = AsyncMock(side_effect=Exception("Connection failed"))
        
        handler = create_verify_action_handler(ha_client)
        result = await handler({"entity_id": "light.bed"}, None)
        assert result["verified"] is False
        assert "error" in result
