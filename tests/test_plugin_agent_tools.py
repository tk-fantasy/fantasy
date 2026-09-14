"""agent_tools 插件能力测试：tools.list / tools.call 分发、capability 校验、host.mode。"""

import asyncio

from app.integration.rpc_protocol import (
    METHOD_HOST_MODE_GET,
    METHOD_HOST_MODE_SET,
    METHOD_TOOLS_CALL,
    METHOD_TOOLS_LIST,
)
from app.integration.sdk.plugin_base import (
    HostProxy,
    IntegrationPlugin,
    ToolDefinition,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _ToolPlugin(IntegrationPlugin):
    """声明 agent_tools 的最小插件：一个 echo 工具。"""

    async def _echo(self, arguments: dict, context: dict) -> dict:
        return {"echo": arguments.get("text", ""), "user_id": context.get("user_id", "")}

    def setup(self, manifest_dict):
        self.manifest = manifest_dict
        self.tools = [ToolDefinition(
            name="echo",
            description="回声测试",
            parameters={"type": "object", "properties": {
                "text": {"type": "string"}}},
            handler=self._echo,
        )]


class _NoToolPlugin(IntegrationPlugin):
    """未声明 agent_tools 的插件（manifest 无该 capability）。"""


_AGENT_MANIFEST = {"id": "t", "capabilities": [{"type": "agent_tools", "id": "t_tools"}]}
_EMPTY_MANIFEST = {"id": "t2", "capabilities": []}


def test_tools_list_returns_definitions_without_handler():
    plugin = _ToolPlugin()
    plugin.setup(_AGENT_MANIFEST)

    result = _run(plugin.handle(METHOD_TOOLS_LIST, {}))

    assert result["tools"] == [{
        "name": "echo",
        "description": "回声测试",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
    }]


def test_tools_call_dispatches_to_handler_with_context():
    plugin = _ToolPlugin()
    plugin.setup(_AGENT_MANIFEST)

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {
        "name": "echo", "arguments": {"text": "hi"}, "context": {"user_id": "u1"},
    }))

    assert result == {"echo": "hi", "user_id": "u1"}


def test_tools_call_unknown_tool_returns_error():
    plugin = _ToolPlugin()
    plugin.setup(_AGENT_MANIFEST)

    result = _run(plugin.handle(METHOD_TOOLS_CALL, {"name": "nope"}))

    assert "error" in result
    assert "nope" in result["error"]


def test_tools_methods_rejected_without_capability():
    """未声明 agent_tools 的插件：tools.list/call 被 capability 校验拒绝。"""
    plugin = _ToolPlugin()
    plugin.setup(_EMPTY_MANIFEST)

    listed = _run(plugin.handle(METHOD_TOOLS_LIST, {}))
    called = _run(plugin.handle(METHOD_TOOLS_CALL, {"name": "echo"}))

    assert "not declared" in listed["error"]
    assert "not declared" in called["error"]


def test_host_mode_set_get_via_reverse_rpc():
    """host.mode.set/get 走方向 2 RPC（mode.set/mode.get），权限校验由宿主做。"""
    calls = []

    async def host_call(method, params=None):
        calls.append((method, params))
        if method == METHOD_HOST_MODE_SET:
            return {"mode": params["mode"]}
        if method == METHOD_HOST_MODE_GET:
            return {"mode": "xiaoai_direct"}
        raise RuntimeError(f"unexpected {method}")

    proxy = HostProxy(host_call)

    set_result = _run(proxy.mode.set("xiaoai_direct"))
    mode = _run(proxy.mode.get())

    assert set_result == {"mode": "xiaoai_direct"}
    assert mode == "xiaoai_direct"
    assert calls[0] == (METHOD_HOST_MODE_SET, {"mode": "xiaoai_direct"})
    assert calls[1][0] == METHOD_HOST_MODE_GET
