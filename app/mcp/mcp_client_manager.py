from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .external_mcp_server import ExternalMCPServer

logger = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


@dataclass
class MCPTool:
    client_id: str
    tool_name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler


_EMPTY_PARAMS: dict[str, Any] = {"type": "object", "properties": {}}


class MCPClientManager:
    def __init__(self) -> None:
        self._tools: dict[str, MCPTool] = {}
        self._external_servers: list[ExternalMCPServer] = []

    def register_tool(self, tool: MCPTool) -> None:
        key = f"{tool.client_id}___{tool.tool_name}"
        self._tools[key] = tool

    def list_tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> MCPTool | None:
        return self._tools.get(name)

    # ------------------------------------------------------- 外部 MCP server

    async def connect_external_server(self, name: str, cmd: str, args: list[str] | None = None) -> list[MCPTool]:
        """连接一个外部 MCP server（通过 stdio），将其工具全部注册到管理器。

        返回注册的工具列表。重复调用同 name 会自动跳过已连接的。
        """
        for server in self._external_servers:
            if server.name == name:
                logger.info("External MCP server already connected", extra={"name": name})
                return self._list_tools_by_client(name)

        server = ExternalMCPServer(name, cmd, args)
        await server.start()
        # 先加入列表确保 disconnect_all_external 能清理,再注册工具
        self._external_servers.append(server)
        try:
            raw_tools = await server.list_tools()
        except Exception:
            self._external_servers.remove(server)
            try:
                await server.stop()
            except Exception:
                pass  # 不掩盖 list_tools 的原始异常
            raise
        registered: list[MCPTool] = []
        for raw in raw_tools:
            tool_name = str(raw.get("name", ""))
            description = str(raw.get("description", ""))
            input_schema = raw.get("inputSchema", _EMPTY_PARAMS) if isinstance(raw.get("inputSchema"), dict) else _EMPTY_PARAMS
            tool = MCPTool(
                client_id=name,
                tool_name=tool_name,
                description=description,
                parameters=input_schema,
                handler=self._make_external_handler(server, tool_name),
            )
            self.register_tool(tool)
            registered.append(tool)

        logger.info("External MCP server connected", extra={"name": name, "tools": len(registered)})
        return registered

    def _make_external_handler(self, server: ExternalMCPServer, tool_name: str) -> ToolHandler:
        async def handler(parameters: dict[str, Any], _session: Any) -> dict[str, Any]:
            try:
                content = await server.call_tool(tool_name, parameters)
                texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                return {"content": content, "text": "\n".join(texts)}
            except Exception as exc:
                logger.warning("External MCP tool call failed", extra={"server": server.name, "tool": tool_name, "error": str(exc)})
                return {"error": str(exc)}

        return handler

    def _list_tools_by_client(self, client_id: str) -> list[MCPTool]:
        prefix = f"{client_id}___"
        return [t for k, t in self._tools.items() if k.startswith(prefix)]

    async def disconnect_all_external(self) -> None:
        client_ids = {s.name for s in self._external_servers}
        for server in self._external_servers:
            await server.stop()
        self._external_servers.clear()
        self._tools = {k: v for k, v in self._tools.items() if v.client_id not in client_ids}

    async def disconnect_server(self, name: str) -> bool:
        """断开指定的外部 MCP server，清理其注册的工具。

        Returns:
            True 表示成功断开，False 表示未找到该 server。
        """
        for i, server in enumerate(self._external_servers):
            if server.name == name:
                await server.stop()
                self._external_servers.pop(i)
                # 清理该 server 注册的所有工具
                prefix = f"{name}___"
                self._tools = {k: v for k, v in self._tools.items() if not k.startswith(prefix)}
                logger.info("External MCP server disconnected", extra={"name": name})
                return True
        return False

    def list_external_servers(self) -> list[dict[str, Any]]:
        """列出所有已连接的外部 MCP server 及其工具数。"""
        result = []
        for server in self._external_servers:
            tools = self._list_tools_by_client(server.name)
            result.append({
                "name": server.name,
                "tool_count": len(tools),
                "tools": [t.tool_name for t in tools],
            })
        return result
