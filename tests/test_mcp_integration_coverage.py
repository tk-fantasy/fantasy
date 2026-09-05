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
    create_verify_action_handler,
    create_verify_condition_handler,
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
    http_request_handler,
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


async def test_http_request_http_error():
    with patch("app.mcp.web_tools._validate_url", return_value=None):
        with patch("app.mcp.web_tools._get_http_client") as mc:
            mc.return_value.request = AsyncMock(
                side_effect=httpx.ConnectError("nope"))
            result = await http_request_handler(
                {"url": "http://example.com", "method": "POST"}, None)
    assert "请求失败" in result["error"]


async def test_http_request_invalid_json_body_returns_text():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"{not json"
    resp.encoding = "utf-8"
    resp.headers = {"Content-Type": "application/json"}
    with patch("app.mcp.web_tools._validate_url", return_value=None):
        with patch("app.mcp.web_tools._get_http_client") as mc:
            mc.return_value.request = AsyncMock(return_value=resp)
            result = await http_request_handler(
                {"url": "http://api.example.com"}, None)
    assert result["text"] == "{not json"
    assert "json" not in result


async def test_http_request_text_body():
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"hello"
    resp.encoding = "utf-8"
    resp.headers = {"Content-Type": "text/plain; charset=utf-8"}
    with patch("app.mcp.web_tools._validate_url", return_value=None):
        with patch("app.mcp.web_tools._get_http_client") as mc:
            mc.return_value.request = AsyncMock(return_value=resp)
            result = await http_request_handler(
                {"url": "http://api.example.com"}, None)
    assert result["text"] == "hello"


# ===========================================================================
# 4) app/mcp/local_mcp_servers.py
# ===========================================================================

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
    assert names == {"describe_state", "fetch_webpage", "http_request",
                     "web_search"}


def _vision_mocks():
    vision = MagicMock(ask_about_frame=AsyncMock(return_value="是"))
    ha = MagicMock(get_states=AsyncMock(return_value=[]))
    cam = MagicMock()
    cam._active_display_id = ""
    cam.list_cameras.return_value = []
    return vision, ha, cam


async def test_verify_condition_auto_detects_time(monkeypatch):
    vision, ha, cam = _vision_mocks()
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "现在几点了"}, None)
    assert result["type"] == "time"
    assert result["condition_met"] is None
    assert "instruction" in result


async def test_verify_condition_auto_detects_weather(monkeypatch):
    vision, ha, cam = _vision_mocks()
    monkeypatch.setattr("app.mcp.local_mcp_servers.get_weather_handler",
                        AsyncMock(return_value={"temp": "20"}))
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "今天会下雨吗"}, None)
    assert result["type"] == "weather"
    assert result["current_weather"] == {"temp": "20"}


async def test_verify_condition_vision_no_camera():
    vision, ha, cam = _vision_mocks()
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "画面里有人吗"}, None)
    assert result == {"condition_met": None, "type": "vision",
                      "camera_connected": False,
                      "data": "摄像头当前没有画面（未连接或无法打开）",
                      "instruction": "摄像头未连接，请根据条件内容判断是否满足"}
    vision.ask_about_frame.assert_not_awaited()


async def test_verify_condition_vision_with_frame_and_camera_id():
    vision, ha, cam = _vision_mocks()
    cam._active_display_id = ""
    cam.list_cameras.return_value = [{"id": "cam9"}]
    cam.get_frame.return_value = b"jpeg-bytes"
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "画面里有人吗"}, None)
    assert result["camera_connected"] is True
    assert result["vision_judgment"] == "是"
    cam.get_frame.assert_called_with("cam9")  # 从第一个 enabled 相机取
    vision.ask_about_frame.assert_awaited_once()


async def test_verify_condition_vision_uses_active_display():
    vision, ha, cam = _vision_mocks()
    cam._active_display_id = "cam1"
    cam.get_frame.return_value = b"jpeg"
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "画面", "camera_id": ""}, None)
    assert result["camera_connected"] is True
    cam.get_frame.assert_called_with("cam1")


async def test_verify_condition_auto_fallback_to_time():
    vision, ha, cam = _vision_mocks()
    handler = create_verify_condition_handler(vision, ha, cam)
    # 不含任何分类关键词 → 回退 time
    result = await handler({"condition": "随便什么条件"}, None)
    assert result["type"] == "time"


async def test_verify_condition_device_states():
    vision, ha, cam = _vision_mocks()
    ha.get_states = AsyncMock(return_value=[
        {"entity_id": "light.bed", "state": "on",
         "attributes": {"friendly_name": "Bed"}},
        {"entity_id": "sun.sun", "state": "up", "attributes": {}},
    ])
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "客厅的设备状态"}, None)
    assert result["type"] == "device"
    assert [d["entity_id"] for d in result["devices"]] == ["light.bed"]


async def test_verify_condition_device_error():
    vision, ha, cam = _vision_mocks()
    ha.get_states = AsyncMock(side_effect=RuntimeError("ha down"))
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "设备状态如何"}, None)
    assert result["type"] == "device"
    assert "ha down" in result["error"]


async def test_verify_condition_unknown_type_falls_back_to_time():
    vision, ha, cam = _vision_mocks()
    handler = create_verify_condition_handler(vision, ha, cam)
    result = await handler({"condition": "x", "condition_type": "weird"}, None)
    assert result["type"] == "time"


async def test_verify_action_exact_entity_and_data_checks():
    ha = MagicMock(get_states=AsyncMock(return_value=[
        {"entity_id": "light.bed", "state": "on",
         "attributes": {"friendly_name": "Bed", "brightness": 100}},
    ]))
    handler = create_verify_action_handler(ha)
    result = await handler(
        {"entity_id": "light.bed", "data": {"brightness": "100"}}, None)
    assert result["verified"] is True
    assert result["checks"][0]["passed"] is True


async def test_verify_action_data_mismatch_reports_error():
    ha = MagicMock(get_states=AsyncMock(return_value=[
        {"entity_id": "light.bed", "state": "on",
         "attributes": {"brightness": 100}},
    ]))
    handler = create_verify_action_handler(ha)
    result = await handler(
        {"entity_id": "light.bed", "data": {"brightness": "80"}}, None)
    assert result["verified"] is False
    assert "brightness 期望 80 实际 100" in result["error"]


async def test_verify_action_current_key_fallback_and_string_compare():
    ha = MagicMock(get_states=AsyncMock(return_value=[
        {"entity_id": "cover.win", "state": "open",
         "attributes": {"current_position": 50, "model": "X"}},
    ]))
    handler = create_verify_action_handler(ha)
    ok = await handler({"entity_id": "cover.win",
                        "data": {"position": "50"}}, None)
    assert ok["verified"] is True
    assert ok["checks"][0]["attribute"] == "position"
    # 数值转 float 失败 → 字符串比较
    ok2 = await handler({"entity_id": "cover.win",
                         "data": {"model": 7}}, None)
    assert ok2["verified"] is False  # "7" != "X"


async def test_verify_action_name_part_and_friendly_matching():
    states = [
        {"entity_id": "light.bed", "state": "on",
         "attributes": {"friendly_name": "Bed"}},
        {"entity_id": "cover.win", "state": "open",
         "attributes": {"friendly_name": "卧室窗帘"}},
    ]
    ha = MagicMock(get_states=AsyncMock(return_value=states))
    handler = create_verify_action_handler(ha)
    # 无 domain：name_part 子串匹配（"be" 命中 light.bed，state=on）
    r1 = await handler({"entity_id": "be"}, None)
    assert r1["current_state"] == "on"  # 匹配到 light.bed 而非 cover.win
    assert r1["is_on"] is True
    # 回显的是实际匹配到的实体，而非输入参数
    assert r1["entity_id"] == "light.bed"
    assert r1["entity_name"] == "Bed"
    # friendly_name 匹配（"窗帘" 命中 cover.win，state=open）
    r = await handler({"entity_id": "窗帘"}, None)
    assert r["current_state"] == "open"
    assert r["is_on"] is True
    assert r["entity_id"] == "cover.win"


async def test_verify_action_skips_empty_friendly_name():
    """friendly_name 为空的实体不再 ""in输入 恒真截胡匹配。"""
    ha = MagicMock(get_states=AsyncMock(return_value=[
        {"entity_id": "light.hidden", "state": "on",
         "attributes": {"friendly_name": ""}},
        {"entity_id": "light.bed", "state": "off",
         "attributes": {"friendly_name": "Bed"}},
    ]))
    handler = create_verify_action_handler(ha)
    r = await handler({"entity_id": "Bed"}, None)
    assert r["entity_id"] == "light.bed"
    assert r["current_state"] == "off"
    assert r["is_on"] is False


async def test_verify_action_expected_state_on_off_other():
    ha = MagicMock(get_states=AsyncMock(return_value=[
        {"entity_id": "light.bed", "state": "on", "attributes": {}},
    ]))
    handler = create_verify_action_handler(ha)
    on = await handler({"entity_id": "light.bed", "expected_state": "开"}, None)
    assert on["verified"] is True
    off = await handler({"entity_id": "light.bed", "expected_state": "关"}, None)
    assert off["verified"] is False
    # 其他值：子串比较（"o" in "on"）
    other = await handler({"entity_id": "light.bed",
                           "expected_state": "o"}, None)
    assert other["verified"] is True


async def test_verify_action_missing_entity_param():
    handler = create_verify_action_handler(MagicMock())
    assert await handler({}, None) == {
        "verified": False, "error": "缺少 entity_id 参数"}


async def test_verify_action_entity_not_found():
    ha = MagicMock(get_states=AsyncMock(return_value=[]))
    handler = create_verify_action_handler(ha)
    result = await handler({"entity_id": "light.ghost"}, None)
    assert result == {"verified": False, "entity_id": "light.ghost",
                      "error": "实体 light.ghost 不存在"}


async def test_verify_action_get_states_raises():
    ha = MagicMock(get_states=AsyncMock(side_effect=RuntimeError("down")))
    handler = create_verify_action_handler(ha)
    result = await handler({"entity_id": "light.bed"}, None)
    assert result["verified"] is False
    assert "down" in result["error"]


# ===========================================================================
# 5) app/mcp/search_tools.py
# ===========================================================================

def test_exa_url_without_key():
    assert _exa_url(None) == "https://mcp.exa.ai/mcp"
    assert _exa_url("") == "https://mcp.exa.ai/mcp"


def test_exa_url_with_key_appends_query():
    url = _exa_url("sekret")
    assert url.startswith("https://mcp.exa.ai/mcp?")
    assert "exaApiKey=sekret" in url


def test_parse_mcp_text_direct_json():
    body = json.dumps({"result": {"content": [
        {"type": "text", "text": "found it"}]}})
    assert _parse_mcp_text(body) == "found it"


def test_parse_mcp_text_rejects_plain_and_broken_json():
    assert _parse_mcp_text("hello world") is None       # 非 JSON 开头
    assert _parse_mcp_text("{oops not json") is None    # JSON 解析失败
    assert _parse_mcp_text(json.dumps({"result": {"content": []}})) is None


def test_parse_mcp_text_sse_lines():
    payload = json.dumps({"result": {"content": [
        {"type": "text", "text": "sse hit"}]}})
    body = f"event: message\ndata: {payload}\n\n"
    assert _parse_mcp_text(body) == "sse hit"
    # SSE 行里是坏 JSON → 跳过
    assert _parse_mcp_text("data: {bad}\n") is None


async def test_web_search_handler_uses_api_key(monkeypatch):
    monkeypatch.setattr("app.mcp.search_tools.get_config",
                        lambda p, d=None: "my-key")
    resp = MagicMock()
    resp.text = json.dumps({"result": {"content": [
        {"type": "text", "text": "results"}]}})
    resp.raise_for_status = MagicMock()
    with patch("app.mcp.search_tools._get_shared_client") as mc:
        mc.return_value.post = AsyncMock(return_value=resp)
        result = await web_search_handler({"query": "hello"}, None)
    url = mc.return_value.post.call_args.args[0]
    assert "exaApiKey=my-key" in url
    assert result == {"query": "hello", "text": "results"}


async def test_web_search_handler_no_text_in_response(monkeypatch):
    monkeypatch.setattr("app.mcp.search_tools.get_config",
                        lambda p, d=None: "")
    resp = MagicMock()
    resp.text = "total garbage"
    resp.raise_for_status = MagicMock()
    with patch("app.mcp.search_tools._get_shared_client") as mc:
        mc.return_value.post = AsyncMock(return_value=resp)
        result = await web_search_handler({"query": "q"}, None)
    assert result["results"] == []
    assert result["message"] == "Exa 未返回结果"


# ===========================================================================
# 6) app/mcp/langchain_tools.py
# ===========================================================================

def _mcp_tool(handler, schema=None):
    return MCPTool(client_id="c", tool_name="t", description="测试工具",
                   parameters=schema or {"type": "object", "properties": {}},
                   handler=handler)


async def test_langchain_tool_error_with_hint_and_candidates():
    handler = AsyncMock(return_value={
        "error": "bad input", "hint": "改一下参数",
        "candidates": ["a", "b"]})
    tool = mcp_to_langchain_tool(_mcp_tool(handler))
    out = await tool.coroutine({"configurable": {"session": None}}, x=1)
    lines = out.splitlines()
    assert lines[0] == "Error: bad input"
    assert lines[1] == "修正提示：改一下参数"
    assert lines[2] == "候选：a、b"
    assert lines[3].startswith("原始返回：")


