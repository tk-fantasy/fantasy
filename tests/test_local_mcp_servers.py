"""Tests for local MCP server tools."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.mcp.local_mcp_servers import (
    current_time_handler,
    describe_state_handler,
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
        assert result["visual_state"] is None

    @pytest.mark.asyncio
    async def test_with_session_data(self):
        session = MagicMock()
        session.latest_visual_state = {"action": "idle", "feedback": "平静"}

        result = await describe_state_handler({}, session)
        assert result["visual_state"] == {"action": "idle", "feedback": "平静"}



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
