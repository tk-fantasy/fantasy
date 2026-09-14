"""Coverage 补全：app/mcp/* + app/integration/* + ha_service + integration_routes。

所有外部边界一律 mock：MCP stdio 客户端（fake server 对象）、子进程（fake process）、
WebSocket（fake websockets 模块）、HTTP（mock client）。不绑定端口、不发真实网络请求、
不读写真实 app/data、integrations、logs 目录。
每个断言都验证真实行为：工具调用结果、supervisor 重启决策、RPC 请求/响应、路由 JSON。
"""
from __future__ import annotations

import asyncio
import io
import json
import queue
import socket
import sys
import types
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integration.host_registry import HostMethodRegistry
from app.integration.integration_layer import IntegrationLayer
from app.integration.manifest_loader import load_all_manifests, load_manifests
from app.integration.plugin_process import PluginProcess
from app.integration.plugin_supervisor import PluginSupervisor
from app.integration.rpc_protocol import (
    METHOD_HOST_BROADCAST,
    METHOD_HOST_CAM_PUSH,
    METHOD_HOST_CAM_REGISTER,
    METHOD_HOST_CAM_SET_FLAGS,
    METHOD_HOST_CAM_UNREGISTER,
    METHOD_HOST_HA_CALL,
    METHOD_HOST_HA_DEVICES,
    METHOD_HOST_HA_STATES,
    METHOD_HOST_LLM_CHAT,
)
from app.integration.schema import Capability, CapabilityType, Manifest
from app.integration.sdk.plugin_base import HostProxy, IntegrationPlugin
from app.integration.sdk.stdio_runtime import _StdioRuntime, run_stdio_plugin
from app.integration.sink_manager import SinkManager
from app.mcp.external_mcp_server import ExternalMCPServer
from app.mcp.langchain_tools import mcp_to_langchain_tool
from app.mcp.local_mcp_servers import (
    current_time_handler,
    describe_state_handler,
    register_local_tools,
)
from app.mcp.mcp_client_manager import MCPClientManager, MCPTool
from app.mcp.search_tools import _exa_url, _parse_mcp_text, web_search_handler
from app.mcp.tool_executor import ToolExecutor, _type_check, _validate_params
from app.mcp.web_tools import (
    _SafeTransport,
    _convert,
    _is_blocked_host,
    _make_redirect_hook,
    close_http_client,
    fetch_webpage_handler,
)
import app.mcp.web_tools as web_tools_mod


def _manifest(mid: str = "p1", caps=None, permissions=(), ui=None) -> Manifest:
    return Manifest(
        id=mid, name=mid, version="1", aether_api_version="1",
        capabilities=caps or [], permissions=list(permissions),
        ui_contributions=ui or [],
    )


# ===========================================================================
# 1) app/mcp/mcp_client_manager.py
# ===========================================================================

class _FakeExternalServer:
    instances: list = []

    def __init__(self, name, cmd, args=None):
        self.name = name
        self.started = False
        self.stopped = False
        self.fail_list = False
        self.fail_stop = False
        self.fail_call = False
        self.tools = [
            {"name": "t1", "description": "d1",
             "inputSchema": {"type": "object",
                             "properties": {"a": {"type": "string"}}}},
            {"name": "t2", "description": "d2", "inputSchema": "not-a-dict"},
            {"name": "t3", "description": "d3"},
        ]
        _FakeExternalServer.instances.append(self)

    async def start(self):
        self.started = True

    async def list_tools(self):
        if self.fail_list:
            raise RuntimeError("list failed")
        return self.tools

    async def stop(self):
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("stop failed")

    async def call_tool(self, name, arguments):
        if self.fail_call:
            raise RuntimeError("call failed")
        return [{"type": "text",
                 "text": f"{name}={json.dumps(arguments or {}, sort_keys=True)}"}]


@pytest.fixture()
def fake_ext(monkeypatch):
    _FakeExternalServer.instances = []
    monkeypatch.setattr("app.mcp.mcp_client_manager.ExternalMCPServer",
                        _FakeExternalServer)
    return _FakeExternalServer