async def test_langchain_tool_error_dict_reason():
    handler = AsyncMock(return_value={"error": {"reason": "结构化原因"}})
    tool = mcp_to_langchain_tool(_mcp_tool(handler))
    out = await tool.coroutine({"configurable": {}}, x=1)
    assert out.startswith("Error: 结构化原因")


async def test_langchain_tool_plain_and_dict_results():
    tool = mcp_to_langchain_tool(_mcp_tool(AsyncMock(return_value="plain")))
    assert await tool.coroutine({"configurable": {}}, x=1) == "plain"
    tool2 = mcp_to_langchain_tool(_mcp_tool(AsyncMock(return_value={"k": "中"})))
    out = await tool2.coroutine({"configurable": {}}, x=1)
    assert json.loads(out) == {"k": "中"}


def test_langchain_tool_full_name_and_args_schema():
    schema = {"type": "object",
              "properties": {"q": {"type": "string", "description": "查询"}},
              "required": ["q"]}
    tool = mcp_to_langchain_tool(_mcp_tool(AsyncMock(), schema),
                                 full_name=True)
    assert tool.name == "c___t"
    assert "q" in tool.args_schema.model_fields


# ===========================================================================
# 7) app/mcp/external_mcp_server.py（fake process，不 spawn）
# ===========================================================================

def _srv() -> ExternalMCPServer:
    return ExternalMCPServer("t", sys.executable, ["-c", "pass"])


async def test_read_stdout_skips_blank_garbage_and_unmatched():
    srv = _srv()
    reader = asyncio.StreamReader()
    reader.feed_data(b"\n")                                   # 空行 → continue
    reader.feed_data(b"<garbage>\n")                          # 非 JSON → 跳过
    reader.feed_data(b'{"jsonrpc":"2.0","method":"x"}\n')     # 无 id → 跳过
    reader.feed_data(json.dumps({"id": 1, "result": {"ok": 1}}).encode() + b"\n")
    reader.feed_eof()
    srv._process = SimpleNamespace(stdout=reader)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    cancelled = loop.create_future()
    cancelled.cancel()
    srv._pending = {1: fut, 2: cancelled}
    task = asyncio.create_task(srv._read_stdout())
    assert await asyncio.wait_for(fut, 5) == {"id": 1, "result": {"ok": 1}}
    await asyncio.wait_for(task, 5)


async def test_drain_stderr_reads_lines_then_eof():
    srv = _srv()
    reader = asyncio.StreamReader()
    reader.feed_data(b"plugin log line\n")
    reader.feed_data(b"another\n")
    reader.feed_eof()
    srv._process = SimpleNamespace(stderr=reader)
    await asyncio.wait_for(srv._drain_stderr(), 5)


async def test_drain_stderr_swallows_read_errors():
    srv = _srv()

    class _Boom:
        async def readline(self):
            raise RuntimeError("pipe exploded")

    srv._process = SimpleNamespace(stderr=_Boom())
    await asyncio.wait_for(srv._drain_stderr(), 5)


class _StopProc:
    """stop() 路径专用假进程：记录 terminate/kill/close，wait 行为可编。"""

    def __init__(self, stdin=None, stdout=None, stderr=None,
                 first_wait_times_out=False):
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.terminated = False
        self.killed = False
        self._first_wait_times_out = first_wait_times_out
        self._waits = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self._waits += 1
        if self._waits == 1 and self._first_wait_times_out:
            raise asyncio.TimeoutError()


class _Stream:
    def __init__(self, at_eof=False, close_raises=False, has_at_eof=True):
        self.closed = False
        self._at_eof = at_eof
        self._close_raises = close_raises
        if has_at_eof:
            self.at_eof = lambda: self._at_eof

    def close(self):
        self.closed = True
        if self._close_raises:
            raise RuntimeError("close failed")


async def _run_stop(proc: _StopProc):
    srv = _srv()
    srv._process = proc
    await asyncio.wait_for(srv.stop(), 10)
    return srv


async def test_stop_graceful_terminate_and_stream_close():
    stdin = _Stream(at_eof=False, close_raises=False)
    stdout = _Stream(at_eof=False, has_at_eof=False)
    proc = _StopProc(stdin=stdin, stdout=stdout, stderr=None)
    await _run_stop(proc)
    assert proc.terminated is True
    assert proc.killed is False
    assert stdin.closed is True
    assert stdout.closed is True


async def test_stop_forces_kill_when_wait_times_out():
    stdin = _Stream(at_eof=False, close_raises=True)  # close 抛错被吞
    proc = _StopProc(stdin=stdin, stdout=None, stderr=None,
                     first_wait_times_out=True)
    await _run_stop(proc)
    assert proc.killed is True


async def test_stop_skips_closed_stream():
    stdout = _Stream(at_eof=True)  # at_eof() 为真 → 跳过 close
    proc = _StopProc(stdin=None, stdout=stdout, stderr=None)
    await _run_stop(proc)
    assert stdout.closed is False


async def test_stop_tolerates_terminate_failure():
    class _AngryProc(_StopProc):
        def terminate(self):
            raise OSError("terminate failed")
    proc = _AngryProc(stdin=_Stream(), stdout=None, stderr=None)
    await _run_stop(proc)
    assert proc.killed is False  # 外层 except 吞掉，不再走 kill


# ===========================================================================
# 8) app/integration/plugin_supervisor.py
# ===========================================================================

class _FakePluginProcess:
    start_calls = 0
    fail_first = 0
    instances: list = []

    def __init__(self, manifest, plugin_root, rpc_timeout=30.0, env=None,
                 host_registry=None, on_stopped=None):
        self.manifest = manifest
        self.plugin_root = plugin_root
        self.started = False
        self.stop_called = 0
        self.stop_error = False
        _FakePluginProcess.instances.append(self)

    async def start(self):
        cls = type(self)
        cls.start_calls += 1
        if cls.start_calls <= cls.fail_first:
            raise RuntimeError("spawn fail")
        self.started = True

    async def stop(self):
        self.stop_called += 1
        if self.stop_error:
            raise RuntimeError("stop boom")


@pytest.fixture()
def fake_proc(monkeypatch):
    _FakePluginProcess.start_calls = 0
    _FakePluginProcess.fail_first = 0
    _FakePluginProcess.instances = []
    monkeypatch.setattr("app.integration.plugin_supervisor.PluginProcess",
                        _FakePluginProcess)
    return _FakePluginProcess


@pytest.fixture()
def no_backoff(monkeypatch):
    delays: list[float] = []

    async def _sleep(s):
        delays.append(s)
    monkeypatch.setattr("app.integration.plugin_supervisor.asyncio.sleep",
                        _sleep)
    return delays


async def test_start_with_retries_succeeds_after_failures(fake_proc, no_backoff):
    _FakePluginProcess.fail_first = 2
    sup = PluginSupervisor(max_restarts=3)
    await sup._start_with_retries(_manifest("p1"), "root")
    assert _FakePluginProcess.start_calls == 3
    assert no_backoff == [1.0, 2.0]  # 指数退避
    assert sup.get_process("p1").started is True


async def test_start_with_retries_exhausts_and_raises(fake_proc, no_backoff):
    _FakePluginProcess.fail_first = 99
    sup = PluginSupervisor(max_restarts=1)
    with pytest.raises(RuntimeError, match="spawn fail"):
        await sup._start_with_retries(_manifest("p1"), "root")
    assert _FakePluginProcess.start_calls == 2  # 首次 + 1 重试
    assert sup.get_process("p1") is None


async def test_start_all_alerts_when_circuit_breaks(fake_proc, monkeypatch):
    _FakePluginProcess.fail_first = 99
    notified: list[tuple] = []

    async def _notify(source, message, level="warning"):
        notified.append((source, message))
    fake_alert = SimpleNamespace(notify=_notify)
    monkeypatch.setattr("app.services.alert_service.alert_service",
                        fake_alert)
    sup = PluginSupervisor(max_restarts=0)
    await sup.start_all([_manifest("a"), _manifest("b")], "root")
    assert [n[0] for n in notified] == ["plugin:a", "plugin:b"]
    assert sup.get_process("a") is None


async def test_start_all_swallows_alert_failure(fake_proc, monkeypatch):
    _FakePluginProcess.fail_first = 99

    async def _boom(*a, **kw):
        raise RuntimeError("alert channel down")
    monkeypatch.setattr("app.services.alert_service.alert_service",
                        SimpleNamespace(notify=_boom))
    sup = PluginSupervisor(max_restarts=0)
    await sup.start_all([_manifest("a")], "root")  # 不应抛出
    assert sup.get_running_manifests() == []


async def test_stop_all_continues_on_error(fake_proc):
    sup = PluginSupervisor()
    a = _FakePluginProcess(_manifest("a"), "root")
    b = _FakePluginProcess(_manifest("b"), "root")
    b.stop_error = True
    sup._processes = {"a": a, "b": b}
    sup._manifests = {"a": _manifest("a"), "b": _manifest("b")}
    await sup.stop_all()
    assert a.stop_called == 1 and b.stop_called == 1
    assert sup.get_running_manifests() == []
    assert sup.get_process("a") is None


async def test_stop_one_paths(fake_proc):
    sup = PluginSupervisor()
    proc = _FakePluginProcess(_manifest("p1"), "root")
    sup._processes = {"p1": proc}
    sup._manifests = {"p1": _manifest("p1")}
    assert await sup.stop_one("missing") is False
    assert await sup.stop_one("p1") is True
    assert sup.get_process("p1") is None
    bad = _FakePluginProcess(_manifest("p2"), "root")
    bad.stop_error = True
    sup._processes = {"p2": bad}
    assert await sup.stop_one("p2") is False


async def test_start_one_returns_bool(fake_proc):
    _FakePluginProcess.fail_first = 99
    sup = PluginSupervisor(max_restarts=0)
    assert await sup.start_one(_manifest("p1"), "root") is False
    _FakePluginProcess.fail_first = 0
    _FakePluginProcess.start_calls = 0
    assert await sup.start_one(_manifest("p1"), "root") is True


# ===========================================================================
# 9) app/integration/plugin_process.py（fake process，不 spawn）
# ===========================================================================

class _FakeStdin:
    def __init__(self, closing=False, drain_delay=0.0, close_raises=False):
        self._closing = closing
        self._drain_delay = drain_delay
        self._close_raises = close_raises
        self.written: list[bytes] = []
        self.closed = False

    def is_closing(self):
        return self._closing

    def write(self, data):
        self.written.append(data)

    async def drain(self):
        if self._drain_delay:
            await asyncio.sleep(self._drain_delay)

    def close(self):
        self.closed = True
        if self._close_raises:
            raise RuntimeError("close failed")


def _feed_reader(lines: list[bytes], eof=True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for ln in lines:
        reader.feed_data(ln)
    if eof:
        reader.feed_eof()
    return reader


class _FakeSubProcess:
    def __init__(self, stdin=None, stdout_lines=None, stderr_lines=None,
                 wait_times_out=False):
        self.stdin = stdin or _FakeStdin()
        self.stdout = _feed_reader(stdout_lines or [])
        self.stderr = _feed_reader(stderr_lines or [])
        self.terminated = False
        self.killed = False
        self._wait_times_out = wait_times_out
        self._waits = 0
        self.pid = 4242

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    async def wait(self):
        self._waits += 1
        if self._waits == 1 and self._wait_times_out:
            raise asyncio.TimeoutError()


def _pp(pid="p1") -> PluginProcess:
    return PluginProcess(manifest=_manifest(pid), plugin_root=".",
                         rpc_timeout=0.2)


async def test_sandbox_env_excludes_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "topsecret")
    monkeypatch.setenv("AETHER_TEST_MARKER", "1")
    proc = _pp()
    assert "JWT_SECRET" not in proc._env
    assert "AETHER_TEST_MARKER" not in proc._env
    assert "PATH" in proc._env  # 白名单变量保留


async def test_process_env_param_overrides(monkeypatch):
    proc = PluginProcess(manifest=_manifest("p1"), plugin_root=".",
                         env={"MY_TOKEN": "abc"})
    assert proc._env["MY_TOKEN"] == "abc"


async def test_start_success_injects_env_and_config(monkeypatch, tmp_path):
    proc = _pp()
    fake = _FakeSubProcess()

    async def fake_exec(*a, **kw):
        return fake
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(PluginProcess, "_handshake", AsyncMock())
    monkeypatch.setattr(PluginProcess, "_find_project_root",
                        staticmethod(lambda: tmp_path))
    monkeypatch.setattr("app.integration.config_helper.get_host_config",
                        lambda pid: {"k": "v"})
    await proc.start()
    assert proc.is_alive is True
    assert proc._env["PYTHONPATH"] == str(tmp_path)
    assert proc._env["AETHER_PLUGIN_UPLOAD_DIR"] == \
        str(tmp_path / "app" / "data" / "uploads" / "p1")
    assert json.loads(proc._env["AETHER_PLUGIN_CONFIG"]) == {"k": "v"}
    proc.call = AsyncMock(return_value={"ok": True})  # 停止时 shutdown 立即应答
    await proc.stop()


