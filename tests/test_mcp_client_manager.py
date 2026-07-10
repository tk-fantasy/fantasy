"""Tests for MCPClientManager pure methods: register_tool, list_tools, get_tool."""
from __future__ import annotations

from app.mcp.mcp_client_manager import MCPClientManager, MCPTool


def _make_tool(client_id: str = "local", tool_name: str = "test", description: str = "desc") -> MCPTool:
    async def _handler(params, session):
        return {"result": "ok"}
    return MCPTool(client_id=client_id, tool_name=tool_name, description=description, parameters={}, handler=_handler)


class TestMCPClientManager:
    def test_register_and_list(self):
        mgr = MCPClientManager()
        mgr.register_tool(_make_tool())
        tools = mgr.list_tools()
        assert len(tools) == 1
        assert tools[0].tool_name == "test"

    def test_get_tool(self):
        mgr = MCPClientManager()
        mgr.register_tool(_make_tool())
        tool = mgr.get_tool("local___test")
        assert tool is not None
        assert tool.tool_name == "test"

    def test_get_nonexistent_tool(self):
        mgr = MCPClientManager()
        assert mgr.get_tool("nonexistent") is None

    def test_register_overwrites(self):
        mgr = MCPClientManager()
        mgr.register_tool(_make_tool(description="first"))
        mgr.register_tool(_make_tool(description="second"))
        tools = mgr.list_tools()
        assert len(tools) == 1
        assert tools[0].description == "second"

    def test_multiple_tools(self):
        mgr = MCPClientManager()
        mgr.register_tool(_make_tool(tool_name="a"))
        mgr.register_tool(_make_tool(tool_name="b"))
        assert len(mgr.list_tools()) == 2

    def test_key_format(self):
        mgr = MCPClientManager()
        mgr.register_tool(_make_tool(client_id="ha", tool_name="call"))
        assert mgr.get_tool("ha___call") is not None
        assert mgr.get_tool("ha.call") is None