async def test_connect_external_server_registers_tools(fake_ext):
    mgr = MCPClientManager()
    tools = await mgr.connect_external_server("ext", "whatever", ["a"])
    assert [t.tool_name for t in tools] == ["t1", "t2", "t3"]
    assert tools[0].parameters["properties"] == {"a": {"type": "string"}}
    # 非 dict / 缺失 inputSchema 回退到空参数 schema
    assert tools[1].parameters == {"type": "object", "properties": {}}
    assert tools[2].parameters == {"type": "object", "properties": {}}
    srv = mgr.list_external_servers()
    assert srv == [{"name": "ext", "tool_count": 3,
                    "tools": ["t1", "t2", "t3"]}]


async def test_connect_external_server_twice_reuses(fake_ext):
    mgr = MCPClientManager()
    first = await mgr.connect_external_server("ext", "cmd")
    again = await mgr.connect_external_server("ext", "cmd")
    assert [t.tool_name for t in again] == [t.tool_name for t in first]
    assert len(_FakeExternalServer.instances) == 1  # 未重复 spawn


async def test_connect_list_failure_cleans_up(monkeypatch):
    def factory(name, cmd, args=None):
        s = _FakeExternalServer(name, cmd, args)
        s.fail_list = True
        return s
    monkeypatch.setattr("app.mcp.mcp_client_manager.ExternalMCPServer",
                        factory)
    mgr = MCPClientManager()
    with pytest.raises(RuntimeError, match="list failed"):
        await mgr.connect_external_server("bad", "cmd")
    assert mgr.list_external_servers() == []
    assert mgr.list_tools() == []


async def test_connect_list_failure_swallows_stop_error(monkeypatch):
    def factory(name, cmd, args=None):
        s = _FakeExternalServer(name, cmd, args)
        s.fail_list = True
        s.fail_stop = True
        return s
    monkeypatch.setattr("app.mcp.mcp_client_manager.ExternalMCPServer",
                        factory)
    mgr = MCPClientManager()
    with pytest.raises(RuntimeError, match="list failed"):
        await mgr.connect_external_server("bad2", "cmd")
    assert mgr.list_tools() == []


async def test_external_handler_success_and_error(fake_ext):
    mgr = MCPClientManager()
    await mgr.connect_external_server("ext", "cmd")
    tool = mgr.get_tool("ext___t1")
    result = await tool.handler({"a": "x"}, None)
    assert result["content"][0]["text"] == 't1={"a": "x"}'
    assert result["text"] == 't1={"a": "x"}'
    srv = _FakeExternalServer.instances[0]
    srv.fail_call = True
    bad = await tool.handler({"a": "y"}, None)
    assert bad == {"error": "call failed"}


async def test_disconnect_server_true_and_false(fake_ext):
    mgr = MCPClientManager()
    await mgr.connect_external_server("ext", "cmd")
    assert await mgr.disconnect_server("nope") is False
    assert await mgr.disconnect_server("ext") is True
    assert mgr.get_tool("ext___t1") is None
    assert mgr.list_external_servers() == []
    assert _FakeExternalServer.instances[0].stopped is True


async def test_disconnect_server_with_info_logging_configured(fake_ext, caplog):
    """回归锚点：disconnect 的日志曾用 extra={"name": ...}（LogRecord 保留键），
    在 INFO handler 已配置时抛 KeyError。caplog 挂上 handler 复现该场景。"""
    import logging
    mgr = MCPClientManager()
    await mgr.connect_external_server("ext", "cmd")
    with caplog.at_level(logging.INFO, logger="app.mcp.mcp_client_manager"):
        assert await mgr.disconnect_server("ext") is True
    assert any("disconnected" in r.getMessage() for r in caplog.records)


async def test_disconnect_all_external(fake_ext):
    mgr = MCPClientManager()
    await mgr.connect_external_server("a", "cmd")
    await mgr.connect_external_server("b", "cmd")
    await mgr.disconnect_all_external()
    assert mgr.list_tools() == []
    assert mgr.list_external_servers() == []
    assert all(s.stopped for s in _FakeExternalServer.instances)


def test_register_get_list_tools():
    mgr = MCPClientManager()
    tool = MCPTool(client_id="c", tool_name="t", description="d",
                   parameters={}, handler=None)
    mgr.register_tool(tool)
    assert mgr.get_tool("c___t") is tool
    assert mgr.get_tool("missing") is None
    assert mgr.list_tools() == [tool]


# ===========================================================================
# 2) app/mcp/tool_executor.py
# ===========================================================================

def _tool(schema, handler):
    return MCPTool(client_id="c", tool_name="t", description="d",
                   parameters=schema, handler=handler)