async def test_start_clears_stale_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AETHER_PLUGIN_CONFIG", '{"old": 1}')
    proc = _pp()
    fake = _FakeSubProcess()

    async def fake_exec(*a, **kw):
        return fake
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(PluginProcess, "_handshake", AsyncMock())
    monkeypatch.setattr(PluginProcess, "_find_project_root",
                        staticmethod(lambda: None))
    monkeypatch.setattr("app.integration.config_helper.get_host_config",
                        lambda pid: {})
    await proc.start()
    assert "AETHER_PLUGIN_CONFIG" not in proc._env
    proc.call = AsyncMock(return_value={"ok": True})
    await proc.stop()


async def test_start_config_injection_failure_is_tolerated(monkeypatch, tmp_path):
    proc = _pp()
    fake = _FakeSubProcess()

    async def fake_exec(*a, **kw):
        return fake
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(PluginProcess, "_handshake", AsyncMock())
    monkeypatch.setattr(PluginProcess, "_find_project_root",
                        staticmethod(lambda: None))

    def _boom(pid):
        raise RuntimeError("config backend down")
    monkeypatch.setattr("app.integration.config_helper.get_host_config", _boom)
    await proc.start()  # 不应抛出
    proc.call = AsyncMock(return_value={"ok": True})
    await proc.stop()


async def test_start_handshake_failure_cleans_up(monkeypatch):
    stdin = _FakeStdin(closing=True)  # 让 stop() 里的 shutdown call 快速失败
    fake = _FakeSubProcess(stdin=stdin)

    async def fake_exec(*a, **kw):
        return fake
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(PluginProcess, "_handshake",
                        AsyncMock(side_effect=RuntimeError("no handshake")))
    proc = _pp()
    with pytest.raises(RuntimeError, match="no handshake"):
        await proc.start()
    assert proc.is_alive is False
    assert fake.terminated is True  # 子进程已被回收
    await proc.stop()


async def test_handshake_rejects_not_ready():
    proc = _pp()
    proc.call = AsyncMock(return_value={"ready": False})
    with pytest.raises(RuntimeError, match="握手失败"):
        await proc._handshake()


async def test_handshake_sends_expected_params():
    caps = [Capability(type=CapabilityType.OUTPUT_SINK, id="s1")]
    proc2 = PluginProcess(manifest=_manifest("p2", caps=caps),
                          plugin_root=".", rpc_timeout=0.2)
    proc2.call = AsyncMock(return_value={"ready": True})
    await proc2._handshake()
    method, params = proc2.call.await_args.args
    assert method == "handshake"
    assert params["capabilities_expected"] == ["output_sink"]


async def test_write_line_raises_when_no_process():
    proc = _pp()
    proc._process = None
    with pytest.raises(RuntimeError, match="未运行"):
        await proc._write_line({"a": 1})


async def test_write_line_timeout_raises():
    proc = _pp()
    proc._process = _FakeSubProcess(
        stdin=_FakeStdin(drain_delay=5.0))
    with pytest.raises(RuntimeError, match="写入超时"):
        await asyncio.wait_for(proc._write_line({"a": 1}), 5)


async def test_call_rejects_closing_stdin():
    proc = _pp()
    proc._process = _FakeSubProcess(stdin=_FakeStdin(closing=True))
    with pytest.raises(RuntimeError, match="未运行"):
        await proc.call("ping")


async def test_call_times_out_without_response():
    proc = _pp()
    proc._process = _FakeSubProcess()
    with pytest.raises(RuntimeError, match="超时"):
        await asyncio.wait_for(proc.call("ping"), 5)


async def test_call_round_trip_pairs_response_by_id():
    proc = _pp()
    stdin = _FakeStdin()
    reader = asyncio.StreamReader()
    proc._process = _FakeSubProcess(stdin=stdin)
    proc._process.stdout = reader
    proc._reader_task = asyncio.create_task(proc._read_stdout())
    await asyncio.sleep(0)  # reader 阻塞在 readline
    call_task = asyncio.create_task(proc.call("ping"))
    for _ in range(200):
        if stdin.written:
            break
        await asyncio.sleep(0.01)
    request = json.loads(stdin.written[0])
    assert request["method"] == "ping" and request["id"] == 2
    reader.feed_data(json.dumps({"id": 2, "result": {"ok": 1}}).encode() + b"\n")
    assert await asyncio.wait_for(call_task, 5) == {"ok": 1}
    reader.feed_eof()
    await asyncio.wait_for(proc._reader_task, 5)


async def test_read_stdout_skips_non_json_line():
    proc = _pp()
    reader = asyncio.StreamReader()
    proc._process = _FakeSubProcess()
    proc._process.stdout = reader
    proc._reader_task = asyncio.create_task(proc._read_stdout())
    await asyncio.sleep(0)
    fut = asyncio.get_running_loop().create_future()
    proc._pending[2] = fut
    reader.feed_data(b"totally not json\n")  # 解析失败 → continue
    reader.feed_data(json.dumps({"id": 2, "result": {"ok": 1}}).encode() + b"\n")
    assert await asyncio.wait_for(fut, 5) == {"ok": 1}
    reader.feed_eof()
    await asyncio.wait_for(proc._reader_task, 5)


async def test_read_stdout_error_response_fails_future():
    proc = _pp()
    reader = asyncio.StreamReader()
    proc._process = _FakeSubProcess()
    proc._process.stdout = reader
    proc._reader_task = asyncio.create_task(proc._read_stdout())
    await asyncio.sleep(0)
    fut = asyncio.get_running_loop().create_future()
    proc._pending[4] = fut
    reader.feed_data(json.dumps(
        {"id": 4, "error": {"code": -1, "message": "bad"}}).encode() + b"\n")
    with pytest.raises(RuntimeError, match="返回错误"):
        await asyncio.wait_for(fut, 5)
    reader.feed_eof()
    await asyncio.wait_for(proc._reader_task, 5)


async def test_read_stdout_dispatches_reverse_request():
    proc = _pp()
    proc._host_registry = HostMethodRegistry()
    dispatched: list = []

    async def handler(params):
        dispatched.append(params)
        return {"echo": params.get("x")}
    proc._host_registry.register("ha.get_states", handler)
    written: list[dict] = []

    async def fake_write(payload):
        written.append(payload)
    proc._write_line = fake_write
    reader = asyncio.StreamReader()
    proc._process = _FakeSubProcess()
    proc._process.stdout = reader
    proc._reader_task = asyncio.create_task(proc._read_stdout())
    reader.feed_data(json.dumps(
        {"jsonrpc": "2.0", "id": 7, "method": "ha.get_states",
         "params": {"x": 1}}).encode() + b"\n")
    for _ in range(100):
        if written:
            break
        await asyncio.sleep(0.01)
    assert dispatched == [{"x": 1, "_plugin_id": "p1"}]
    assert written[0]["result"] == {"echo": 1}
    assert written[0]["id"] == 7
    reader.feed_eof()
    await asyncio.wait_for(proc._reader_task, 5)


async def test_handle_reverse_without_registry():
    proc = _pp()
    written: list[dict] = []

    async def fake_write(payload):
        written.append(payload)
    proc._write_line = fake_write
    await proc._handle_reverse({"id": 3, "method": "ha.call_service",
                                "params": {}})
    assert written[0]["error"]["code"] == -32000
    assert "宿主未注入" in written[0]["error"]["message"]


async def test_handle_reverse_permission_denied():
    proc = _pp()
    proc._host_registry = HostMethodRegistry()
    proc._host_registry.register("ha.call_service", AsyncMock(return_value={}),
                                 required_permission="ha")
    written: list[dict] = []

    async def fake_write(payload):
        written.append(payload)
    proc._write_line = fake_write
    await proc._handle_reverse({"id": 5, "method": "ha.call_service",
                                "params": {}})
    assert written[0]["error"]["code"] == -32500


async def test_handle_reverse_generic_error_and_write_failure():
    proc = _pp()
    proc._host_registry = HostMethodRegistry()

    async def boom(params):
        raise ValueError("bad param")
    proc._host_registry.register("ha.call_service", boom)
    written: list[dict] = []

    async def failing_write(payload):
        raise RuntimeError("plugin gone")
    proc._write_line = failing_write
    await proc._handle_reverse({"id": 9, "method": "ha.call_service",
                                "params": {}})  # 写回失败被吞


async def test_drain_stderr_logs_lines():
    proc = _pp()
    proc._process = _FakeSubProcess(stderr_lines=[b"plugin stderr\n"])
    task = asyncio.create_task(proc._drain_stderr())
    await asyncio.wait_for(task, 5)


async def test_stop_returns_when_no_process():
    proc = _pp()
    await proc.stop()
    assert proc.is_alive is False


async def test_stop_graceful_full_flow():
    proc = _pp()
    stdin = _FakeStdin()
    proc._process = _FakeSubProcess(stdin=stdin)
    proc.call = AsyncMock(return_value={"ok": True})

    async def _hang():
        await asyncio.sleep(30)
    proc._reader_task = asyncio.create_task(_hang())
    proc._stderr_task = asyncio.create_task(_hang())
    loop = asyncio.get_running_loop()
    pending = loop.create_future()
    proc._pending[8] = pending
    stopped: list = []
    proc._on_stopped = stopped.append
    await asyncio.wait_for(proc.stop(), 10)
    assert stdin.closed is True
    assert proc._process.terminated is True
    assert pending.exception() is not None  # 在途请求被失败
    assert stopped == ["p1"]
    assert proc._reader_task is None and proc._stderr_task is None


async def test_stop_shutdown_call_failure_swallowed():
    proc = _pp()
    stdin = _FakeStdin(closing=True)
    proc._process = _FakeSubProcess(stdin=stdin)
    await asyncio.wait_for(proc.stop(), 10)  # shutdown call 抛 RuntimeError 被吞
    assert proc._process.terminated is True


async def test_stop_terminate_on_dead_process_and_on_stopped_error():
    proc = _pp()

    class _DeadProc(_FakeSubProcess):
        def terminate(self):
            raise ProcessLookupError("already dead")

    def _cb_fail(pid):
        raise RuntimeError("cb fail")

    proc._process = _DeadProc(stdin=_FakeStdin(closing=True))
    proc._on_stopped = _cb_fail
    # terminate 对已死进程抛 ProcessLookupError、on_stopped 抛错——都被吞掉
    await asyncio.wait_for(proc.stop(), 10)


async def test_stop_kill_after_wait_timeout():
    proc = _pp()
    fake = _FakeSubProcess(stdin=_FakeStdin(closing=True),
                           wait_times_out=True)
    proc._process = fake
    await asyncio.wait_for(proc.stop(), 10)
    assert fake.killed is True


def test_resolve_entry_and_project_root(tmp_path):
    proc = PluginProcess(manifest=_manifest("p1"), plugin_root=str(tmp_path))
    proc.manifest.entry = "sub/plugin.py"
    assert proc._resolve_entry() == str(tmp_path / "sub" / "plugin.py")
    from app.core.config import BASE_DIR
    assert PluginProcess._find_project_root() == BASE_DIR


# ===========================================================================
# 10) app/integration/sdk/stdio_runtime.py（fake stdin/stdout）
# ===========================================================================

class _FakeStdinBuffer:
    """线程安全 fake stdin：readline 在 executor 线程里阻塞读。"""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()

    def put_line(self, obj):
        self._q.put(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")

    def put_raw(self, data: bytes):
        self._q.put(data)

    def put_eof(self):
        self._q.put(b"")

    def readline(self, timeout=5.0):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return b""


class _FakeStdoutBuffer:
    def __init__(self):
        self.chunks: list[bytes] = []

    def write(self, data):
        self.chunks.append(data)

    def flush(self):
        pass

    def lines(self):
        return b"".join(self.chunks).decode("utf-8").splitlines()

    def parsed(self, idx):
        return json.loads(self.lines()[idx])

    async def wait_for_count(self, n, timeout=5.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while len(self.lines()) < n:
            if loop.time() > deadline:
                raise TimeoutError(f"only {len(self.lines())}/{n} lines")
            await asyncio.sleep(0.01)


class _ScriptedPlugin(IntegrationPlugin):
    async def handle(self, method, params):
        if method == "explode":
            raise RuntimeError("kaputt")
        return {"echo": method, "params": params}


def _patch_stdio(monkeypatch):
    fake_in = _FakeStdinBuffer()
    fake_out = _FakeStdoutBuffer()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=fake_in))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=fake_out))
    return fake_in, fake_out


