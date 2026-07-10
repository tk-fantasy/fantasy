"""Tests for ToolExecutor with mocked MCPClientManager."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp.tool_executor import ToolExecutor


class TestToolExecutor:
    @pytest.mark.asyncio
    async def test_tool_found_and_executed(self):
        handler = AsyncMock(return_value={"data": "result"})
        tool = MagicMock()
        tool.handler = handler
        manager = MagicMock()
        manager.get_tool.return_value = tool
        executor = ToolExecutor(manager)
        result = await executor.execute_tool_by_name("test_tool", {"key": "val"}, None)
        assert result["success"] is True
        assert result["tool_name"] == "test_tool"
        handler.assert_called_once_with({"key": "val"}, None)

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        manager = MagicMock()
        manager.get_tool.return_value = None
        executor = ToolExecutor(manager)
        result = await executor.execute_tool_by_name("missing", {}, None)
        assert result["success"] is False
        assert "not found" in result["error"]

    @pytest.mark.asyncio
    async def test_handler_returns_error(self):
        handler = AsyncMock(return_value={"error": "something failed"})
        tool = MagicMock()
        tool.handler = handler
        manager = MagicMock()
        manager.get_tool.return_value = tool
        executor = ToolExecutor(manager)
        result = await executor.execute_tool_by_name("test", {}, None)
        assert result["success"] is False
        assert result["error"] == "something failed"