async def test_executor_validation_rejects_missing_required():
    schema = {"type": "object", "properties": {"name": {"type": "string"}},
              "required": ["name"]}
    handler = AsyncMock(return_value={})
    executor = ToolExecutor(MagicMock())
    executor._manager.get_tool = MagicMock(
        return_value=_tool(schema, handler))
    result = await executor.execute_tool_by_name("c___t", {}, None)
    assert result["success"] is False
    assert "invalid parameters" in result["error"]
    assert "missing required field: name" in result["error"]
    handler.assert_not_awaited()


async def test_executor_validation_type_and_enum_errors():
    schema = {"type": "object", "properties": {
        "n": {"type": "integer", "enum": [1, 2]},
        "s": {"type": "string"},
    }}
    handler = AsyncMock(return_value={"ok": 1})
    executor = ToolExecutor(MagicMock())
    executor._manager.get_tool = MagicMock(
        return_value=_tool(schema, handler))
    result = await executor.execute_tool_by_name(
        "c___t", {"n": "not-int", "s": "ok", "extra": 1}, None)
    assert result["success"] is False
    assert "expected integer, got str" in result["error"]


async def test_executor_success_with_schema():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
    handler = AsyncMock(return_value={"value": 7})
    executor = ToolExecutor(MagicMock())
    executor._manager.get_tool = MagicMock(
        return_value=_tool(schema, handler))
    result = await executor.execute_tool_by_name("c___t", {"n": 7}, None)
    assert result == {"success": True, "tool_name": "c___t",
                      "result": {"value": 7}}


def test_resolve_tool_name_paths():
    tool = MCPTool(client_id="c", tool_name="search", description="d",
                   parameters={}, handler=None)
    manager = MCPClientManager()
    manager.register_tool(tool)
    executor = ToolExecutor(manager)
    assert executor.resolve_tool_name("c___search") == "c___search"
    assert executor.resolve_tool_name("search") == "c___search"
    assert executor.resolve_tool_name("nope") == "nope"
    assert executor.resolve_tool_name("a___nope") == "a___nope"


def test_type_check_unknown_type_passes():
    assert _type_check(123, "uuid") is True
    assert _type_check(123, "integer") is True
    assert _type_check("x", "integer") is False


def test_validate_params_enum_and_passthrough():
    schema = {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["fast", "slow"]},
    }}
    assert _validate_params({"mode": "turbo"}, schema) == [
        "field 'mode': value 'turbo' not in ['fast', 'slow']"]
    # 未定义字段宽松透传；类型不对时跳过 enum 校验
    assert _validate_params({"other": 1}, schema) == []
    schema_bad_type = {"type": "object", "properties": {
        "mode": {"type": "integer", "enum": [1]}},
        "required": ["mode"]}
    errors = _validate_params({"mode": "x"}, schema_bad_type)
    assert len(errors) == 1  # 类型错报一条；类型不对就不再校验 enum
    assert "expected integer" in errors[0]


# ===========================================================================
# 3) app/mcp/web_tools.py
# ===========================================================================

def test_is_blocked_host_gaierror_returns_false(monkeypatch):
    def boom(*a, **kw):
        raise socket.gaierror("dns fail")
    monkeypatch.setattr("app.mcp.web_tools.socket.getaddrinfo", boom)
    assert _is_blocked_host("no-such-host.invalid") is False


def test_is_blocked_host_non_ip_entries_skipped(monkeypatch):
    infos = [(None, None, None, "", ("some-hostname", 0))]
    monkeypatch.setattr("app.mcp.web_tools.socket.getaddrinfo",
                        lambda *a, **kw: infos)
    assert _is_blocked_host("example.com") is False


def test_is_blocked_host_detects_private_ip(monkeypatch):
    infos = [(None, None, None, "", ("192.168.1.10", 0))]
    monkeypatch.setattr("app.mcp.web_tools.socket.getaddrinfo",
                        lambda *a, **kw: infos)
    assert _is_blocked_host("internal.lan") is True


_SENTINEL = object()


async def test_safe_transport_delegates_on_gaierror(monkeypatch):
    async def fake_super(self, request):
        return _SENTINEL
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                        fake_super)

    def boom(*a, **kw):
        raise socket.gaierror("fail")
    monkeypatch.setattr("app.mcp.web_tools.socket.getaddrinfo", boom)
    transport = _SafeTransport()
    req = httpx.Request("GET", "https://example.com/")
    assert await transport.handle_async_request(req) is _SENTINEL