async def test_stdio_runtime_end_to_end(monkeypatch):
    fake_in, fake_out = _patch_stdio(monkeypatch)
    plugin = _ScriptedPlugin()
    plugin.manifest = {"id": "echo2", "version": "9"}
    runtime = _StdioRuntime(plugin)
    fake_in.put_line({"jsonrpc": "2.0", "id": 2, "method": "handshake",
                      "params": {}})
    run_task = asyncio.create_task(runtime.run())
    await fake_out.wait_for_count(1)
    handshake = fake_out.parsed(0)
    assert handshake["id"] == 2
    assert handshake["result"] == {"plugin_id": "echo2", "plugin_version": "9",
                                   "ready": True}

    # 方向 2：host_call 发请求（奇数 id），响应按 id 配对
    hc = asyncio.create_task(runtime.host_call("ha.get_states", {"x": 1}))
    await fake_out.wait_for_count(2)
    req = fake_out.parsed(1)
    assert req["id"] % 2 == 1 and req["method"] == "ha.get_states"
    fake_in.put_line({"jsonrpc": "2.0", "id": req["id"],
                      "result": {"states": ["s1"]}})
    assert await asyncio.wait_for(hc, 5) == {"states": ["s1"]}

    # 方向 2 错误响应 → RuntimeError
    hc2 = asyncio.create_task(runtime.host_call("llm.chat", {}))
    await fake_out.wait_for_count(3)
    req2 = fake_out.parsed(2)
    fake_in.put_line({"jsonrpc": "2.0", "id": req2["id"],
                      "error": {"code": -32000, "message": "denied"}})
    with pytest.raises(RuntimeError, match="宿主反向调用错误"):
        await asyncio.wait_for(hc2, 5)

    # 未完成的反向调用在进程退出时被失败
    leftover = asyncio.create_task(runtime.host_call("sink.broadcast", {}))
    await fake_out.wait_for_count(4)
    fake_in.put_eof()
    await asyncio.wait_for(run_task, 5)
    with pytest.raises(RuntimeError, match="插件进程退出"):
        await asyncio.wait_for(leftover, 5)


async def test_stdio_runtime_plugin_exception_returns_error_name(monkeypatch):
    import contextlib
    fake_in, fake_out = _patch_stdio(monkeypatch)
    runtime = _StdioRuntime(_ScriptedPlugin())
    with contextlib.redirect_stderr(io.StringIO()):
        await runtime._handle_request(
            {"jsonrpc": "2.0", "id": 9, "method": "explode",
             "params": {"a": 1}})
    resp = await asyncio.wait_for(runtime._queue.get(), 5)
    assert resp == {"jsonrpc": "2.0", "id": 9, "result": {"error": "RuntimeError"}}


async def test_stdio_runtime_writer_serializes_utf8(monkeypatch):
    fake_in, fake_out = _patch_stdio(monkeypatch)
    runtime = _StdioRuntime(_ScriptedPlugin())
    await runtime._queue.put({"msg": "中文"})
    await runtime._queue.put(None)
    await runtime._writer()
    assert fake_out.lines() == ['{"msg": "中文"}']


async def test_stdio_runtime_host_call_timeout():
    runtime = _StdioRuntime(_ScriptedPlugin(), reverse_timeout=0.05)
    with pytest.raises(RuntimeError, match="反向调用 ha.get_states 超时"):
        await runtime.host_call("ha.get_states", {})


async def test_stdio_runtime_skips_invalid_line_and_tolerates_writer_failure(
        monkeypatch):
    fake_in = _FakeStdinBuffer()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=fake_in))

    class _BrokenOut:
        def write(self, data):
            raise RuntimeError("stdout closed")

        def flush(self):
            pass
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=_BrokenOut()))

    plugin = _ScriptedPlugin()
    plugin.manifest = {"id": "p", "version": "1"}
    runtime = _StdioRuntime(plugin)
    fake_in.put_raw(b"<not-json garbage>\n")  # 解析失败 → 跳过该行
    fake_in.put_line({"jsonrpc": "2.0", "id": 2, "method": "handshake",
                      "params": {}})
    fake_in.put_eof()
    # stdout write 抛错 → writer 任务失败 → run() 收尾吞掉异常
    await asyncio.wait_for(runtime.run(), 5)
    await asyncio.sleep(0.05)  # 让残留的 handle 任务收尾


async def test_run_stdio_plugin_applies_ui_config(monkeypatch, tmp_path):
    manifest = {
        "id": "cfgplug", "version": "1",
        "capabilities": [
            {"type": "output_sink", "id": "s",
             "config_schema": {"token": {"type": "string",
                                         "default": "origin"}}},
            {"type": "output_sink", "id": "s2", "config_schema": "not-a-dict"},
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("AETHER_PLUGIN_CONFIG",
                       json.dumps({"token": "new-token", "unknown": "x"}))
    fake_in = _FakeStdinBuffer()
    fake_in.put_eof()  # 立即 EOF → run() 马上收尾
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=fake_in))
    fake_out = _FakeStdoutBuffer()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=fake_out))

    created: dict = {}

    class _P(IntegrationPlugin):
        def setup(self, m):
            created["plugin"] = self

    await run_stdio_plugin(_P, str(path))
    plugin = created["plugin"]
    assert isinstance(plugin.host, HostProxy)
    caps = plugin.manifest["capabilities"]
    assert caps[0]["config_schema"]["token"]["default"] == "new-token"
    assert caps[1]["config_schema"] == "not-a-dict"  # 非 dict schema 跳过


async def test_run_stdio_plugin_without_ui_config(monkeypatch, tmp_path):
    monkeypatch.delenv("AETHER_PLUGIN_CONFIG", raising=False)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"id": "p", "version": "1",
                                "capabilities": []}), encoding="utf-8")
    fake_in = _FakeStdinBuffer()
    fake_in.put_eof()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=fake_in))
    fake_out = _FakeStdoutBuffer()
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=fake_out))
    created: dict = {}

    class _P(IntegrationPlugin):
        def setup(self, m):
            created["ok"] = True

    await run_stdio_plugin(_P, str(path))
    assert created["ok"] is True


# ===========================================================================
# 11) app/integration/sdk/plugin_base.py
# ===========================================================================

def _host_proxy():
    calls: list[tuple] = []

    async def host_call(method, params=None):
        calls.append((method, params))
        return {"method": method, "params": params}
    return HostProxy(host_call), calls


async def test_host_proxy_sub_agents():
    proxy, calls = _host_proxy()
    r = await proxy.ha.call_service("light", "turn_on", "light.bed",
                                    {"b": 1})
    assert r["method"] == "ha.call_service"
    assert calls[-1][1] == {"domain": "light", "service": "turn_on",
                            "entity_id": "light.bed", "data": {"b": 1}}
    assert (await proxy.ha.get_states())["method"] == "ha.get_states"
    assert (await proxy.ha.get_devices_grouped())["method"] == \
        "ha.get_devices_grouped"
    await proxy.llm.chat([{"role": "user", "content": "hi"}], 5)
    assert calls[-1][1] == {"messages": [{"role": "user", "content": "hi"}],
                            "timeout": 5}
    await proxy.camera.register({"w": 1})
    assert calls[-1][1] == {"spec": {"w": 1}}
    await proxy.camera.push_frame("cam", "Zml2")
    assert calls[-1][1] == {"camera_id": "cam", "jpeg_b64": "Zml2"}
    await proxy.camera.unregister()
    assert calls[-1][1] == {}
    await proxy.camera.set_flags("cam", {"night": True})
    assert calls[-1][1] == {"camera_id": "cam", "flags": {"night": True}}
    await proxy.broadcast("hello", "m1")
    assert calls[-1] == ("sink.broadcast", {"text": "hello", "msg_id": "m1"})


def _plugin_with_caps(caps=("output_sink",)) -> IntegrationPlugin:
    p = IntegrationPlugin()
    p.manifest = {"id": "p", "capabilities":
                  [{"type": c} for c in caps]}
    return p


async def test_handle_custom_method_priority():
    plugin = _plugin_with_caps()
    seen: list = []

    async def handler(params):
        seen.append(params)
        return {"custom": True, "got": params}
    plugin.register_method("videos.list", handler)
    result = await plugin.handle("videos.list", {"q": 2})
    assert result == {"custom": True, "got": {"q": 2}}
    assert seen == [{"q": 2}]


async def test_handle_shutdown_calls_hook_and_tolerates_errors(capsys):
    plugin = _plugin_with_caps()

    async def bad_shutdown():
        raise RuntimeError("cleanup exploded")
    plugin.on_shutdown = bad_shutdown
    result = await plugin.handle("shutdown", {})
    assert result == {"ok": True}
    err = capsys.readouterr().err
    assert "on_shutdown error" in err

    plugin2 = _plugin_with_caps()
    plugin2.manifest = {"id": "p2", "capabilities": []}
    assert await plugin2.handle("shutdown", {}) == {"ok": True}


class _FakeSink:
    def __init__(self):
        self.spoken = None
        self.interrupted = False

    async def speak(self, text, msg_id=""):
        self.spoken = (text, msg_id)
        return {"spoken": text}

    async def interrupt(self):
        self.interrupted = True
        return {"interrupted": True}


class _FakeRouter:
    def __init__(self):
        self.routed = None

    async def route(self, text):
        self.routed = text
        return {"route": text}


async def test_handle_speak_interrupt_route_with_and_without_caps():
    plugin = _plugin_with_caps()
    assert await plugin.handle("sink.speak", {"text": "t"}) == \
        {"error": "no sink registered"}
    assert await plugin.handle("sink.interrupt", {}) == \
        {"error": "no sink registered"}
    router_plugin = _plugin_with_caps(caps=("inbound_router",))
    assert "no router registered" in \
        (await router_plugin.handle("router.handle", {"text": "x"}))["error"]

    sink = _FakeSink()
    plugin.sinks = [sink]
    assert await plugin.handle("sink.speak",
                               {"text": "你好", "msg_id": "m"}) == \
        {"spoken": "你好"}
    assert sink.spoken == ("你好", "m")
    assert (await plugin.handle("sink.interrupt", {})) == \
        {"interrupted": True}

    router = _FakeRouter()
    router_plugin.routers = [router]
    assert await router_plugin.handle("router.handle", {"text": "开灯"}) == \
        {"route": "开灯"}
    assert router.routed == "开灯"


async def test_handle_unknown_method():
    plugin = _plugin_with_caps()
    assert await plugin.handle("no.such", {}) == \
        {"error": "unknown method: no.such"}


async def test_on_shutdown_default_noop():
    plugin = _plugin_with_caps()
    await plugin.on_shutdown()  # 不应抛出


def test_declared_capabilities_ignores_non_dict():
    plugin = _plugin_with_caps()
    plugin.manifest = {"capabilities": ["junk", {"type": "output_sink"}]}
    assert plugin._declared_capabilities() == {"output_sink"}


# ===========================================================================
# 12) app/integration/integration_layer.py
# ===========================================================================

async def test_layer_host_methods_dispatch(tmp_path):
    ha_client = MagicMock(call_service=AsyncMock(return_value={"ok": True}))
    ha_svc = MagicMock(get_states_snapshot=AsyncMock(return_value=[{"s": 1}]),
                       get_all_devices_grouped=AsyncMock(return_value={"d": []}))
    llm = MagicMock(chat=AsyncMock(return_value="hello"))
    cam = MagicMock(register_virtual_camera=AsyncMock(
                        return_value={"camera_id": "vcam_p1"}),
                    push_frame=MagicMock(return_value={"queued": True}),
                    unregister_plugin_cameras=AsyncMock(return_value=True),
                    set_virtual_flag=MagicMock(return_value=True))
    layer = IntegrationLayer(plugin_dir=str(tmp_path), host_deps={
        "ha_client": ha_client, "ha_service": ha_svc,
        "llm_chat_client": llm, "camera_manager": cam,
    })
    reg = layer._host_registry
    m = _manifest("p1", permissions=["ha", "llm", "broadcast", "camera"])

    r = await reg.dispatch(m, METHOD_HOST_HA_CALL,
                           {"domain": "light", "service": "turn_on",
                            "entity_id": "light.bed", "data": {"b": 1}})
    assert r == {"ok": True}
    ha_client.call_service.assert_awaited_once_with("light", "turn_on",
                                                    "light.bed", {"b": 1})
    assert await reg.dispatch(m, METHOD_HOST_HA_STATES, {}) == \
        {"states": [{"s": 1}]}
    assert await reg.dispatch(m, METHOD_HOST_HA_DEVICES, {}) == {"d": []}
    assert await reg.dispatch(m, METHOD_HOST_LLM_CHAT,
                              {"messages": [{"m": 1}], "timeout": 7}) == \
        {"text": "hello"}

    layer.sink_manager.broadcast = AsyncMock()
    assert await reg.dispatch(m, METHOD_HOST_BROADCAST,
                              {"text": "t", "msg_id": "m"}) == {"ok": True}
    layer.sink_manager.broadcast.assert_awaited_once_with("t", "m")

    assert await reg.dispatch(m, METHOD_HOST_CAM_REGISTER,
                              {"_plugin_id": "p1", "spec": {"w": 1}}) == \
        {"camera_id": "vcam_p1"}
    cam.register_virtual_camera.assert_awaited_with("p1", {"w": 1})
    assert await reg.dispatch(m, METHOD_HOST_CAM_PUSH,
                              {"camera_id": "c", "jpeg_b64": "Zg=="}) == \
        {"queued": True}
    cam.push_frame.assert_called_with("c", "Zg==")
    assert await reg.dispatch(m, METHOD_HOST_CAM_UNREGISTER,
                              {"plugin_id": "p2"}) == {"ok": True}
    cam.unregister_plugin_cameras.assert_awaited_with("p2")
    assert await reg.dispatch(m, METHOD_HOST_CAM_SET_FLAGS,
                              {"camera_id": "c", "key": "night",
                               "value": True}) == {"ok": True}


