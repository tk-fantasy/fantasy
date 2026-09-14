"""agents_tools / mode 反向 RPC 的真实子进程 e2e（慢速标记）。

用真实子进程跑 echo 插件（test 专属，不依赖小爱硬件）：
- 宿主装配（tools.list → MCPTool → tools.call 宿主侧代理）全链路；
- 插件经 mode.set/mode.get 反向 RPC 改宿主全局聊天模式（小爱直通同一通道）。
"""
import asyncio

import pytest

from app.integration.integration_layer import IntegrationLayer
from app.mcp.mcp_client_manager import MCPClientManager, MCPTool
from app.integration.config_helper import get_current_mode as _mode, set_current_mode as _set_mode

pytestmark = pytest.mark.slow

PLUGINS_DIR = "tests/integrations"


async def _install_echo_tool(layer: IntegrationLayer, manager: MCPClientManager) -> dict:
    """复制宿主装配逻辑：按 tools.list + MCPTool + handler→可调用。

    返回 {全名: handler}。tools.call 的宿主侧代理该走
    layer.get_agent_tool_plugins() 现查进程——这里用同一入口。
    """
    pid, proc = layer.get_agent_tool_plugins()[0]
    result = await proc.call("tools.list", {})
    from app.main import _make_plugin_tool_handler

    out: dict = {}
    for tool in result["tools"]:
        handler = _make_plugin_tool_handler(layer, pid, tool["name"])
        full = f"{pid}___{tool['name']}"
        manager.register_tool(MCPTool(
            client_id=pid, tool_name=tool["name"],
            description=tool["description"], parameters=tool["parameters"],
            handler=handler,
        ))
        out[full] = handler
    return out


def test_agent_tools_end_to_end_via_subprocess():
    """真实子进程：tools.list 定义 → 宿主代理 tools.call → 插件结果回传。"""
    layer = IntegrationLayer(
        plugin_dir=PLUGINS_DIR, api_version="1", rpc_timeout=15.0, max_restarts=0,
        host_deps={"ha_client": None, "ha_service": None, "llm_chat_client": None},
    )
    manager = MCPClientManager()

    async def go():
        try:
            await layer.start()
            assert any(pid == "echo" for pid, _ in layer.get_agent_tool_plugins())
            installed = await _install_echo_tool(layer, manager)
            full = "echo___echo_tool"
            assert full in installed
            result = await installed[full](
                {"text": "你好"}, type("S", (), {
                    "user_id": "u9", "current_query": "测试工具",
                })())
            assert result["echo"] == "你好"
            assert result["user_id"] == "u9"
            # 插件进程内读宿主全局模式（mode.get 反向 RPC）——默认 aether
            assert result["mode"] == "aether"
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())


def test_plugin_can_flip_host_global_mode():
    """插件工具经 mode.set 反向 RPC 改宿主全局 current_mode，读回一致。"""
    layer = IntegrationLayer(
        plugin_dir=PLUGINS_DIR, api_version="1", rpc_timeout=15.0, max_restarts=0,
        host_deps={"ha_client": None, "ha_service": None, "llm_chat_client": None},
    )
    manager = MCPClientManager()

    async def go():
        try:
            await layer.start()
            installed = await _install_echo_tool(layer, manager)
            flip = installed["echo___flip_mode"]
            # 切到小爱直通模式：保存现场，测完恢复（防污染其他测试）
            prev = _mode()
            try:
                result = await flip(
                    {"mode": "xiaoai_direct"},
                    type("S", (), {"user_id": "", "current_query": ""})(),
                )
                assert result["mode"] == "xiaoai_direct"
                assert _mode() == "xiaoai_direct"   # 宿主全局确实被改
                await flip({"mode": "aether"},
                           type("S", (), {"user_id": "", "current_query": ""})())
                assert _mode() == "aether"
            finally:
                _set_mode(prev)  # 还原（即便断言失败）
        finally:
            await layer.stop()

    asyncio.new_event_loop().run_until_complete(go())