async def test_safe_transport_blocks_loopback(monkeypatch):
    async def fake_super(self, request):
        return _SENTINEL
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                        fake_super)
    infos = [(None, None, None, "", ("127.0.0.1", 0))]
    monkeypatch.setattr("app.mcp.web_tools.socket.getaddrinfo",
                        lambda *a, **kw: infos)
    transport = _SafeTransport()
    req = httpx.Request("GET", "https://example.com/")
    with pytest.raises(httpx.ConnectError, match="内网"):
        await transport.handle_async_request(req)


async def test_safe_transport_allows_public_and_skips_bad_ip(monkeypatch):
    async def fake_super(self, request):
        return _SENTINEL
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request",
                        fake_super)
    infos = [(None, None, None, "", ("not-an-ip", 0)),
             (None, None, None, "", ("8.8.8.8", 0))]
    monkeypatch.setattr("app.mcp.web_tools.socket.getaddrinfo",
                        lambda *a, **kw: infos)
    transport = _SafeTransport()
    req = httpx.Request("GET", "https://example.com/")
    assert await transport.handle_async_request(req) is _SENTINEL


async def test_redirect_hook_allows_same_domain_relative():
    hook = _make_redirect_hook()[0]
    resp = MagicMock()
    resp.is_redirect = True
    resp.status_code = 301
    resp.headers = {"location": "/next"}
    assert await hook(resp) is None  # 相对路径同域跳过


async def test_redirect_hook_blocks_bad_absolute_target():
    hook = _make_redirect_hook()[0]
    resp = MagicMock()
    resp.is_redirect = True
    resp.status_code = 302
    resp.headers = {"location": "ftp://evil.example/x"}
    with pytest.raises(httpx.RequestError, match="重定向目标被拦截"):
        await hook(resp)


async def test_redirect_hook_ignores_non_redirect():
    hook = _make_redirect_hook()[0]
    resp = MagicMock()
    resp.is_redirect = False
    resp.status_code = 200
    assert await hook(resp) is None


async def test_http_client_reused_recreated_and_closed(monkeypatch):
    monkeypatch.setattr(web_tools_mod, "_http_client", None)
    c1 = web_tools_mod._get_http_client()
    c2 = web_tools_mod._get_http_client()
    assert c1 is c2
    await c1.aclose()
    c3 = web_tools_mod._get_http_client()  # is_closed → 重建
    assert c3 is not c1
    await close_http_client()
    assert web_tools_mod._http_client is None
    await close_http_client()  # 重复 close 无副作用


def test_convert_unknown_format_passthrough():
    html = "<h1>Hi</h1>"
    assert _convert(html, "text/html", "raw") == html


async def test_fetch_webpage_http_error():
    with patch("app.mcp.web_tools._validate_url", return_value=None):
        with patch("app.mcp.web_tools._get_http_client") as mc:
            mc.return_value.get = AsyncMock(
                side_effect=httpx.ConnectError("boom"))
            result = await fetch_webpage_handler(
                {"url": "http://example.com"}, None)
    assert "抓取失败" in result["error"]
    assert result["url"] == "http://example.com"



async def test_current_time_handler_explicit_tz():
    result = await current_time_handler({"tz_offset_hours": -5}, None)
    assert result["tz_offset_hours"] == -5
    assert len(result["date"].split("-")) == 3
    assert len(result["time"].split(":")) == 3
    assert result["weekday"]


async def test_current_time_handler_default_tz():
    result = await current_time_handler({}, None)
    assert result["tz_offset_hours"] >= -12


async def test_describe_state_handler():
    session = SimpleNamespace(latest_visual_state="frame-1")
    assert await describe_state_handler({}, session) == {
        "visual_state": "frame-1"}
    assert await describe_state_handler({}, None) == {"visual_state": None}


def test_register_local_tools_registers_expected_set():
    mgr = MCPClientManager()
    register_local_tools(mgr)
    names = {t.tool_name for t in mgr.list_tools()}
    assert names == {"describe_state", "fetch_webpage", "web_search"}


def _vision_mocks():
    vision = MagicMock(ask_about_frame=AsyncMock(return_value="是"))
    ha = MagicMock(get_states=AsyncMock(return_value=[]))
    cam = MagicMock()
    cam._active_display_id = ""
    cam.list_cameras.return_value = []
    return vision, ha, cam