async def test_layer_cam_register_requires_identity(tmp_path):
    cam = MagicMock(register_virtual_camera=AsyncMock())
    layer = IntegrationLayer(plugin_dir=str(tmp_path),
                             host_deps={"camera_manager": cam})
    m = _manifest("p1", permissions=["camera"])
    with pytest.raises(RuntimeError, match="missing plugin identity"):
        await layer._host_registry.dispatch(m, METHOD_HOST_CAM_REGISTER,
                                            {"spec": {}})
    with pytest.raises(RuntimeError, match="missing plugin identity"):
        await layer._host_registry.dispatch(m, METHOD_HOST_CAM_UNREGISTER, {})


def test_layer_on_plugin_stopped_without_camera_manager(tmp_path):
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer._supervisor._on_plugin_stopped("p1")  # 早退，不抛


def test_layer_on_plugin_stopped_sync_context_warns(tmp_path):
    cam = MagicMock(unregister_plugin_cameras=AsyncMock())
    layer = IntegrationLayer(plugin_dir=str(tmp_path),
                             host_deps={"camera_manager": cam})
    layer._supervisor._on_plugin_stopped("p1")  # 无运行中 loop → 走告警分支


async def test_layer_on_plugin_stopped_unregisters_cameras(tmp_path):
    cam = MagicMock(unregister_plugin_cameras=AsyncMock())
    layer = IntegrationLayer(plugin_dir=str(tmp_path),
                             host_deps={"camera_manager": cam})
    layer._supervisor._on_plugin_stopped("p1")
    for _ in range(50):
        if cam.unregister_plugin_cameras.await_count:
            break
        await asyncio.sleep(0.01)
    cam.unregister_plugin_cameras.assert_awaited_with("p1")


async def test_layer_update_ha_refs_rebinds_handlers(tmp_path):
    old_client = MagicMock(call_service=AsyncMock(return_value={"old": True}))
    layer = IntegrationLayer(plugin_dir=str(tmp_path),
                             host_deps={"ha_client": old_client})
    new_client = MagicMock(call_service=AsyncMock(return_value={"new": True}))
    layer.update_ha_refs(new_client, MagicMock())
    m = _manifest("p1", permissions=["ha"])
    r = await layer._host_registry.dispatch(m, METHOD_HOST_HA_CALL, {})
    assert r == {"new": True}


def test_layer_update_ha_refs_noop_without_deps(tmp_path):
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer.update_ha_refs(MagicMock(), MagicMock())  # 无 host_deps → 直接返回


async def test_layer_start_filters_inprocess_and_disabled(tmp_path, monkeypatch):
    sub = _manifest("sub", caps=[Capability(type=CapabilityType.OUTPUT_SINK,
                                            id="s")])
    inproc = Manifest(id="inproc", name="inproc", version="1",
                      aether_api_version="1",
                      capabilities=[Capability(type=CapabilityType.MODEL_ADAPTER,
                                               id="m")])
    monkeypatch.setattr("app.integration.integration_layer.load_manifests",
                        lambda *a, **kw: [sub, inproc])
    monkeypatch.setattr("app.integration.config_helper.get_disabled_plugins",
                        lambda: [])
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer._supervisor.start_all = AsyncMock()
    await layer.start()
    args = layer._supervisor.start_all.await_args.args
    assert [m.id for m in args[0]] == ["sub"]  # 进程内插件不 spawn
    layer._supervisor.stop_all = AsyncMock()
    await layer.stop()
    layer._supervisor.stop_all.assert_awaited_once()


async def test_layer_list_plugins_merges_host_integrations(tmp_path,
                                                           monkeypatch):
    sub = _manifest("sub", caps=[
        Capability(type=CapabilityType.OUTPUT_SINK, id="s",
                   config_schema={"api_key": {"type": "secret"}}),
    ])
    monkeypatch.setattr("app.integration.manifest_loader.load_all_manifests",
                        lambda *a, **kw: [sub])
    monkeypatch.setattr("app.integration.config_helper.get_disabled_plugins",
                        lambda: [])
    monkeypatch.setattr("app.integration.config_helper.get_host_config",
                        lambda pid: {"api_key": "x"})
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    alive = SimpleNamespace(is_alive=True)
    layer._supervisor.get_process = lambda pid: alive if pid == "sub" else None
    layer.register_host_integration("feishu", {
        "name": "飞书", "description": "长连接", "alive": True,
        "capabilities": ["output_sink"], "version": "2.0"})
    plugins = layer.list_plugins()
    assert plugins[0] == {
        "id": "sub", "name": "sub", "version": "1", "description": "",
        "capabilities": ["output_sink"], "alive": True, "enabled": True,
        "config_schema": {"api_key": {"type": "secret"}},
        "has_config_set": True}
    assert plugins[1]["id"] == "feishu"
    assert plugins[1]["alive"] is True


async def test_layer_list_ui_contributions_skips_disabled(tmp_path,
                                                          monkeypatch):
    from app.integration.schema import UIContribution
    enabled = _manifest("on", ui=[UIContribution(
        slot="s", type="badge", props={"x": 1}, state_key="k", action="act")])
    monkeypatch.setattr("app.integration.integration_layer.load_manifests",
                        lambda *a, **kw: [enabled])
    monkeypatch.setattr("app.integration.config_helper.get_disabled_plugins",
                        lambda: [])
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    assert layer.list_ui_contributions() == [{
        "plugin_id": "on", "slot": "s", "type": "badge", "props": {"x": 1},
        "state_key": "k", "action": "act"}]


async def test_layer_route_inbound_paths(tmp_path, monkeypatch):
    router_manifest = _manifest("router", caps=[
        Capability(type=CapabilityType.INBOUND_ROUTER, id="r")])
    monkeypatch.setattr("app.integration.manifest_loader.load_manifests",
                        lambda *a, **kw: [router_manifest])
    monkeypatch.setattr("app.integration.config_helper.get_disabled_plugins",
                        lambda: [])
    layer = IntegrationLayer(plugin_dir=str(tmp_path))

    # 无存活进程 → no inbound router
    layer._supervisor.get_process = lambda pid: None
    assert await layer.route_inbound("开灯", "text") == \
        {"ok": False, "error": "no inbound router available"}

    # 存活 → RPC 转发
    alive = SimpleNamespace(is_alive=True,
                            call=AsyncMock(return_value={"ok": True,
                                                         "reply": "好"}))
    layer._supervisor.get_process = lambda pid: alive
    assert await layer.route_inbound("开灯", "text") == \
        {"ok": True, "reply": "好"}
    alive.call.assert_awaited_with("router.handle",
                                   {"text": "开灯", "mode": "text"})

    # RPC 异常 → ok False
    broken = SimpleNamespace(
        is_alive=True, call=AsyncMock(side_effect=RuntimeError("rpc down")))
    layer._supervisor.get_process = lambda pid: broken
    assert await layer.route_inbound("x", "text") == \
        {"ok": False, "error": "插件 router 路由失败"}


async def test_layer_restart_subprocess_plugin_paths(tmp_path, monkeypatch):
    sub = _manifest("sub", caps=[Capability(type=CapabilityType.OUTPUT_SINK,
                                            id="s")])
    inproc = Manifest(id="inproc", name="i", version="1",
                      aether_api_version="1",
                      capabilities=[Capability(type=CapabilityType.MODEL_ADAPTER,
                                               id="m")])
    monkeypatch.setattr("app.integration.manifest_loader.load_all_manifests",
                        lambda *a, **kw: [sub, inproc])
    disabled: list = []
    monkeypatch.setattr("app.integration.config_helper.get_disabled_plugins",
                        lambda: disabled)
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer._supervisor.stop_one = AsyncMock(return_value=True)
    layer._supervisor.start_one = AsyncMock(return_value=True)

    assert await layer.restart_subprocess_plugin("ghost") is False

    disabled.append("sub")
    assert await layer.restart_subprocess_plugin("sub") is True
    layer._supervisor.start_one.assert_not_awaited()

    disabled.clear()
    assert await layer.restart_subprocess_plugin("inproc") is True
    assert await layer.restart_subprocess_plugin("sub") is True
    layer._supervisor.start_one.assert_awaited_with(sub, str(tmp_path))

    layer._supervisor.start_one = AsyncMock(return_value=False)
    assert await layer.restart_subprocess_plugin("sub") is False


async def test_layer_start_and_stop_plugin(tmp_path, monkeypatch):
    sub = _manifest("sub", caps=[Capability(type=CapabilityType.OUTPUT_SINK,
                                            id="s")])
    inproc = Manifest(id="inproc", name="i", version="1",
                      aether_api_version="1",
                      capabilities=[Capability(type=CapabilityType.MODEL_ADAPTER,
                                               id="m")])
    monkeypatch.setattr("app.integration.manifest_loader.load_all_manifests",
                        lambda *a, **kw: [sub, inproc])
    disabled: list = []
    monkeypatch.setattr("app.integration.config_helper.get_disabled_plugins",
                        lambda: disabled)
    set_calls: list = []
    monkeypatch.setattr("app.integration.config_helper.set_plugin_disabled",
                        lambda pid, dis: set_calls.append((pid, dis)))
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer._supervisor.start_one = AsyncMock(return_value=True)
    layer._supervisor.stop_one = AsyncMock(return_value=True)

    assert await layer.start_plugin("ghost") is False
    assert await layer.start_plugin("inproc") is True  # 进程内 → 直接成功
    assert await layer.start_plugin("sub") is True
    layer._supervisor.start_one.assert_awaited_with(sub, str(tmp_path))
    assert set_calls[-1] == ("sub", False)

    assert await layer.stop_plugin("sub") is True
    assert set_calls[-1] == ("sub", True)
    layer._supervisor.stop_one.assert_awaited_with("sub")


async def test_layer_set_broadcast_enabled_persists_and_tolerates_failure(
        tmp_path, monkeypatch):
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    persisted: list = []
    monkeypatch.setattr("app.integration.config_helper.set_broadcast_enabled",
                        lambda v: persisted.append(v))
    layer.set_broadcast_enabled(False)
    assert layer.sink_manager.broadcast_enabled is False
    assert persisted == [False]

    def _boom(v):
        raise RuntimeError("disk full")
    monkeypatch.setattr("app.integration.config_helper.set_broadcast_enabled",
                        _boom)
    layer.set_broadcast_enabled(True)  # 持久化失败只告警
    assert layer.sink_manager.broadcast_enabled is True


async def test_layer_set_plugin_enabled_refresh_failure_tolerated(
        tmp_path, monkeypatch):
    monkeypatch.setattr("app.integration.config_helper.set_plugin_disabled",
                        lambda pid, dis: None)
    layer = IntegrationLayer(plugin_dir=str(tmp_path))

    def _boom(*a, **kw):
        raise RuntimeError("adapter registry broken")
    monkeypatch.setattr(
        "app.agents.model_family_adapters.refresh_plugin_adapters", _boom)
    layer.set_plugin_enabled("p", True)  # 刷新失败只记 debug


async def test_layer_set_plugin_enabled_refresh_success(tmp_path, monkeypatch):
    monkeypatch.setattr("app.integration.config_helper.set_plugin_disabled",
                        lambda pid, dis: None)
    calls: list = []
    monkeypatch.setattr(
        "app.agents.model_family_adapters.refresh_plugin_adapters",
        lambda *a, **kw: calls.append(1))
    layer = IntegrationLayer(plugin_dir=str(tmp_path))
    layer.set_plugin_enabled("p", True)
    assert calls == [1]


# ===========================================================================
# 13) app/integration/sink_manager.py
# ===========================================================================

async def test_sink_manager_logs_and_continues_when_sink_fails():
    m1 = _manifest("s1", caps=[Capability(type=CapabilityType.OUTPUT_SINK,
                                          id="o")])
    m2 = _manifest("s2", caps=[Capability(type=CapabilityType.OUTPUT_SINK,
                                          id="o")])
    broken = SimpleNamespace(is_alive=True,
                             call=AsyncMock(side_effect=RuntimeError("boom")))
    healthy = SimpleNamespace(is_alive=True, call=AsyncMock(return_value={}))

    class _Sup:
        def get_running_manifests(self):
            return [m1, m2]

        def get_process(self, pid):
            return broken if pid == "s1" else healthy

    manager = SinkManager(_Sup())
    await manager.broadcast("你好", "m1")  # 单个失败不影响其他
    healthy.call.assert_awaited_with("sink.speak",
                                     {"text": "你好", "msg_id": "m1"})

    broken2 = SimpleNamespace(is_alive=True,
                              call=AsyncMock(side_effect=RuntimeError("x")))
    healthy2 = SimpleNamespace(is_alive=True, call=AsyncMock(return_value={}))

    class _Sup2:
        def get_running_manifests(self):
            return [m1, m2]

        def get_process(self, pid):
            return broken2 if pid == "s1" else healthy2

    manager2 = SinkManager(_Sup2())
    await manager2.interrupt_all()
    healthy2.call.assert_awaited_with("sink.interrupt", {})


