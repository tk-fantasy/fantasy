"""Tests for LangChain tools conversion."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.mcp.langchain_tools import (
    _json_schema_to_args_schema,
    mcp_to_langchain_tool,
    convert_all_tools,
)
from app.mcp.mcp_client_manager import MCPTool


class TestJsonSchemaToArgsSchema:
    def test_empty_schema(self):
        model = _json_schema_to_args_schema({"type": "object", "properties": {}})
        assert model is not None
        instance = model()
        assert instance is not None

    def test_string_field(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name"}},
            "required": ["name"],
        }
        model = _json_schema_to_args_schema(schema)
        instance = model(name="test")
        assert instance.name == "test"

    def test_integer_field(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer", "description": "Count"}},
            "required": ["count"],
        }
        model = _json_schema_to_args_schema(schema)
        instance = model(count=42)
        assert instance.count == 42

    def test_optional_field(self):
        schema = {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Name"}},
        }
        model = _json_schema_to_args_schema(schema)
        instance = model()
        assert instance.name is None

    def test_boolean_field(self):
        schema = {
            "type": "object",
            "properties": {"enabled": {"type": "boolean", "description": "Enabled"}},
            "required": ["enabled"],
        }
        model = _json_schema_to_args_schema(schema)
        instance = model(enabled=True)
        assert instance.enabled is True

    def test_object_field(self):
        schema = {
            "type": "object",
            "properties": {"data": {"type": "object", "description": "Data"}},
            "required": ["data"],
        }
        model = _json_schema_to_args_schema(schema)
        instance = model(data={"key": "value"})
        assert instance.data == {"key": "value"}

    def test_array_field(self):
        schema = {
            "type": "object",
            "properties": {"items": {"type": "array", "description": "Items"}},
            "required": ["items"],
        }
        model = _json_schema_to_args_schema(schema)
        instance = model(items=[1, 2, 3])
        assert instance.items == [1, 2, 3]


class TestMcpToLangchainTool:
    def test_short_name(self):
        tool = MCPTool(
            client_id="test_client",
            tool_name="test_tool",
            description="Test description",
            parameters={"type": "object", "properties": {}},
            handler=AsyncMock(return_value={"result": "ok"}),
        )
        
        lc_tool = mcp_to_langchain_tool(tool, full_name=False)
        assert lc_tool.name == "test_tool"
        assert lc_tool.description == "Test description"

    def test_full_name(self):
        tool = MCPTool(
            client_id="test_client",
            tool_name="test_tool",
            description="Test description",
            parameters={"type": "object", "properties": {}},
            handler=AsyncMock(return_value={"result": "ok"}),
        )
        
        lc_tool = mcp_to_langchain_tool(tool, full_name=True)
        assert lc_tool.name == "test_client___test_tool"

    @pytest.mark.asyncio
    async def test_handler_called_with_session(self):
        handler = AsyncMock(return_value={"result": "ok"})
        tool = MCPTool(
            client_id="test",
            tool_name="tool",
            description="desc",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
        
        lc_tool = mcp_to_langchain_tool(tool)
        config = {"configurable": {"session": "test_session"}}
        await lc_tool.coroutine(config, param1="value1")
        
        handler.assert_called_once()
        call_args = handler.call_args
        assert call_args[0][1] == "test_session"

    @pytest.mark.asyncio
    async def test_dict_result_json_serialized(self):
        handler = AsyncMock(return_value={"key": "value", "num": 42})
        tool = MCPTool(
            client_id="test",
            tool_name="tool",
            description="desc",
            parameters={"type": "object", "properties": {}},
            handler=handler,
        )
        
        lc_tool = mcp_to_langchain_tool(tool)
        config = {"configurable": {"session": None}}
        result = await lc_tool.coroutine(config)
        
        import json
        parsed = json.loads(result)
        assert parsed["key"] == "value"
        assert parsed["num"] == 42


class TestConvertAllTools:
    def test_converts_all_tools(self):
        manager = MagicMock()
        manager.list_tools.return_value = [
            MCPTool("c1", "t1", "d1", {"type": "object", "properties": {}}, AsyncMock()),
            MCPTool("c2", "t2", "d2", {"type": "object", "properties": {}}, AsyncMock()),
        ]
        
        tools = convert_all_tools(manager)
        assert len(tools) == 2

    def test_empty_manager(self):
        manager = MagicMock()
        manager.list_tools.return_value = []
        
        tools = convert_all_tools(manager)
        assert tools == []