async def test_sink_manager_ignores_non_sink_and_dead_processes():
    sink_m = _manifest("s", caps=[Capability(type=CapabilityType.OUTPUT_SINK,
                                             id="o")])
    other_m = _manifest("other")
    dead = SimpleNamespace(is_alive=False, call=AsyncMock())

    class _Sup:
        def get_running_manifests(self):
            return [other_m, sink_m]

        def get_process(self, pid):
            return dead if pid == "s" else None

    manager = SinkManager(_Sup())
    await manager.broadcast("x")     # sink 死 → 无目标，静默
    await manager.interrupt_all()
    dead.call.assert_not_awaited()


# ===========================================================================
# 14) app/integration/manifest_loader.py
# ===========================================================================

_VALID = {"id": "ok", "name": "OK", "version": "1", "aether_api_version": "1"}


def _write_plugin(root: Path, pid: str, raw: str | None = None,
                  plugin_id: str | None = None):
    d = root / pid
    d.mkdir(parents=True)
    mp = d / "manifest.json"
    if raw is not None:
        mp.write_text(raw, encoding="utf-8")
    else:
        mp.write_text(json.dumps(dict(_VALID, id=plugin_id or pid)),
                      encoding="utf-8")
    return mp


def test_manifest_loader_skips_broken_entries(tmp_path):
    _write_plugin(tmp_path, "badjson", raw="{oops")
    _write_plugin(tmp_path, "badmodel",
                  raw=json.dumps({"id": "x"}))  # 缺 name/version
    (tmp_path / "nodir").mkdir()
    (tmp_path / "loose.txt").write_text("not a dir", encoding="utf-8")
    result = load_manifests(str(tmp_path))
    assert result == []


def test_manifest_loader_api_version_mismatch(tmp_path):
    bad = dict(_VALID, aether_api_version="9")
    _write_plugin(tmp_path, "future", raw=json.dumps(bad))
    assert load_manifests(str(tmp_path), api_version="1") == []
    assert [m.id for m in load_manifests(str(tmp_path), api_version="9")] == \
        ["ok"]

def test_manifest_loader_disabled_filter(tmp_path):
    _write_plugin(tmp_path, "a")
    _write_plugin(tmp_path, "b")
    ids = [m.id for m in load_manifests(str(tmp_path), disabled=["b"])]
    assert ids == ["a"]
    all_ids = [m.id for m in load_all_manifests(str(tmp_path))]
    assert sorted(all_ids) == ["a", "b"]


def test_manifest_loader_cache_reuses_and_rescans(tmp_path):
    mp = _write_plugin(tmp_path, "p1")
    first = load_all_manifests(str(tmp_path))
    second = load_all_manifests(str(tmp_path))
    assert first is second  # 指纹未变 → 命中缓存（同一对象）
    changed = dict(_VALID, id="p1b")
    mp.write_text(json.dumps(changed), encoding="utf-8")
    st = mp.stat()
    import os
    os.utime(mp, ns=(st.st_mtime_ns + 10 ** 9, st.st_mtime_ns + 10 ** 9))
    third = load_all_manifests(str(tmp_path))
    assert [m.id for m in third] == ["p1b"]


def test_manifest_loader_missing_dir(tmp_path):
    assert load_manifests(str(tmp_path / "nope")) == []


# ===========================================================================
# 15) app/integration/config_helper.py
# ===========================================================================

def test_config_helper_roundtrip():
    from app.integration import config_helper as ch
    assert ch.get_broadcast_enabled() is True
    ch.set_broadcast_enabled(False)
    assert ch.get_broadcast_enabled() is False

    assert ch.get_disabled_plugins() == []
    assert ch.set_plugin_disabled("a", True) == ["a"]
    assert ch.set_plugin_disabled("a", True) == ["a"]  # 幂等
    assert ch.set_plugin_disabled("a", False) == []

    assert ch.get_current_mode() == "aether"
    ch.set_current_mode("direct")
    assert ch.get_current_mode() == "direct"


def test_config_helper_host_config_merge_and_secret_keep():
    from app.integration import config_helper as ch
    ch.set_host_config("plug", {"token": "old-secret", "region": "cn"})
    assert ch.get_host_config("plug") == {"token": "old-secret",
                                          "region": "cn"}
    merged = ch.merge_plugin_config(
        "plug", {"token": "", "region": "us", "extra": "1"},
        secret_keys={"token"})
    assert merged == {"token": "old-secret", "region": "us", "extra": "1"}
    # 其他插件的配置不受影响（深合并）
    ch.set_host_config("other", {"k": "v"})
    assert ch.get_host_config("plug")["region"] == "us"


def test_config_helper_non_dict_host_config_returns_empty(monkeypatch):
    import app.core.config as cfg
    from app.integration import config_helper as ch
    monkeypatch.setattr(cfg, "CONFIG",
                        {"integration": {"host_configs": {"p": "junk"}}})
    assert ch.get_host_config("p") == {}


# ===========================================================================
# 16) app/services/ha_service.py
# ===========================================================================

def _svc(states=None) -> tuple[object, MagicMock]:
    client = MagicMock()
    client.get_states = AsyncMock(return_value=states or [])
    client.base_url = "http://127.0.0.1:8123"
    client.token = "tok"
    from app.services.ha_service import HAService
    return HAService(client=client), client


class _FakeDB:
    def __init__(self, alias=None, raise_on_read=False):
        self._alias = alias or {}
        self._raise = raise_on_read
        self.reads = 0

    async def prefs_get_by_scope(self, scope):
        self.reads += 1
        if self._raise:
            raise RuntimeError("db closed")
        return self._alias


def _patch_db(monkeypatch, fake):
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod.Database, "get", classmethod(lambda cls: fake))


async def test_alias_map_reads_db_once_and_caches(monkeypatch):
    svc, _ = _svc()
    fake = _FakeDB({"light.bed": "床头灯"})
    _patch_db(monkeypatch, fake)
    assert await svc._get_alias_map() == {"light.bed": "床头灯"}
    assert await svc._get_alias_map() == {"light.bed": "床头灯"}  # 命中缓存
    assert fake.reads == 1


async def test_alias_map_db_failure_returns_stale(monkeypatch):
    svc, _ = _svc()
    svc._alias_map = {"keep": "me"}
    _patch_db(monkeypatch, _FakeDB(raise_on_read=True))
    assert await svc._get_alias_map() == {"keep": "me"}


async def test_get_states_snapshot_and_name_map():
    states = [{"entity_id": "light.bed", "state": "on",
               "attributes": {"friendly_name": "Bed"}}]
    svc, client = _svc(states)
    assert await svc.get_states_snapshot() == states
    assert await svc.get_entity_name_map() == {"light.bed": "Bed"}
    client.get_states.assert_awaited_once()  # 第二次命中缓存


async def test_get_areas_from_registry_cache():
    svc, _ = _svc()
    svc._area_map = {"a1": "客厅", "a2": "卧室"}
    svc._registry_cache_at = 9e9
    assert await svc.get_areas() == [{"area_id": "a1", "name": "客厅"},
                                     {"area_id": "a2", "name": "卧室"}]


class _FakeWS:
    def __init__(self, results, fail_types=(), pre=()):
        self._results = results
        self._fail = set(fail_types)
        self._queue: list = [json.dumps(m) for m in pre]
        self.sent: list = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if "id" not in msg:
            return
        mt = msg["type"]
        if mt in self._fail:
            self._queue.append(json.dumps(
                {"id": msg["id"], "type": "result", "success": False,
                 "error": {"code": "x", "message": "boom"}}))
        else:
            self._queue.append(json.dumps(
                {"id": msg["id"], "type": "result", "success": True,
                 "result": self._results[mt]}))

    async def recv(self):
        if not self._queue:
            raise RuntimeError("ws closed early")
        return self._queue.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _MismatchWS(_FakeWS):
    """永远回错 id 的 ws：call() 重试 20 次后放弃。"""

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if "id" in msg:
            # 预填 25 条错 id 响应，保证 20 次重试全部不匹配
            for i in range(25):
                self._queue.append(json.dumps(
                    {"id": msg["id"] + 100 + i, "type": "result",
                     "success": True, "result": {}}))


async def test_refresh_registry_no_matching_response(monkeypatch):
    ws = _MismatchWS({"config/area_registry/list": []},
                     pre=[{"type": "auth_required"}, {"type": "auth_ok"}])
    monkeypatch.setitem(sys.modules, "websockets", _FakeWebsockets(ws))
    svc, _ = _svc()
    await svc._refresh_registry()
    assert svc._registry_cache_at == 0.0  # 失败不缓存
    assert len(ws.sent) == 2  # auth + 1 个 registry 请求（放弃后续）


class _FakeWebsockets:
    def __init__(self, ws):
        self._ws = ws
        self.url = None
        self.headers = None

    def connect(self, url, additional_headers=None):
        self.url = url
        self.headers = additional_headers
        return self._ws


async def test_refresh_registry_success_with_device_area_inheritance(
        monkeypatch):
    results = {
        "config/area_registry/list": [{"area_id": "a1", "name": "客厅"},
                                      {"area_id": "a2", "name": "卧室"}],
        "config/device_registry/list": [
            {"id": "d1", "name": "Generic", "name_by_user": "我的网关",
             "model": "M1", "manufacturer": "Xiaomi", "sw_version": "1.2",
             "area_id": "a2"},
            {"id": "d2", "name": "D2", "area_id": None},
        ],
        "config/entity_registry/list": [
            {"entity_id": "light.tv", "area_id": "a1", "device_id": "d1"},
            {"entity_id": "sensor.t", "area_id": None, "device_id": "d1"},
            {"entity_id": "cover.c", "area_id": None, "device_id": "d2"},
            {"entity_id": "switch.x", "area_id": None, "device_id": None},
        ],
    }
    # pre: auth_required + auth_ok + 事件推送（验证 call() 会跳过不匹配 id 的帧）
    ws = _FakeWS(results, pre=[{"type": "auth_required"},
                               {"type": "auth_ok"},
                               {"type": "event", "event": {}}])
    mod = _FakeWebsockets(ws)
    monkeypatch.setitem(sys.modules, "websockets", mod)
    svc, _ = _svc()
    await svc._refresh_registry()
    assert mod.url == "ws://127.0.0.1:8123/api/websocket"
    assert mod.headers["Authorization"] == "Bearer tok"
    assert svc._area_map == {"a1": "客厅", "a2": "卧室"}
    assert svc._device_info_map["d1"]["name"] == "我的网关"  # name_by_user 优先
    assert svc._entity_device_map["cover.c"] == "d2"
    assert svc._entity_area_map == {"light.tv": "a1", "sensor.t": "a2"}
    assert svc._registry_cache_at > 0


async def test_refresh_registry_auth_failure_keeps_empty(monkeypatch):
    ws = _FakeWS({}, pre=[{"type": "auth_required"},
                          {"type": "auth_invalid"}])
    monkeypatch.setitem(sys.modules, "websockets", _FakeWebsockets(ws))
    svc, _ = _svc()
    await svc._refresh_registry()
    assert svc._area_map == {}
    assert svc._registry_cache_at == 0.0


async def test_refresh_registry_subcall_failure(monkeypatch):
    results = {"config/area_registry/list": [{"area_id": "a1", "name": "x"}]}
    ws = _FakeWS(results, fail_types={"config/device_registry/list"},
                 pre=[{"type": "auth_required"}, {"type": "auth_ok"}])
    monkeypatch.setitem(sys.modules, "websockets", _FakeWebsockets(ws))
    svc, _ = _svc()
    await svc._refresh_registry()
    assert svc._area_map == {"a1": "x"}  # areas 已填充
    assert svc._device_info_map == {}
    assert svc._registry_cache_at == 0.0  # 失败不缓存


async def test_refresh_registry_timeout(monkeypatch):
    class _TimeoutMod:
        def connect(self, url, additional_headers=None):
            raise asyncio.TimeoutError()
    monkeypatch.setitem(sys.modules, "websockets", _TimeoutMod())
    svc, _ = _svc()
    await svc._refresh_registry()
    assert svc._registry_cache_at == 0.0


async def test_get_all_devices_alias_priority(monkeypatch):
    states = [{"entity_id": "light.bed", "state": "on",
               "attributes": {"friendly_name": "HA Name"}}]
    svc, _ = _svc(states)
    svc._area_map = {"a1": "卧室"}
    svc._entity_area_map = {"light.bed": "a1"}
    svc._registry_cache_at = 9e9
    _patch_db(monkeypatch, _FakeDB({"light.bed": "我的床灯"}))
    devices = await svc.get_all_devices()
    assert devices[0]["name"] == "我的床灯"  # 别名优先于 friendly_name
    assert devices[0]["area_name"] == "卧室"


async def test_get_all_devices_grouped_structure(monkeypatch):
    states = [
        {"entity_id": "light.tv", "state": "on",
         "attributes": {"friendly_name": "TV 灯"}},
        {"entity_id": "sensor.temp", "state": "23",
         "attributes": {"friendly_name": "温度"}},
        {"entity_id": "climate.ac", "state": "cool",
         "attributes": {"friendly_name": "空调"}},
        {"entity_id": "fan.ceiling", "state": "on",
         "attributes": {"friendly_name": "吊扇"}},
        {"entity_id": "cover.c1", "state": "open",
         "attributes": {"friendly_name": "窗帘"}},
        {"entity_id": "sensor.solo", "state": "1",
         "attributes": {"friendly_name": "独立"}},
        {"entity_id": "sun.sun", "state": "up", "attributes": {}},
        {"entity_id": "sensor.noarea", "state": "1", "attributes": {}},
    ]
    svc, _ = _svc(states)
    svc._area_map = {"a1": "客厅"}
    svc._entity_area_map = {
        "light.tv": "a1", "sensor.temp": "a1", "climate.ac": "a1",
        "fan.ceiling": "a1", "cover.c1": "a1", "sensor.solo": "a1"}
    svc._device_info_map = {
        "d1": {"name": "", "model": "M1", "manufacturer": "X", "sw_version": "9"},
        "d2": {"name": "空调主机", "model": None, "manufacturer": None,
               "sw_version": None},
    }
    svc._entity_device_map = {
        "light.tv": "d1", "sensor.temp": "d1",
        "climate.ac": "d2", "fan.ceiling": "d2",
        "cover.c1": None, "sensor.solo": None}
    svc._registry_cache_at = 9e9
    _patch_db(monkeypatch, _FakeDB({"light.tv": "电视灯"}))

    result = await svc.get_all_devices_grouped()
    devices = {d["device_id"]: d for d in result["devices"]}

    d1 = devices["d1"]
    assert d1["name"] == "TV 灯"  # device name 为空 → 首实体 friendly_name
    assert d1["model"] == "M1"
    assert d1["controllable_count"] == 1
    assert d1["summary"] == "TV 灯"  # 单一可控实体 → 设备名
    assert d1["entities"][0]["name"] == "电视灯"  # 别名优先

    d2 = devices["d2"]
    assert d2["summary"] == "2个可控功能"  # climate + fan
    assert d2["area_name"] == "客厅"

    v = devices["virtual:cover.c1"]
    assert v["name"] == "窗帘" and v["model"] is None  # 虚拟设备用实体信息

    solo = devices["virtual:sensor.solo"]
    assert solo["summary"] == "属性查看"  # 纯诊断设备
    assert solo["controllable_count"] == 0

    # 稳定排序：区域名 → 设备名
    names = [d["name"] for d in result["devices"]]
    assert names == sorted(names, key=lambda n: n)


async def test_get_service_defs_filters_and_requires():
    ha_client = MagicMock(get_services=AsyncMock(return_value=[
        {"domain": "light", "services": {
            "turn_on": {"fields": {"brightness": {},
                                   "transition": {"required": True}}}}},
        {"domain": "media_player", "services": {
            "play": {"fields": {}}}},
    ]))
    from app.services.ha_service import HAService
    info = await HAService.get_service_defs(ha_client,
                                            domains={"light"},
                                            include_required=True)
    assert info == {"light": {"turn_on": {
        "fields": ["brightness", "transition"],
        "required": ["transition"]}}}
    full = await HAService.get_service_defs(ha_client)
    assert set(full.keys()) == {"light", "media_player"}
    assert full["media_player"]["play"] == {"fields": []}


async def test_get_service_defs_swallows_errors():
    ha_client = MagicMock(get_services=AsyncMock(
        side_effect=RuntimeError("ha offline")))
    from app.services.ha_service import HAService
    assert await HAService.get_service_defs(ha_client) == {}


# ===========================================================================
# 17) app/routes/integration_routes.py
# ===========================================================================

from app.routes import integration_routes as routes_mod
from app.routes.integration_routes import (
    BroadcastRequest,
    PluginConfigRequest,
    PluginMethodRequest,
    call_plugin_method,
    delete_plugin,
    export_plugin,
    get_plugin_config,
    get_state,
    invoke_action,
    list_integrations,
    list_ui_contributions,
    save_plugin_config,
    toggle_broadcast,
    toggle_plugin_enabled,
    upload_plugin,
    upload_plugin_file,
    _mask_secret,
    _sanitize_filename,
)


def _container(layer):
    c = MagicMock()
    c.integration_layer = layer
    return c


async def test_list_integrations_layer_none():
    result = await list_integrations(container=_container(None))
    assert result == {"success": True,
                      "data": {"plugins": [], "enabled": False,
                               "broadcast_enabled": False}}


async def test_toggle_plugin_enabled_layer_none():
    result = await toggle_plugin_enabled("p", container=_container(None),
                                         admin={})
    assert result == {"success": False, "message": "集成平台未启用"}


def test_mask_secret_rules():
    assert _mask_secret("") == ""
    assert _mask_secret("short") == "***"
    assert _mask_secret("abcd1234efgh") == "abcd…efgh"


async def test_get_plugin_config_masks_secrets():
    from app.integration import config_helper as ch
    ch.set_host_config("p1", {"token": "abcd1234efgh", "region": "cn"})
    layer = MagicMock()
    layer.list_plugins.return_value = [{
        "id": "p1",
        "config_schema": {"token": {"type": "secret"},
                          "region": {"type": "string"}}}]
    result = await get_plugin_config("p1", container=_container(layer))
    assert result["success"] is True
    assert result["data"]["values"]["token"] == \
        {"is_set": True, "masked": "abcd…efgh"}
    assert result["data"]["values"]["region"] == "cn"
    assert result["data"]["has_config_set"] is True


async def test_get_plugin_config_layer_none_and_unknown():
    assert (await get_plugin_config("p", container=_container(None)))[
        "success"] is False
    layer = MagicMock()
    layer.list_plugins.return_value = []
    result = await get_plugin_config("ghost", container=_container(layer))
    assert result == {"success": False, "message": "未知插件: ghost"}


async def test_save_plugin_config_missing_required():
    layer = MagicMock()
    layer.list_plugins.return_value = [{
        "id": "p1",
        "config_schema": {"token": {"type": "secret", "required": True}}}]
    result = await save_plugin_config(
        "p1", PluginConfigRequest(values={}), container=_container(layer),
        admin={"username": "op"})
    assert result["success"] is False
    assert "必填字段未填写" in result["message"]


async def test_save_plugin_config_non_secret_required_empty():
    layer = MagicMock()
    layer.list_plugins.return_value = [{
        "id": "p1", "config_schema": {"region": {"required": True}}}]
    result = await save_plugin_config(
        "p1", PluginConfigRequest(values={"region": "  "}),
        container=_container(layer), admin={"username": "op"})
    assert result["success"] is False


async def test_save_plugin_config_success_subprocess(monkeypatch):
    from app.integration import config_helper as ch
    ch.set_host_config("p1", {"token": "keep-me"})
    layer = MagicMock()
    layer.list_plugins.return_value = [{
        "id": "p1",
        "config_schema": {"region": {"required": True},
                          "token": {"type": "secret"}}}]
    layer.host_integrations = {}
    layer.restart_subprocess_plugin = AsyncMock(return_value=True)
    recorded: list = []
    monkeypatch.setattr("app.ops.audit.record",
                        lambda op, act, detail=None:
                        recorded.append((op, act, detail)))
    result = await save_plugin_config(
        "p1", PluginConfigRequest(values={"region": "us", "junk": "x",
                                          "token": ""}),
        container=_container(layer), admin={"user_id": "u7"})
    assert result["success"] is True
    assert result["data"]["applied"] == "restarted"
    op, act, detail = recorded[0]
    assert op == "u7" and act == "plugin_config"
    assert detail["fields"] == ["region", "token"]  # junk 被丢弃
    assert detail["applied"] == "restarted"
    # secret 留空 → 保留原值
    assert ch.get_host_config("p1")["token"] == "keep-me"

    layer.restart_subprocess_plugin = AsyncMock(return_value=False)
    result2 = await save_plugin_config(
        "p1", PluginConfigRequest(values={"region": "eu"}),
        container=_container(layer), admin={"username": "op"})
    assert result2["data"]["applied"] == "not_found"


async def test_save_plugin_config_host_integration_paths(monkeypatch):
    layer = MagicMock()
    layer.list_plugins.return_value = [{
        "id": "feishu", "config_schema": {"app_id": {}}}]
    layer.host_integrations = {"feishu": {}}
    recorded: list = []

    monkeypatch.setattr("app.ops.audit.record",
                        lambda op, act, detail=None:
                        recorded.append(detail))
    container = SimpleNamespace(integration_layer=layer,
                                restart_host_integration_fn=
                                MagicMock(return_value=True))
    r = await save_plugin_config("feishu",
                                 PluginConfigRequest(values={"app_id": "1"}),
                                 container=container, admin={"username": "o"})
    assert r["data"]["applied"] == "restarted"

    container2 = SimpleNamespace(
        integration_layer=layer,
        restart_host_integration_fn=MagicMock(return_value=False))
    r2 = await save_plugin_config("feishu",
                                  PluginConfigRequest(values={"app_id": "1"}),
                                  container=container2,
                                  admin={"username": "o"})
    assert r2["data"]["applied"] == "not_found"

    container3 = SimpleNamespace(integration_layer=layer)  # fn 缺失 → skipped
    r3 = await save_plugin_config("feishu",
                                  PluginConfigRequest(values={"app_id": "1"}),
                                  container=container3,
                                  admin={"username": "o"})
    assert r3["data"]["applied"] == "skipped"


async def test_save_plugin_config_layer_none_and_unknown():
    assert (await save_plugin_config(
        "p", PluginConfigRequest(), container=_container(None),
        admin={}))["success"] is False
    layer = MagicMock()
    layer.list_plugins.return_value = []
    assert (await save_plugin_config(
        "g", PluginConfigRequest(), container=_container(layer),
        admin={}))["success"] is False


async def test_call_plugin_method_paths():
    # 框架方法拒绝（在 layer 检查之前）
    r = await call_plugin_method("p", "sink.speak", req=None,
                                 container=_container(None), admin={})
    assert r["success"] is False and "框架方法" in r["message"]
    # layer None
    r = await call_plugin_method("p", "videos.list", req=None,
                                 container=_container(None), admin={})
    assert r == {"success": False, "message": "集成平台未启用"}
    # 未运行
    sup = MagicMock()
    sup.get_process.return_value = None
    layer = MagicMock()
    layer._supervisor = sup
    r = await call_plugin_method("p", "videos.list", req=None,
                                 container=_container(layer), admin={})
    assert "未运行" in r["message"]
    # 成功调用（params 透传 + req=None → {}）
    proc = SimpleNamespace(is_alive=True,
                           call=AsyncMock(return_value={"videos": []}))
    sup.get_process.return_value = proc
    r = await call_plugin_method("p", "videos.list",
                                 PluginMethodRequest(params={"q": 1}),
                                 container=_container(layer), admin={})
    assert r == {"success": True, "data": {"videos": []}}
    proc.call.assert_awaited_with("videos.list", {"q": 1})
    r2 = await call_plugin_method("p", "videos.list", req=None,
                                  container=_container(layer), admin={})
    assert r2["success"] is True
    proc.call.assert_awaited_with("videos.list", {})
    # 调用失败
    bad = SimpleNamespace(is_alive=True,
                          call=AsyncMock(side_effect=RuntimeError("rpc boom")))
    sup.get_process.return_value = bad
    r_bad = await call_plugin_method("p", "videos.list", req=None,
                                     container=_container(layer), admin={})
    assert r_bad["success"] is False
    assert "rpc boom" in r_bad["message"]
    # 未运行
    dead = SimpleNamespace(is_alive=False, call=AsyncMock())
    sup.get_process.return_value = dead
    r3 = await call_plugin_method("p", "videos.list", req=None,
                                  container=_container(layer), admin={})
    assert "未运行" in r3["message"]


async def test_get_state_and_invoke_action_current_mode(monkeypatch):
    layer = MagicMock()
    layer.sink_manager.broadcast_enabled = True
    r = await get_state("current_mode", container=_container(layer))
    assert r == {"success": True, "data": {"value": "aether"}}

    from app.integration import config_helper as ch
    monkeypatch.setattr(ch, "get_current_mode",
                        lambda: (_ for _ in ()).throw(RuntimeError("nope")))
    r2 = await get_state("current_mode", container=_container(layer))
    assert r2["data"]["value"] == "aether"  # 异常回退

    r3 = await invoke_action("set_mode", {"mode": "direct"},
                             container=_container(layer))
    assert r3["data"]["current_mode"] == "direct"

    r4 = await invoke_action("set_mode", "not-a-dict",
                             container=_container(layer))
    assert r4["data"]["current_mode"] == "aether"


async def test_upload_helpers():
    assert _sanitize_filename("a/b\\c:d*e?.txt") != "/"  # 剥路径分隔符
    assert _sanitize_filename("") == "upload.bin"
    assert _sanitize_filename("x" * 500) == "x" * 120  # 截断
    assert len(_sanitize_filename("../etc/passwd")) < len("../etc/passwd")


async def test_upload_plugin_file_success_and_errors(tmp_path, monkeypatch):
    plugin_root = tmp_path / "integrations"
    (plugin_root / "p1").mkdir(parents=True)
    monkeypatch.setattr(routes_mod, "get_config",
                        lambda p, d=None: str(plugin_root))
    monkeypatch.setattr(routes_mod, "BASE_DIR", tmp_path)

    from fastapi import UploadFile
    uf = UploadFile(file=io.BytesIO(b"hello"), filename="my file.txt")
    result = await upload_plugin_file("p1", file=uf, admin={})
    assert result["success"] is True
    assert result["data"]["size"] == 5
    assert result["data"]["name"] == "my file.txt"
    stored = Path(result["data"]["path"])
    assert stored.exists() and stored.read_bytes() == b"hello"

    # 非法 id / 插件不存在
    uf2 = UploadFile(file=io.BytesIO(b"x"), filename="a.txt")
    assert (await upload_plugin_file("../evil", file=uf2, admin={}))[  # type: ignore[arg-type]
        "success"] is False
    uf3 = UploadFile(file=io.BytesIO(b"x"), filename="a.txt")
    assert (await upload_plugin_file("ghost", file=uf3, admin={}))[  # type: ignore[arg-type]
        "success"] is False

    # 读取失败 → 清理半截文件
    uf4 = UploadFile(file=io.BytesIO(b"x"), filename="bad.bin")

    async def _boom_read(n):
        raise RuntimeError("disk full")
    monkeypatch.setattr(uf4, "read", _boom_read)
    result4 = await upload_plugin_file("p1", file=uf4, admin={})
    assert result4["success"] is False
    assert "disk full" in result4["message"]
    leftovers = list((tmp_path / "app" / "data" / "uploads" / "p1").glob(
        "*bad.bin*"))
    assert leftovers == []


def _zip_bytes(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


_VALID_UPLOAD = {"id": "plugnew", "name": "P", "version": "1.0",
                 "aether_api_version": "1", "entry": "plugin.py"}


def _upload_route(monkeypatch, tmp_path):
    plugin_root = tmp_path / "integrations"
    plugin_root.mkdir(exist_ok=True)
    monkeypatch.setattr(routes_mod, "get_config",
                        lambda p, d=None: str(plugin_root))
    monkeypatch.setattr(routes_mod, "BASE_DIR", tmp_path)
    return plugin_root


async def test_upload_plugin_rejects_oversize(tmp_path, monkeypatch):
    _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile
    big = b"\0" * (50 * 1024 * 1024 + 1)
    uf = UploadFile(file=io.BytesIO(big), filename="p.zip")
    result = await upload_plugin(file=uf, admin={})
    assert result["success"] is False and "过大" in result["message"]


async def test_upload_plugin_rejects_bad_zip_and_missing_manifest(
        tmp_path, monkeypatch):
    _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile
    bad = UploadFile(file=io.BytesIO(b"definitely not a zip"),
                     filename="p.zip")
    r = await upload_plugin(file=bad, admin={})
    assert "不是有效的 zip" in r["message"]

    nomanifest = UploadFile(
        file=io.BytesIO(_zip_bytes({"readme.txt": b"hi"})), filename="p.zip")
    r2 = await upload_plugin(file=nomanifest, admin={})
    assert "manifest.json" in r2["message"]


async def test_upload_plugin_zip_bomb_guards(tmp_path, monkeypatch):
    _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile
    # 解压后体积超限：600MB 全零 → 压缩后 ~600KB
    bomb = _zip_bytes({"big.bin": b"\0" * (600 * 1024 * 1024)})
    uf = UploadFile(file=io.BytesIO(bomb), filename="bomb.zip")
    r = await upload_plugin(file=uf, admin={})
    assert "解压后体积过大" in r["message"]
    # 压缩比超限：1MB 全零 → 压缩后 ~1KB → 比值 > 100
    ratio = _zip_bytes({"big.bin": b"\0" * (1024 * 1024)})
    uf2 = UploadFile(file=io.BytesIO(ratio), filename="ratio.zip")
    r2 = await upload_plugin(file=uf2, admin={})
    assert "压缩比异常" in r2["message"]


async def test_upload_plugin_manifest_validation_failures(tmp_path,
                                                          monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile

    bad_json = UploadFile(
        file=io.BytesIO(_zip_bytes({"manifest.json": b"{nope"})),
        filename="p.zip")
    r = await upload_plugin(file=bad_json, admin={})
    assert "manifest 校验失败" in r["message"]

    bad_id = dict(_VALID_UPLOAD, id="../evil")
    uf2 = UploadFile(file=io.BytesIO(_zip_bytes(
        {"manifest.json": json.dumps(bad_id).encode()})), filename="p.zip")
    r2 = await upload_plugin(file=uf2, admin={})
    assert "非法字符" in r2["message"]

    no_entry = UploadFile(
        file=io.BytesIO(_zip_bytes(
            {"manifest.json": json.dumps(_VALID_UPLOAD).encode()})),
        filename="p.zip")
    r3 = await upload_plugin(file=no_entry, admin={})
    assert "入口文件" in r3["message"]

    (plugin_root / "plugnew").mkdir()
    dup = UploadFile(
        file=io.BytesIO(_zip_bytes({
            "manifest.json": json.dumps(_VALID_UPLOAD).encode(),
            "plugin.py": b"print(1)"})),
        filename="p.zip")
    r4 = await upload_plugin(file=dup, admin={})
    assert "已存在" in r4["message"]


async def test_upload_plugin_success_with_subdir_and_traversal_guard(
        tmp_path, monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile
    files = {
        "pkg/manifest.json": json.dumps(_VALID_UPLOAD).encode(),
        "pkg/plugin.py": b"print('hi')",
        "pkg": b"",                # 与 subdir 同名的裸条目 → 空 rel → 跳过
        "pkg/": b"",               # 目录条目 → 跳过
        "__MACOSX/junk": b"",      # __MACOSX → 跳过
        "pkg/assets/a.txt": b"data",
        "../evil.txt": b"nope",
    }
    adapter_calls: list = []
    monkeypatch.setattr(
        "app.agents.model_family_adapters.refresh_plugin_adapters",
        lambda *a, **kw: adapter_calls.append(1))
    uf = UploadFile(file=io.BytesIO(_zip_bytes(files)), filename="p.zip")
    result = await upload_plugin(file=uf, admin={})
    assert result["success"] is True
    assert result["data"]["id"] == "plugnew"
    target = plugin_root / "plugnew"
    assert (target / "plugin.py").read_bytes() == b"print('hi')"
    assert (target / "assets" / "a.txt").exists()
    assert not (plugin_root / "evil.txt").exists()  # 穿越条目被拒
    assert adapter_calls == [1]


async def test_upload_plugin_extraction_failure_rolls_back(tmp_path,
                                                           monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile
    files = {
        "manifest.json": json.dumps(_VALID_UPLOAD).encode(),
        "plugin.py": b"print('x')",
        "a": b"file first",
        "a/b.txt": b"then dir under file, mkdir fails",
    }
    uf = UploadFile(file=io.BytesIO(_zip_bytes(files)), filename="p.zip")
    result = await upload_plugin(file=uf, admin={})
    assert result["success"] is False
    assert "解压失败" in result["message"]
    assert not (plugin_root / "plugnew").exists()  # 已回滚


async def test_upload_plugin_size_cap_cleans_up(tmp_path, monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    (plugin_root / "p1").mkdir(parents=True)
    from fastapi import UploadFile
    monkeypatch.setattr(routes_mod, "MAX_PLUGIN_FILE_SIZE", 4)
    uf = UploadFile(file=io.BytesIO(b"0123456789"), filename="big.bin")
    result = await upload_plugin_file("p1", file=uf, admin={})
    assert result["success"] is False
    assert "文件过大" in result["message"]


async def test_upload_plugin_refresh_failure_still_succeeds(tmp_path,
                                                             monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    from fastapi import UploadFile

    def _boom():
        raise RuntimeError("adapter refresh failed")
    monkeypatch.setattr(
        "app.agents.model_family_adapters.refresh_plugin_adapters", _boom)
    uf = UploadFile(
        file=io.BytesIO(_zip_bytes({
            "manifest.json": json.dumps(_VALID_UPLOAD).encode(),
            "plugin.py": b"print(1)"})),
        filename="p.zip")
    result = await upload_plugin(file=uf, admin={})
    assert result["success"] is True  # 刷新失败只记日志，上传不受影响
    assert (plugin_root / "plugnew" / "plugin.py").exists()


async def test_export_plugin(tmp_path, monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    pdir = plugin_root / "p1"
    pdir.mkdir()
    (pdir / "manifest.json").write_text("{}", encoding="utf-8")
    (pdir / "sub").mkdir()
    (pdir / "sub" / "x.py").write_text("print(1)", encoding="utf-8")

    resp = await export_plugin("p1")
    assert resp.status_code == 200
    assert resp.headers["content-disposition"] == \
        'attachment; filename="p1.zip"'
    body = b""
    async for chunk in resp.body_iterator:
        body += chunk
    zf = zipfile.ZipFile(io.BytesIO(body))
    assert set(zf.namelist()) == {"manifest.json", "sub/x.py"}

    assert (await export_plugin("../evil"))["success"] is False
    assert (await export_plugin("ghost"))["success"] is False


async def test_delete_plugin_full_flow(tmp_path, monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    pdir = plugin_root / "p1"
    pdir.mkdir()
    (pdir / "manifest.json").write_text("{}", encoding="utf-8")
    updir = tmp_path / "app" / "data" / "uploads" / "p1"
    updir.mkdir(parents=True)
    (updir / "f.bin").write_bytes(b"x")

    proc = SimpleNamespace(is_alive=True, stop=AsyncMock())
    sup = MagicMock()
    sup.get_process.return_value = proc
    layer = MagicMock()
    layer._supervisor = sup
    fake_main = types.ModuleType("app.main")
    cam = MagicMock(unregister_plugin_cameras=AsyncMock())
    fake_main._services = {"camera_manager": cam}
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    fake_db = MagicMock(vision_logs_delete_camera=AsyncMock())
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod.Database, "get",
                        classmethod(lambda cls: fake_db))
    adapter_calls: list = []
    monkeypatch.setattr(
        "app.agents.model_family_adapters.refresh_plugin_adapters",
        lambda *a, **kw: adapter_calls.append(1))

    result = await delete_plugin("p1", container=_container(layer),
                                 admin={})
    assert result == {"success": True, "data": {"id": "p1"}}
    proc.stop.assert_awaited_once()
    cam.unregister_plugin_cameras.assert_awaited_with("p1")
    fake_db.vision_logs_delete_camera.assert_awaited_with("vcam_p1")
    assert not pdir.exists()
    assert not updir.exists()  # 上传文件一并物理回收
    assert adapter_calls == [1]


async def test_delete_plugin_failure_paths_tolerated(tmp_path, monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    pdir = plugin_root / "p1"
    pdir.mkdir()

    # stop 抛错 + 无 camera_manager + DB 未就绪 → 仍然删除成功
    proc = SimpleNamespace(is_alive=True,
                           stop=AsyncMock(side_effect=RuntimeError("nope")))
    sup = MagicMock()
    sup.get_process.return_value = proc
    layer = MagicMock()
    layer._supervisor = sup
    fake_main = types.ModuleType("app.main")
    fake_main._services = {}
    monkeypatch.setitem(sys.modules, "app.main", fake_main)

    def _boom():
        raise RuntimeError("no db")
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod.Database, "get",
                        classmethod(lambda cls: _boom()))
    result = await delete_plugin("p1", container=_container(layer), admin={})
    assert result["success"] is True
    assert not pdir.exists()

    # 非法 id / 插件不存在 / layer None
    r2 = await delete_plugin("../evil", container=_container(layer),
                             admin={})
    assert r2["success"] is False
    r3 = await delete_plugin("ghost", container=_container(layer), admin={})
    assert r3["success"] is False
    r4 = await delete_plugin("p1", container=_container(None), admin={})
    assert r4["success"] is False  # 目录已删


async def test_delete_plugin_camera_unregister_failure_tolerated(tmp_path,
                                                                 monkeypatch):
    plugin_root = _upload_route(monkeypatch, tmp_path)
    pdir = plugin_root / "p2"
    pdir.mkdir()
    proc = SimpleNamespace(is_alive=False, stop=AsyncMock())
    sup = MagicMock()
    sup.get_process.return_value = proc
    layer = MagicMock()
    layer._supervisor = sup
    fake_main = types.ModuleType("app.main")
    cam = MagicMock(unregister_plugin_cameras=AsyncMock(
        side_effect=RuntimeError("camera core exploded")))
    fake_main._services = {"camera_manager": cam}
    monkeypatch.setitem(sys.modules, "app.main", fake_main)
    fake_db = MagicMock(vision_logs_delete_camera=AsyncMock())
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod.Database, "get",
                        classmethod(lambda cls: fake_db))
    result = await delete_plugin("p2", container=_container(layer), admin={})
    assert result["success"] is True  # 注销失败只告警，删除继续
    assert not pdir.exists()


async def test_toggle_and_ui_routes_layer_none():
    assert (await toggle_broadcast(container=_container(None)))[
        "success"] is False
    assert (await list_ui_contributions(container=_container(None)))[
        "data"] == []
    assert (await get_state("broadcast_enabled",
                            container=_container(None)))["success"] is False
    assert (await invoke_action("toggle_broadcast", {},
                                container=_container(None)))[
        "success"] is False
